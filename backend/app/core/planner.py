"""Planner.

Turns intent plus context into a validated, executable plan. Nemotron Ultra does the reasoning; this
module owns the *schema* — it validates every tool name and parameter shape, drops anything invented,
and always has a deterministic rule-based path so the loop still works offline.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.agents.nemotron import Reasoner
from app.config import Settings, get_settings
from app.core.tokenjuice import TokenJuice
from app.events import EventBus, EventType, get_event_bus
from app.logging_setup import get_logger
from app.schemas import (
    ActionSpec,
    ContextBundle,
    ModelTier,
    Plan,
    PlanStep,
    RiskLevel,
)
from app.tools.registry import ToolRegistry

logger = get_logger(__name__)

#: Nemotron's thinking trace is billed against the same max_tokens as the JSON answer, so a plan
#: needs far more headroom than its rendered size suggests. 1800 cut plans mid-document.
PLANNER_MAX_TOKENS = 4096


def _looks_like_path(value: str) -> bool:
    """True when a candidate extracted from prose could plausibly be a filesystem path.

    The offline planner pulls targets out of free text, so it has to reject the common case where
    the regex swallowed the rest of the sentence — "folder called X in my home directory. Inside it".
    """
    if not value or len(value) > 200:
        return False
    if any(mark in value for mark in (". ", ", ", "! ", "? ", ";")):
        return False  # sentence punctuation: this is prose
    if any(char in value for char in "/\\~"):
        return True
    # A single word ("Documents") or a short hyphenated name is a plausible relative name.
    return " " not in value

#: Tools whose effects can be undone by the user without data loss.
REVERSIBLE_TOOLS = {
    "fs_write",
    "fs_mkdir",
    "fs_move",
    "app_open",
    "browser_open",
    "browser_read",
    "tavily_search",
    "tavily_research",
    "screen_capture",
    "screen_analyze",
    "remember",
    "task_create",
    "reminder_create",
}

PLANNER_SYSTEM = """You are Dobot's planner. You run on the user's own computer and can act on it.

Rules:
1. Understand the user's real intent, including the selected screen context if present.
2. Use the smallest set of steps that accomplishes it. Prefer reading over writing, moving over deleting.
3. Never invent tools. Use only the tools listed below.
4. Prefer archiving/organising over deleting. Never delete more than the user explicitly asked for.
5. If the request needs current information (news, latest releases, papers, prices, docs), set
   needs_research=true and include a tavily_search or tavily_research step.
6. If the user asks you to remember something durable, include a `remember` step.
7. If the request is only a question, return no steps and put the full answer in "answer".
8. Steps that change the machine must state exactly what they will do in "description".
9. "Open the browser / go to <url>" with no interaction means the user wants to SEE the page:
   use app_open(app="browser", target="<url>"). browser_open and browser_read only fetch text
   over HTTP behind the scenes — no window appears. The URL must be passed as target; omitting
   it opens a blank browser, which is not what was asked for.
10. To WORK inside a page (type, click, download), drive the visible browser directly:
    browser_navigate(url) first — its output lists interactive elements with indices — then
    browser_type(target=..., text=..., press_enter=...) or browser_click(target=...).
    Prefer the element's numeric INDEX from that list as target; never guess a tag name
    ("textarea" often does not exist on modern app pages — the composer may be a div whose css
    contains "rich-textarea"). The snapshot's `input_indices` field lists the text-entry
    elements: to type, target one of those (the composer/chat box), never index 0 by default.
    Copy the element's css from the snapshot only if you need a
    selector. Re-read the page with browser_snapshot after actions; indices refresh after every
    action. A download triggered by a click is saved and its path appears in the result.
    Generation is slow: after sending a prompt that creates an image/file, wait with
    browser_snapshot(wait_seconds=15..30) before clicking a result or download button. If a
    click fails because the element was not found, the page was probably still loading —
    snapshot again (with wait_seconds) and retry the same click; do not end the task after the
    first failed click.
    Whole forms can use browser_submit (needs approval). Only desktop GUI apps (computer_*)
    need the Hermes runtime; browser interaction does not. Never invent claims about what a
    website can or cannot do.
