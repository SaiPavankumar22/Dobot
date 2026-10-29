"""The eval runner: one message through the real HTTP surface, scored mechanically.

Design notes
------------
* **Hermetic by default.** Each case boots the real FastAPI app (``create_app``) inside
  a throwaway HOME/state directory with every provider key cleared — the same isolation
  the unit suite uses. The offline planner path therefore runs, which is exactly the
  deterministic behaviour we want to pin: if a refactor changes what the rules plan, a
  case breaks.
* **Real approvals.** When a case expects a gate, the runner polls ``GET /approvals``
  and either approves (``auto_approve``) or asserts the task is waiting. Approval ids
  come from the API, never guessed.
* **No judge model.** Assertions are plan shape, world state (files, memories) and the
  Verifier's own checks. That keeps the whole suite runnable with zero API keys, which
  is the point versus DeepEval/promptfoo judge-based flows.

Usage:  cd backend && uv run python -m evals.runner [--live] [--json out.json]
         --write-baseline   accept the current results as the comparison baseline
Exit code is non-zero when any case fails **or** a case that passed in
``evals/baseline.json`` regresses/disappears, so CI can call it directly. Tool-plan
drift versus the baseline is reported as a warning (the planner legitimately evolves);
pass/fail regressions are hard failures.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evals.cases import LIVE_CASES, OFFLINE_CASES, Case

_MAX_APPROVAL_ROUNDS = 8
#: Snapshot of a known-good run; the runner diffs against it automatically so a
#: previously-passing case that breaks (or vanishes) is called out by name.
BASELINE_PATH = Path(__file__).with_name("baseline.json")


@dataclass
class CaseResult:
    id: str
    passed: bool
    duration_s: float
    failures: list[str] = field(default_factory=list)
    status: str = ""
    planned_tools: list[str] = field(default_factory=list)
    checks_run: int = 0
    checks_passed: int = 0
    answer: str = ""


class _Env:
    """Per-case environment: throwaway HOME, no provider keys, offline defaults."""

    def __init__(self, case: Case) -> None:
        self.case = case
        self.root = Path(tempfile.mkdtemp(prefix=f"dobot-eval-{case.id}-"))
        self.home = self.root / "home"
        (self.home / "Downloads").mkdir(parents=True, exist_ok=True)
        for rel in case.seed_home:
            target = self.home / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("seed", encoding="utf-8")
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> _Env:
        pins = {
            "DOBOT_STATE_DIR": str(self.root / "state"),
            "DOBOT_ENV": "test",
            "NEBIUS_API_KEY": "",
            "NEBIUS_PROJECT_ID": "",
            "TAVILY_API_KEY": "",
            "SANDBOX_PROVIDER": "local",
            "AGENT_RUNTIME": "local",
            "SHADOW_MODE": "false",
            "REQUIRE_WRITE_APPROVAL": "false",
            "SANDBOX_ALLOWED_PATHS": str(self.root),
            "SANDBOX_ALLOWED_NETWORK": "example.com,localhost",
            "DOBOT_SKILLS_DIR": str(self.root / "skills"),
            "DOBOT_API_TOKEN": "",
            "VOICE_ENABLED": "false",
            "CONSOLIDATION_ENABLED": "false",
            "INTERCEPTORS_ENABLED": "true",
            "TOKENJUICE_ENABLED": "true",
            "MEMORY_DECAY_ENABLED": "true",
            "PRICE_PER_MTOK_INPUT": "0",
            "PRICE_PER_MTOK_OUTPUT": "0",
            "PRINCIPAL_NAME": "",
            "HERMES_SIDECAR": "0",
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            **self.case.settings_overrides,
        }
        for key, value in pins.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value
        # The developer's real .env must never leak into a case.
        from app.config import reset_settings_cache

        reset_settings_cache()
        # …and neither may the machine's credential store. ``build_services`` calls
        # ``local_secrets.hydrate()``, which happily resurrects NEBIUS_API_KEY from the OS keyring
        # right after the pin above cleared it — so the "offline" suite silently called a live
        # provider, paid for it, and then failed on whatever state that left behind. Same pin as
        # tests/conftest.py: an eval must measure Dobot, not a third party's uptime.
        from unittest.mock import patch

        self._keyring = patch("app.security.local_secrets._keyring", lambda: None)
        self._keyring.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        from app.config import reset_settings_cache

        self._keyring.stop()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_settings_cache()
        shutil.rmtree(self.root, ignore_errors=True)
        return None


def _plan_tools(chat_json: dict[str, Any]) -> list[str]:
    steps = ((chat_json.get("plan") or {}).get("steps")) or []
    tools: list[str] = []
    for step in steps:
        tool = (step.get("action") or {}).get("tool") or step.get("tool")
        if tool:
            tools.append(str(tool))
    return tools


def _verification_stats(chat_json: dict[str, Any]) -> tuple[int, int]:
    run = passed = 0
    for verification in chat_json.get("verifications") or []:
        for check in verification.get("checks") or []:
            run += 1
            if check.get("passed"):
                passed += 1
    return run, passed


def _drive_chat(client: Any, message: str, *, auto_approve: bool) -> dict[str, Any]:
    """POST /chat, then resolve approvals like a human would until the task settles."""
    response = client.post("/chat", json={"message": message})
    chat = response.json()
    for _ in range(_MAX_APPROVAL_ROUNDS):
        if chat.get("status") != "WAITING_APPROVAL":
            return chat
        pending = client.get("/approvals").json()
        if not pending:
            return chat
        if not auto_approve:
            return chat
        # Approve the oldest approval belonging to this chat's task, then the API
        # response *is* the resumed ChatResponse — exactly what the app does.
        task_id = chat.get("task_id")
        mine = [a for a in pending if task_id is None or a.get("task_id") == task_id] or pending
        decision = client.post(
            f"/approvals/{mine[0]['id']}",
            json={"decision": "approve", "note": "eval harness"},
        )
        resumed = decision.json()
        if isinstance(resumed, dict) and resumed.get("status"):
            chat = resumed
    return chat


def _run_case(case: Case) -> CaseResult:
    started = time.perf_counter()
    result = CaseResult(id=case.id, passed=True, duration_s=0.0)
    memories: list[str] = []
    with _Env(case):
        # Imported inside the env block: Settings is cached at import time otherwise.
        from fastapi.testclient import TestClient

        from app.config import Settings
        from app.events import EventBus, set_event_bus
        from app.main import create_app

        set_event_bus(EventBus())
        app = create_app(Settings())
        with TestClient(app) as client:
            chat = _drive_chat(client, case.message, auto_approve=case.auto_approve)
            if case.memory_contains:
                records = client.get("/memory").json()
                memories = [str(record.get("content") or "") for record in records]
        # World state must be read before __exit__ deletes the throwaway HOME.
        missing_files = [
            rel
            for rel, _needle in case.files
            if not (Path(os.environ["HOME"]) / rel).exists()
        ]
        wrong_content = [
            rel
            for rel, needle in case.files
            if needle
            and (Path(os.environ["HOME"]) / rel).exists()
            and needle.lower() not in (Path(os.environ["HOME"]) / rel).read_text(encoding="utf-8").lower()
        ]

    result.status = str(chat.get("status") or "")
    result.planned_tools = _plan_tools(chat)
    result.answer = str(chat.get("answer") or "")[:200]

    def fail(reason: str) -> None:
        result.passed = False
        result.failures.append(reason)

    if case.expect_status and result.status != case.expect_status:
        fail(f"status {result.status!r} != expected {case.expect_status!r}")

    tools = result.planned_tools
    missing = [tool for tool in case.expect_tools if tool not in tools]
    if missing:
        fail(f"plan missing tools {missing} (planned {tools})")
    if case.tool_order and not missing:
        positions = [tools.index(tool) for tool in case.expect_tools]
        if positions != sorted(positions):
            fail(f"expected order {list(case.expect_tools)} but planned {tools}")
    forbidden = [tool for tool in case.forbid_tools if tool in tools]
    if forbidden:
        fail(f"plan contains forbidden tools {forbidden}")

    checks_run, checks_passed = _verification_stats(chat)
    result.checks_run, result.checks_passed = checks_run, checks_passed
    if case.min_verification and checks_run and checks_passed < case.min_verification * checks_run:
        fail(f"only {checks_passed}/{checks_run} verification checks passed")

    if case.answer_contains and case.answer_contains.lower() not in result.answer.lower():
        fail(f"answer does not contain {case.answer_contains!r}")

    if missing_files:
        fail(f"files not created on disk: {missing_files}")
    if wrong_content:
        fail(f"file content missing substring: {wrong_content}")
    for needle in case.memory_contains:
        if not any(needle.lower() in text.lower() for text in memories):
            fail(f"no memory contains {needle!r}")

    result.duration_s = round(time.perf_counter() - started, 2)
    return result


def run_suite(cases: tuple[Case, ...]) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in cases:
        results.append(_run_case(case))
    return results


def write_baseline(results: list[CaseResult], mode: str) -> Path:
    """Persist the current run as the known-good baseline (intentional behaviour changes end here)."""
    payload = {
        "mode": mode,
        "results": [
            {
                "id": result.id,
                "passed": result.passed,
                "status": result.status,
                "checks_run": result.checks_run,
                "checks_passed": result.checks_passed,
                "planned_tools": result.planned_tools,
            }
            for result in results
        ],
    }
    BASELINE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return BASELINE_PATH


def compare_baseline(results: list[CaseResult], mode: str) -> tuple[list[str], list[str]]:
    """Diff this run against ``evals/baseline.json``.

    Returns ``(regressions, notes)``. Regressions are hard failures: a case that passed
    before but fails now, or one that passed before but did not run at all (silent
    coverage loss). Notes are informational: improvements (rewrite the baseline) and
    tool-plan drift (the planner's plan legitimately changes; look, don't fail).
    """
    if not BASELINE_PATH.exists():
        return [], ["no baseline yet — run with --write-baseline to pin today's results"]
    try:
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"baseline unreadable ({exc}) — regenerate with --write-baseline"]
    if str(baseline.get("mode") or "") != mode:
        return [], [f"baseline is for mode '{baseline.get('mode')}', this run is '{mode}' — not compared"]

    was = {str(item.get("id")): item for item in baseline.get("results", [])}
    now = {result.id: result for result in results}
    regressions: list[str] = []
    notes: list[str] = []
    for case_id, old in was.items():
        if not old.get("passed"):
            continue  # a case that never passed cannot regress
        current = now.get(case_id)
        if current is None:
            regressions.append(
                f"{case_id}: passed in the baseline but did not run — coverage was removed"
            )
            continue
        if not current.passed:
            failures = "; ".join(current.failures[:2])
            regressions.append(f"{case_id}: passed in the baseline, now fails ({failures})")
            continue
        baseline_tools = list(old.get("planned_tools") or [])
        if baseline_tools != current.planned_tools:
            notes.append(
                f"{case_id}: plan drift — baseline planned "
                f"[{','.join(baseline_tools) or '-'}], now [{','.join(current.planned_tools) or '-'}]"
            )
    for case_id, current in now.items():
        old = was.get(case_id)
        if old is not None and not old.get("passed") and current.passed:
            notes.append(f"{case_id}: now passes but failed in the baseline — rewrite it with --write-baseline")
        elif old is None:
            notes.append(f"{case_id}: new case (not in the baseline)")
    return regressions, notes


def _report(results: list[CaseResult]) -> str:
    lines = ["", "Dobot evals", "-----------"]
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        lines.append(
            f"[{mark}] {result.id:<44} {result.status:<17} "
            f"tools={','.join(result.planned_tools) or '-':<28} "
            f"checks {result.checks_passed}/{result.checks_run} {result.duration_s}s"
        )
        for reason in result.failures:
            lines.append(f"       -> {reason}")
    passed = sum(1 for result in results if result.passed)
    lines.append(f"\n{passed}/{len(results)} cases passed")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dobot eval harness")
    parser.add_argument("--live", action="store_true", help="run the live (model-planned) cases")
    parser.add_argument("--json", dest="json_out", help="write machine-readable results here")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="accept the current results as the new evals/baseline.json (after reviewing a deliberate change)",
    )
    args = parser.parse_args(argv)

    if args.live and not os.environ.get("NEBIUS_API_KEY", "").strip():
        print("live cases need NEBIUS_API_KEY; run offline cases instead", file=sys.stderr)
        return 2

    mode = "live" if args.live else "offline"
    cases = LIVE_CASES if args.live else OFFLINE_CASES
    results = run_suite(cases)
    print(_report(results))
    if args.write_baseline:
        path = write_baseline(results, mode)
        print(f"\nbaseline written: {path.relative_to(Path(__file__).parent.parent)} ({mode})")
        return 0 if all(result.passed for result in results) else 1
    regressions, notes = compare_baseline(results, mode)
    if notes:
        print("\nBaseline notes:")
        for note in notes:
            print(f"  - {note}")
    if regressions:
        print("\nBASELINE REGRESSIONS:")
        for regression in regressions:
            print(f"  ! {regression}")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps([asdict(result) for result in results], indent=2), encoding="utf-8"
        )
    ok = all(result.passed for result in results) and not regressions
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
