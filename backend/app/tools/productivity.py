"""Productivity tools: remembering, task and reminder creation, automations, skills, outbound drafts.

These are the tools that make Dobot more than a chat window, so their verification matters: each one
reads back the record it claims to have created.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.schemas import (
    AutomationKind,
    AutomationRecord,
    CheckResult,
    ExecutionResult,
    MemoryType,
    RiskLevel,
    TaskRecord,
    TaskStatus,
    VerificationResult,
)
from app.tools.base import Tool, ToolContext


class RememberTool(Tool):
    name = "remember"
    description = (
        "Store a durable memory about the user: a preference, project, person, fact or workflow. "
        "Use when the user says 'remember…' or when a durable fact is clearly established."
    )
    risk_floor = RiskLevel.LOW
    proactive_ok = True
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact to remember, in one sentence"},
            "type": {
                "type": "string",
                "enum": [member.value for member in MemoryType],
                "default": MemoryType.FACT.value,
            },
            "importance": {"type": "number", "default": 0.6},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["content"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        content = self.require(params, "content")
        if ctx.memory is None:
            return self.fail(self.name, "memory service unavailable", started)
        try:
            memory_type = MemoryType(str(params.get("type", MemoryType.FACT.value)))
        except ValueError:
            memory_type = MemoryType.FACT
        record = await ctx.memory.remember(
            content,
            type=memory_type,
            importance=float(params.get("importance", 0.6) or 0.6),
            tags=[str(tag) for tag in (params.get("tags") or [])],
            source="agent",
        )
        return self.ok(
            self.name,
            {"memory_id": record.id, "type": record.type.value, "content": record.content},
            started,
        )

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        memory_id = str((result.output or {}).get("memory_id", ""))
        found = await ctx.memory.get(memory_id) if ctx.memory and memory_id else None
        checks = [
            CheckResult(name="memory_persisted", passed=found is not None, detail=memory_id),
            CheckResult(
                name="retrievable_by_semantics",
                passed=bool(
                    found
                    and await ctx.memory.recall(found.content, limit=1)
                )
                if ctx.memory and found
                else False,
            ),
        ]
        return VerificationResult(verified=all(check.passed for check in checks), checks=checks)


class TaskCreateTool(Tool):
    name = "task_create"
    description = "Create a tracked task so long-running work is visible in the dashboard."
    risk_floor = RiskLevel.LOW
    proactive_ok = True
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "priority": {"type": "string", "enum": ["low", "normal", "high"], "default": "normal"},
            "deadline": {"type": "string", "description": "ISO 8601 timestamp"},
        },
        "required": ["title"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        if ctx.store is None:
            return self.fail(self.name, "task store unavailable", started)
        deadline = None
        if params.get("deadline"):
            try:
                deadline = datetime.fromisoformat(str(params["deadline"]))
            except ValueError:
                deadline = None
        record = TaskRecord(
            title=self.require(params, "title"),
            description=str(params.get("description", "")),
            priority=str(params.get("priority", "normal")),
            status=TaskStatus.PENDING,
            deadline=deadline,
            source="agent",
        )
        await ctx.store.insert("tasks", record.model_dump(mode="json"))
        return self.ok(self.name, record.model_dump(mode="json"), started)

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        task_id = str((result.output or {}).get("id", ""))
        stored = await ctx.store.get("tasks", task_id) if ctx.store and task_id else None
        checks = [CheckResult(name="task_stored", passed=stored is not None, detail=task_id)]
        return VerificationResult(verified=all(check.passed for check in checks), checks=checks)


class ReminderCreateTool(Tool):
    name = "reminder_create"
    description = "Create a one-shot reminder that fires once at the given local time."
    risk_floor = RiskLevel.LOW
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to remind the user about"},
            "when": {"type": "string", "description": "ISO 8601 timestamp, or a delay like '2h'"},
        },
        "required": ["text", "when"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        if ctx.scheduler is None:
            return self.fail(self.name, "scheduler unavailable", started)
        text = self.require(params, "text")
        when = self.require(params, "when")
        try:
            record = await ctx.scheduler.create_reminder(text, when)
        except Exception as exc:  # noqa: BLE001
            return self.fail(self.name, f"invalid schedule: {exc}", started)
        return self.ok(self.name, record.model_dump(mode="json"), started)

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        automation_id = str(output.get("id", ""))
        record = await ctx.scheduler.get(automation_id) if ctx.scheduler and automation_id else None
        checks = [
            CheckResult(name="reminder_scheduled", passed=record is not None, detail=automation_id),
            CheckResult(
                name="next_run_computed",
                passed=bool(record and record.next_run_at),
                detail=str(record.next_run_at if record else ""),
            ),
        ]
        return VerificationResult(verified=all(check.passed for check in checks), checks=checks)


class AutomationCreateTool(Tool):
    name = "automation_create"
    description = (
        "Create a recurring automation from a cron expression, e.g. '0 18 * * 5' for Fridays at 6pm. "
        "Use when the user asks for something to happen on a schedule."
    )
    risk_floor = RiskLevel.MEDIUM
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "schedule": {"type": "string", "description": "Five-field cron expression"},
            "task": {"type": "string", "description": "The instruction Dobot should run each time"},
        },
        "required": ["name", "schedule", "task"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        if ctx.scheduler is None:
            return self.fail(self.name, "scheduler unavailable", started)
        try:
            record = await ctx.scheduler.create_automation(
                name=self.require(params, "name"),
                schedule=self.require(params, "schedule"),
                prompt=self.require(params, "task"),
            )
        except Exception as exc:  # noqa: BLE001
            return self.fail(self.name, f"invalid cron expression: {exc}", started)
        return self.ok(self.name, record.model_dump(mode="json"), started)

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        automation_id = str(output.get("id", ""))
        record: AutomationRecord | None = (
            await ctx.scheduler.get(automation_id) if ctx.scheduler and automation_id else None
        )
        checks = [
            CheckResult(name="automation_stored", passed=record is not None, detail=automation_id),
            CheckResult(
                name="next_run_computed",
                passed=bool(record and record.next_run_at),
                detail=str(record.next_run_at if record else ""),
            ),
            CheckResult(
                name="status_active",
                passed=bool(record and record.status == "active" and record.kind is AutomationKind.CRON),
            ),
        ]
        return VerificationResult(verified=all(check.passed for check in checks), checks=checks)

    async def preview(self, params: dict[str, Any], ctx: ToolContext) -> list[str]:
        return [
            f"Create recurring automation: {params.get('name')}",
            f"Schedule: {params.get('schedule')}",
            f"Runs: {params.get('task')}",
        ]


class SkillRunTool(Tool):
    name = "skill_run"
    description = (
        "Run a saved Dobot skill (a reusable workflow) by name. The skill's workflow is loaded so its "
        "steps can be planned and executed under the same safety checks."
    )
    risk_floor = RiskLevel.MEDIUM
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "inputs": {"type": "object"},
        },
        "required": ["name"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        if ctx.skills is None:
            return self.fail(self.name, "skill library unavailable", started)
        name = self.require(params, "name")
        skill = ctx.skills.get(name)
        if skill is None:
            available = ", ".join(ctx.skills.names()) or "none"
            return self.fail(self.name, f"unknown skill '{name}' (available: {available})", started)
        return self.ok(
            self.name,
            {
                "skill": skill.name,
                "instruction": skill.body[:4000],
                "workflow": skill.workflow,
                "inputs": params.get("inputs") or {},
                "replan": True,
            },
            started,
        )

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        checks = [
            CheckResult(name="skill_found", passed=bool(output.get("skill")), detail=str(output.get("skill", ""))),
            CheckResult(
                name="workflow_loaded",
                passed=bool(output.get("workflow")) or bool(output.get("instruction")),
            ),
        ]
        return VerificationResult(verified=all(check.passed for check in checks), checks=checks)


class MessageSendTool(Tool):
    name = "message_send"
    description = (
        "Prepare and (when an outbound provider is configured) send a message or email on the user's "
        "behalf. Always requires approval; without a provider Dobot produces a draft and does not send."
    )
    risk_floor = RiskLevel.HIGH
    parameters = {
        "type": "object",
        "properties": {
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "channel": {"type": "string", "enum": ["email", "chat"], "default": "email"},
        },
        "required": ["to", "body"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        recipient = self.require(params, "to")
        body = self.require(params, "body")
        subject = str(params.get("subject", "")).strip()
        drafts = ctx.settings.state_dir / "drafts"
        drafts.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        path = drafts / f"draft_{stamp}.md"
        path.write_text(
            f"To: {recipient}\nSubject: {subject}\n\n{body}\n", encoding="utf-8"
        )
        # There is no outbound provider in the MVP: say so rather than claiming delivery.
        return self.ok(
            self.name,
            {
                "draft_path": str(path),
                "to": recipient,
                "subject": subject,
                "sent": False,
                "notice": "No outbound messaging provider is configured; the message was saved as a draft.",
            },
            started,
            side_effects=[str(path)],
        )

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        path = Path(str(output.get("draft_path", "")))
        checks = [
            CheckResult(name="draft_written", passed=path.exists(), detail=str(path)),
            CheckResult(
                name="actually_sent",
                passed=bool(output.get("sent")),
                detail="no provider configured",
            ),
        ]
        return VerificationResult(
            verified=path.exists() and bool(output.get("sent")),
            checks=checks,
            skipped_reason="" if output.get("sent") else "message was drafted, not sent",
        )

    async def preview(self, params: dict[str, Any], ctx: ToolContext) -> list[str]:
        return [
            f"Send to: {params.get('to')}",
            f"Subject: {params.get('subject', '(none)')}",
            "Body preview: " + str(params.get("body", ""))[:240].replace("\n", " "),
        ]