11. Do the part you can. If a request has several parts and one needs an unavailable runtime,
    still plan the steps that work (e.g. open the browser at the page), then say plainly which
    part is blocked and why. Do not skip to a no-steps answer when there are steps you can run.

Reply with ONLY a JSON object:
{
  "intent": "short description of what the user wants",
  "summary": "one sentence for the user",
  "reasoning": "why these steps",
  "answer": "direct answer when no steps are needed, else empty string",
  "needs_research": false,
  "research_query": "",
  "confidence": 0.0-1.0,
  "memory_writes": ["durable facts worth remembering"],
  "steps": [
    {
      "tool": "tool_name",
      "params": {},
      "description": "what this does, in the user's terms",
      "expected": "what success looks like"
    }
  ]
}

Available tools:
%s
"""

STEP_SCHEMA_HINT = {"tool": "string", "params": "object", "description": "string", "expected": "string"}


class Planner:
    def __init__(
        self,
        *,
        reasoner: Reasoner,
        registry: ToolRegistry,
        bus: EventBus | None = None,
        settings: Settings | None = None,
        tokenjuice: TokenJuice | None = None,
    ) -> None:
        self.reasoner = reasoner
        self.registry = registry
        self.settings = settings or get_settings()
        self.bus = bus or get_event_bus()
        self.tokenjuice = tokenjuice or TokenJuice(
            enabled=bool(getattr(self.settings, "tokenjuice_enabled", True)),
            min_chars=int(getattr(self.settings, "tokenjuice_min_chars", 1200)),
            max_chars=int(getattr(self.settings, "tokenjuice_max_chars", 4000)),
        )
        self.system_prompt = PLANNER_SYSTEM % registry.prompt_section()

    # ------------------------------------------------------------------ main entry

    async def plan(
        self,
        message: str,
        bundle: ContextBundle,
        *,
        tier: ModelTier = ModelTier.ULTRA,
        task_id: str = "",
        extra_instruction: str = "",
    ) -> Plan:
        await self.bus.emit(
            EventType.PLANNING,
            message=f"Planning with {tier.value} tier",
            task_id=task_id,
            tier=tier.value,
        )
        plan: Plan
        if self.reasoner.available:
            plan = await self._model_plan(
                message, bundle, tier=tier, task_id=task_id, extra_instruction=extra_instruction
            )
        else:
            plan = self._offline_plan(message, bundle)
        plan = self._validate(plan, bundle)
        plan.model_tier = tier
        await self.bus.emit(
            EventType.PLAN_CREATED,
            message=plan.summary or plan.intent,
            task_id=task_id,
            plan=plan.model_dump(mode="json"),
            degraded=plan.degraded,
        )
        return plan

    async def _model_plan(
        self,
        message: str,
        bundle: ContextBundle,
        *,
        tier: ModelTier,
        task_id: str,
        extra_instruction: str,
    ) -> Plan:
        prompt_parts = [f"USER REQUEST:\n{message}"]
        # `as_prompt_context` already budgets itself with a hard character cut, which can slice a
        # sentence in half and drop the tail. TokenJuice compresses structurally instead: it keeps the
        # shape (and annotates every elision) so the model is never handed a silently truncated fact.
        context_text = bundle.as_prompt_context(max_chars=12_000)
        if context_text:
            compression = self.tokenjuice.compress(
                "planner-context", context_text, max_chars=8000, task_id=task_id
            )
            prompt_parts.append(f"CONTEXT:\n{compression.text}")
        if extra_instruction:
            prompt_parts.append(extra_instruction)
        prompt_parts.append(
            "Respond with the JSON object only."
        )
        response = await self.reasoner.chat(
            [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": "\n\n".join(prompt_parts)},
            ],
            tier=tier,
            json_mode=True,
            max_tokens=PLANNER_MAX_TOKENS,
            # The light tier only handles short conversational turns; a thinking trace there is
            # almost the entire response latency. Deep tiers keep the default (think).
            thinking=(tier is not ModelTier.LIGHT),
        )
        if response.finish_reason == "length" and not response.degraded:
            # Nemotron's thinking shares the max_tokens budget with the JSON, so the plan can be
            # cut mid-document. One retry with a larger ceiling is cheaper than executing an
            # offline rule that misreads the request.
            logger.warning("planner output truncated; retrying with a larger token budget")
            response = await self.reasoner.chat(
                [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": "\n\n".join(prompt_parts)},
                ],
                tier=tier,
                json_mode=True,
                max_tokens=PLANNER_MAX_TOKENS * 3,
                thinking=(tier is not ModelTier.LIGHT),
            )
        if response.degraded:
            logger.info("planning degraded to offline rules (model unreachable)")
            plan = self._offline_plan(message, bundle)
            plan.degraded = True
            plan.raw = response.content
            return plan
        payload = response.json()
        recognised = {"intent", "summary", "steps", "answer", "reasoning", "research_query"}
        if not payload or not (recognised & set(payload)):
            # `response.json()` also accepts a degenerate object like `{"": ""}` — truthy, but it
            # carries no plan at all. Falling through to _from_payload would yield an empty plan
            # that the final-answer pass then describes as if the work had happened.
            logger.warning("planner returned unparsable JSON; using offline rules")
            plan = self._offline_plan(message, bundle)
            plan.degraded = True
            plan.raw = response.content[:2000]
            return plan
        plan = self._from_payload(payload)
        plan.used_model = response.model
        plan.raw = response.content[:4000]
        return plan

    # ------------------------------------------------------------------ parsing

    def _from_payload(self, payload: dict[str, Any]) -> Plan:
        steps: list[PlanStep] = []
        for index, raw_step in enumerate(payload.get("steps") or []):
            if not isinstance(raw_step, dict):
                continue
            tool = str(raw_step.get("tool", "")).strip()
            if not tool:
                continue
            params = raw_step.get("params")
            if not isinstance(params, dict):
                params = {}
            steps.append(
                PlanStep(
                    index=index,
                    reason=str(raw_step.get("description", ""))[:400],
                    action=ActionSpec(
                        tool=tool,
                        params=params,
                        description=str(raw_step.get("description", ""))[:400],
                        expected=str(raw_step.get("expected", ""))[:400],
                        reversible=tool in REVERSIBLE_TOOLS,
                    ),
                )
            )
        memory_writes = payload.get("memory_writes") or []
        return Plan(
            intent=str(payload.get("intent", ""))[:400],
            summary=str(payload.get("summary", ""))[:600],
            reasoning=str(payload.get("reasoning", ""))[:2000],
            steps=steps,
            answer=str(payload.get("answer", "")),
            needs_research=bool(payload.get("needs_research")),
            research_query=str(payload.get("research_query", "")),
            memory_writes=[str(item) for item in memory_writes if str(item).strip()][:5],
            confidence=float(payload.get("confidence", 0.6) or 0.6),
        )

    def _validate(self, plan: Plan, bundle: ContextBundle) -> Plan:
        """Drop steps the registry cannot serve, and dedupe research/memory steps."""
        kept: list[PlanStep] = []
        dropped: list[str] = []
        seen: set[tuple[str, str]] = set()
        for step in plan.steps:
            tool = self.registry.get(step.action.tool)
            if tool is None:
                dropped.append(step.action.tool)
                continue
            key = (step.action.tool, json.dumps(step.action.params, sort_keys=True, default=str))
            if key in seen:
                continue
            seen.add(key)
            step.action.risk_floor = tool.risk_floor
            step.action.reversible = step.action.reversible or step.action.tool in REVERSIBLE_TOOLS
            kept.append(step)
        for index, step in enumerate(kept):
            step.index = index
        plan.steps = kept

        if plan.needs_research and not any(
            step.action.tool in {"tavily_search", "tavily_research", "research"} for step in kept
        ):
            query = plan.research_query or plan.intent or bundle.user_message
            kept.append(
                PlanStep(
                    index=len(kept),
                    reason="request needs current external information",
                    action=ActionSpec(
                        tool="tavily_research",
                        params={"topic": query[:300]},
                        description=f"Research: {query[:120]}",
                        expected="sources with URLs",
                        risk_floor=RiskLevel.LOW,
                        reversible=True,
                    ),
                )
            )
            plan.steps = kept

        for content in plan.memory_writes:
            if not any(step.action.tool == "remember" and step.action.params.get("content") == content for step in plan.steps):
                plan.steps.append(
                    PlanStep(
                        index=len(plan.steps),
                        reason="durable fact worth remembering",
                        action=ActionSpec(
                            tool="remember",
                            params={"content": content, "type": "fact", "importance": 0.6},
                            description=f"Remember: {content[:80]}",
                            expected="memory stored and retrievable",
                            risk_floor=RiskLevel.LOW,
                            reversible=True,
                        ),
                    )
                )
        if dropped:
            plan.reasoning += f"\n(dropped unknown tools: {', '.join(sorted(set(dropped)))})"
        return plan

    # ------------------------------------------------------------------ offline

    def _offline_plan(self, message: str, bundle: ContextBundle) -> Plan:
        text = message.strip()
        lowered = text.lower()
        steps: list[PlanStep] = []
        answer = ""
        intent = text[:200]
        needs_research = False
        research_query = ""

        def add(tool: str, params: dict[str, Any], description: str, expected: str = "") -> None:
            steps.append(
                PlanStep(
                    index=len(steps),
                    reason=description,
                    action=ActionSpec(
                        tool=tool,
                        params=params,
                        description=description,
                        expected=expected,
                        risk_floor=self.registry.get(tool).risk_floor if self.registry.get(tool) else None,
                        reversible=tool in REVERSIBLE_TOOLS,
                    ),
                )
            )

        remember_match = re.search(r"(?i)\bremember (?:that )?(.+)$", text)
        if remember_match:
            content = remember_match.group(1).strip()
            add("remember", {"content": content, "type": "fact", "importance": 0.7}, f"Remember: {content[:80]}")
            answer = f"I'll remember that: {content}"

        if re.search(r"(?i)\b(every|daily|weekly|each (day|week|morning|evening)|remind me)\b", text):
            if re.search(r"(?i)\bremind me\b", text):
                when = re.search(r"(?i)remind me (?:in |at |on )?([^,]+?)(?: to |,|$)", text)
                add(
                    "reminder_create",
                    {"text": text[:160], "when": (when.group(1).strip() if when else "in 1h")},
                    "Create the reminder",
                    "reminder scheduled",
                )
            else:
                add(
                    "automation_create",
                    {
                        "name": text[:60] or "Scheduled task",
                        "schedule": self._cron_hint(lowered),
                        "task": text,
                    },
                    "Create the recurring automation",
                    "automation scheduled with a next run time",
                )
            answer = answer or "I've set that up and it will show under Automations."

        if re.search(r"(?i)\b(open|launch|start)\b.*\b(vscode|vs code|code|browser|chrome|edge|explorer|notepad|terminal)\b", lowered) or re.search(
            r"(?i)\bopen\b.*\b(project|folder|repo)\b", lowered
        ):
            app_match = re.search(
                r"(?i)\b(vscode|vs code|code|chrome|edge|browser|explorer|notepad|terminal)\b", lowered
            )
            app = app_match.group(1) if app_match else "explorer"
            if app in {"vs code", "code"}:
                app = "vscode"
            target_match = re.search(r"(?i)(?:project|folder|repo|directory)\s+([\w\-\./\\~ ]+)", text)
            params: dict[str, Any] = {"app": app}
            if target_match:
                candidate = target_match.group(1).strip()
                # The match is greedy over prose: "folder called X in my home directory. Inside it"
                # is a sentence fragment, not a path. Only pass a target that can plausibly be one.
                if _looks_like_path(candidate):
                    params["target"] = candidate
            add("app_open", params, f"Open {app}", f"{app} appears in the process list")

        if re.search(r"(?i)\b(clean|organi[sz]e|tidy)\b.*\b(downloads?|folder|files)\b", lowered):
            downloads = "~/Downloads"
            add("fs_list", {"path": downloads}, "Inspect what is in the folder", "file listing")
            for label, extensions in (
                ("Screenshots", ("png", "jpg", "jpeg")),
                ("Documents", ("pdf", "docx", "txt", "md")),
                ("Installers", ("exe", "msi", "dmg")),
            ):
                add(
                    "fs_move",
                    {
                        "sources": [f"{downloads}/*.{extension}" for extension in extensions],
                        "destination": f"{downloads}/Archive/{label}",
                        "on_conflict": "rename",
                    },
                    f"Move {label.lower()} into Archive/{label}",
                    "files present under Archive",
                )
            answer = (
                "I planned this as an organising pass: screenshots, documents and installers move into "
                "Archive subfolders. I did not plan any deletion — tell me if you actually want files "
                "removed and I will ask for approval first."
            )

        if not steps:
            move_match = re.search(r"(?i)\bmove\s+(\S+)\s+(?:to|into|in)\s+(\S+)", text)
            if move_match:
                source, destination = move_match.group(1).strip('\"\''), move_match.group(2).strip('\"\'')
                # Only when both ends are written as paths: "remember I move to Paris" is prose.
                if any(char in source for char in "/\\~") and any(
                    char in destination for char in "/\\~"
                ):
                    add(
                        "fs_move",
                        {"source": source, "destination": destination, "on_conflict": "rename"},
                        f"Move {source} into {destination}",
                        "the file exists at the destination",
                    )
                    answer = (
                        "I'll move that. If the destination is outside my usual workspace I'll stop "
                        "and ask you first — once, or always."
                    )

        if re.search(r"(?i)\b(delete|remove|erase|wipe)\b", lowered) and re.search(
            r"(?i)\b(files?|folder|downloads?|everything|all)\b", lowered
        ):
            target = "~/Downloads" if "download" in lowered else "~/"
            add(
                "fs_list",
                {"path": target},
                f"Inspect {target} before touching anything",
                "file listing for review",
            )
            add(
                "fs_delete",
                {
                    "path": f"{target}/*",
                    "recursive": True,
                    "reason": "user asked to remove these files",
                },
                f"Delete the files in {target}",
                "the targets no longer exist",
            )
            answer = (
                "Deleting is destructive, so this plan stops for your approval first. "
                "I will show you exactly what would be removed."
            )

        if not steps and re.search(r"(?i)\b(create|write|make|save)\b.*\b(file|todo|readme|\.md|\.txt|\.py)\b", lowered):
            name_match = re.search(r"(?i)([\w\-]+\.(?:md|txt|py|json|ts|js|csv))", text)
            filename = name_match.group(1) if name_match else "TODO.md"
            body = f"# {filename.rsplit('.', 1)[0]}\n\nCreated by Dobot.\n"
            add(
                "fs_write",
                {"path": f"~/{filename}", "content": body, "overwrite": False},
                f"Create {filename}",
                "file exists with matching content",
            )

        research_words = re.search(
            r"(?i)\b(research|latest|recent|news|compare|find (?:recent|papers)|look ?up|search|sources?)\b",
            lowered,
        )
        if research_words:
            topic = bundle.screen.ocr_text[:300] if (bundle.screen and bundle.screen.ocr_text) else text
            add(
                "tavily_research",
                {"topic": topic[:300], "max_results": 6},
                f"Research: {topic[:80]}",
                "sources with URLs",
            )
            needs_research = True
            research_query = topic[:300]

        if not steps and not answer:
            if bundle.screen and bundle.screen.ocr_text:
                answer = (
                    "Offline mode: Nemotron is unreachable, so I can only echo the text I captured from "
                    f"your screen:\n\n{bundle.screen.ocr_text[:1200]}"
                )
            else:
                answer = (
                    "Offline mode: Nemotron is unreachable, so I cannot reason about this request yet. "
                    "Check NEBIUS_API_KEY and the endpoint, then try again."
                )

        return Plan(
            intent=intent,
            summary=intent,
            reasoning="rule-based plan (Nemotron unavailable)",
            steps=steps,
            answer=answer,
            needs_research=needs_research,
            research_query=research_query,
            confidence=0.3,
            degraded=True,
        )

    @staticmethod
    def _cron_hint(lowered: str) -> str:
        """Map simple schedule phrases onto cron so offline planning is still useful."""
        if "friday" in lowered or "weekly" in lowered or "every friday" in lowered:
            return "0 18 * * 5"
        if "morning" in lowered:
            return "0 8 * * *"
        if "evening" in lowered or "night" in lowered:
            return "0 20 * * *"
        if "daily" in lowered or "every day" in lowered:
            return "0 9 * * *"
        return "0 9 * * 1"
