"""Skills: reusable Dobot workflows.

A skill is a directory under ``skills/`` holding ``SKILL.md`` (intent, trigger, required tools, safety
requirements) and optionally ``workflow.json`` (machine-readable steps). Loaded skills are offered to
the planner, and a skill Dobot learns is written here so the user can read, edit or delete it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.logging_setup import get_logger
from app.schemas import SkillRecord
from app.security.skill_scanner import ScanReport, scan_skill

logger = get_logger(__name__)

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KV = re.compile(r"^([A-Za-z_]+):\s*(.*)$")


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        kv = _KV.match(line.strip())
        if kv:
            meta[kv.group(1).strip().lower()] = kv.group(2).strip().strip("'\"")
    return meta, text[match.end() :]


def _split_list(value: str) -> list[str]:
    cleaned = value.strip().strip("[]")
    return [part.strip().strip("'\"") for part in cleaned.split(",") if part.strip()]


@dataclass
class SkillsLibrary:
    directory: Path
    #: None means "read from settings at call time", so constructing a library in a test is enough
    #: to control it without touching the environment.
    scan_mode: str | None = None
    allowlist: list[str] | None = None
    _skills: dict[str, SkillRecord] | None = None
    _reports: dict[str, ScanReport] | None = None
    #: Runtime guards a skill ships for itself, keyed by skill name. Compiled by the interceptor
    #: engine, so a skill can carry enforceable rules rather than prose the model may ignore.
    _interceptors: dict[str, object] | None = None

    @classmethod
    def default(cls) -> SkillsLibrary:
        return cls(directory=get_settings().skills_dir)

    # ------------------------------------------------------------------ scanning

    def _scan_settings(self) -> tuple[str, list[str]]:
        if self.scan_mode is not None:
            return self.scan_mode, list(self.allowlist or [])
        settings = get_settings()
        return settings.skill_scan_mode, settings.skill_allowlist

    def scan(self, *, force: bool = False) -> dict[str, ScanReport]:
        """Scan every skill. Delegates to `load()` so there is exactly one pass over the folder and
        the reports are keyed by the skill's own name, matching what the planner and the UI see."""
        self.load(force=force)
        return self._reports or {}

    def reports(self) -> list[ScanReport]:
        return [self.scan()[name] for name in sorted(self.scan())]

    def blocked_names(self) -> list[str]:
        """Skill names the scanner refused. These never reach the planner and can never run."""
        return sorted(name for name, report in self.scan().items() if report.blocked)

    def flagged_names(self) -> list[str]:
        return sorted(name for name, report in self.scan().items() if report.findings)

    # ------------------------------------------------------------------ loading

    def load(self, *, force: bool = False) -> dict[str, SkillRecord]:
        if self._skills is not None and not force:
            return self._skills
        mode, allowlist = self._scan_settings()
        reports: dict[str, ScanReport] = {}
        skills: dict[str, SkillRecord] = {}
        interceptors: dict[str, object] = {}
        if not self.directory.exists():
            self._skills = skills
            return skills
        for path in sorted(self.directory.iterdir()):
            if not path.is_dir():
                continue
            skill_file = path / "SKILL.md"
            if not skill_file.exists():
                continue
            try:
                raw = skill_file.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("could not read skill %s (%s)", path.name, exc)
                continue
            meta, body = _parse_frontmatter(raw)
            workflow: list[dict] = []
            workflow_file = path / "workflow.json"
            if workflow_file.exists():
                try:
                    payload = json.loads(workflow_file.read_text(encoding="utf-8"))
                    workflow = payload.get("steps", payload) if isinstance(payload, dict) else payload
                    if isinstance(payload, dict) and payload.get("interceptors"):
                        interceptors[meta.get("name") or path.name] = payload["interceptors"]
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("invalid workflow for %s (%s)", path.name, exc)
            name = meta.get("name") or path.name
            report = scan_skill(name, path, mode=mode, allowlist=allowlist)
            reports[name] = report
            skills[name] = SkillRecord(
                name=name,
                description=meta.get("description", "") or body.strip().splitlines()[0][:160] if body.strip() else "",
                trigger=meta.get("trigger", ""),
                required_tools=_split_list(meta.get("required_tools", "")) if meta.get("required_tools") else [],
                safety=meta.get("safety", ""),
                workflow=[step for step in workflow if isinstance(step, dict)],
                path=str(path),
                body=body.strip(),
                scan_status=report.status,
                scan_findings=[finding.as_dict() for finding in report.findings],
            )
        self._skills = skills
        # The reports from this pass are the cache: `scan()` and `load()` cannot disagree because
        # they are the same pass.
        self._reports = reports
        self._interceptors = interceptors
        return skills

    def interceptor_rules(self) -> dict[str, object]:
        """Per-skill runtime guards, for the interceptor engine to compile."""
        self.load()
        return dict(self._interceptors or {})

    # ------------------------------------------------------------------ access

    def get(self, name: str) -> SkillRecord | None:
        return self.load().get(name)

    def names(self) -> list[str]:
        return sorted(self.load())

    def records(self) -> list[SkillRecord]:
        """Every loaded skill, including flagged ones — the UI must be able to show them."""
        return [self.load()[name] for name in self.names()]

    def active(self) -> list[SkillRecord]:
        """Skills that may be offered to the planner: everything except scanner-blocked ones."""
        blocked = set(self.blocked_names())
        return [skill for skill in self.records() if skill.name not in blocked]

    def matching(self, message: str, limit: int = 3) -> list[SkillRecord]:
        """Cheap relevance match so the planner can be told which skills apply."""
        lowered = message.lower()
        hits: list[tuple[int, SkillRecord]] = []
        for skill in self.active():
            score = 0
            if skill.name.replace("_", " ") in lowered:
                score += 5
            for word in re.findall(r"[a-z]{4,}", skill.trigger.lower() or skill.description.lower()):
                if word in lowered:
                    score += 1
            if score:
                hits.append((score, skill))
        hits.sort(key=lambda item: item[0], reverse=True)
        return [skill for _, skill in hits[:limit]]

    def prompt_section(self, skills: list[SkillRecord] | None = None) -> str:
        records = skills if skills is not None else self.active()
        lines: list[str] = []
        for skill in records:
            tools = ", ".join(skill.required_tools) or "unspecified"
            lines.append(f"- {skill.name}: {skill.description} (tools: {tools})")
            if skill.workflow:
                for index, step in enumerate(skill.workflow, start=1):
                    lines.append(f"    {index}. {json.dumps(step, ensure_ascii=False)[:200]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ learning

    def save(self, name: str, body: str, *, description: str = "", workflow: list[dict] | None = None,
             trigger: str = "", required_tools: list[str] | None = None, safety: str = "") -> SkillRecord:
        directory = self.directory / name
        directory.mkdir(parents=True, exist_ok=True)
        frontmatter = ["---", f"name: {name}", f"description: {description}"]
        if trigger:
            frontmatter.append(f"trigger: {trigger}")
        if required_tools:
            frontmatter.append(f"required_tools: [{', '.join(required_tools)}]")
        if safety:
            frontmatter.append(f"safety: {safety}")
        frontmatter.append("---")
        (directory / "SKILL.md").write_text(
            "\n".join(frontmatter) + "\n\n" + body.strip() + "\n", encoding="utf-8"
        )
        if workflow:
            (directory / "workflow.json").write_text(
                json.dumps({"steps": workflow}, indent=2), encoding="utf-8"
            )
        # A just-written skill is scanned like any other: Dobot writing its own skill does not make
        # the content trustworthy, and a false positive here is a bug in the skill it just wrote.
        self.scan(force=True)
        self.load(force=True)
        record = self.get(name)
        assert record is not None
        return record

    def delete(self, name: str) -> bool:
        directory = self.directory / name
        if not directory.exists():
            return False
        for file in directory.iterdir():
            file.unlink()
        directory.rmdir()
        self._reports = None
        self._interceptors = None
        self.load(force=True)
        return True
