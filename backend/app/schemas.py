"""Shared domain models.

These types are the contract between the API layer, the orchestrator, the decision engine, the memory
service and the desktop client. Keep them serialisation-friendly: every model must round-trip
through JSON unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- enums


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}[self.value]


class Verdict(str, Enum):
    ALLOW = "ALLOW"
    APPROVAL = "APPROVAL"
    BLOCK = "BLOCK"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_USER = "WAITING_USER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"
    WAITING_APPROVAL = "WAITING_APPROVAL"


class MemoryType(str, Enum):
    PREFERENCE = "preference"
    PROJECT = "project"
    PERSON = "person"
    FACT = "fact"
    WORKFLOW = "workflow"
    EPISODE = "episode"
    TASK = "task"
    #: Typed knowledge (the shape of a life, not a transcript): organisations, ideas, research findings,
    #: and the working history of what has already been done.
    COMPANY = "company"
    IDEA = "idea"
    RESEARCH = "research"
    WORK_HISTORY = "work_history"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EDITED = "EDITED"
    EXPIRED = "EXPIRED"


class DotStatus(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    EXECUTING = "EXECUTING"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    KILLED = "KILLED"


class ModelTier(str, Enum):
    LIGHT = "light"
    SUPER = "super"
    ULTRA = "ultra"


class AutomationKind(str, Enum):
    CRON = "cron"
    REMINDER = "reminder"


# ----------------------------------------------------------------------- primitives


class Region(BaseModel):
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    monitor: int = 0


class ActionSpec(BaseModel):
    """A concrete thing an agent wants to do. Everything else orbits this type."""

    model_config = ConfigDict(extra="allow")

    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    risk_floor: RiskLevel | None = None
    expected: str = ""
    #: ``None`` means unknown — the classifier only escalates when reversibility is explicitly False.
    reversible: bool | None = None


class PlanStep(BaseModel):
    id: str = Field(default_factory=lambda: new_id("step"))
    index: int = 0
    reason: str = ""
    action: ActionSpec
    status: StepStatus = StepStatus.PENDING
    risk: RiskLevel | None = None
    verdict: Verdict | None = None
    decision_reason: str = ""
    result: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: int | None = None
    approval_id: str | None = None


class Plan(BaseModel):
    intent: str = ""
    summary: str = ""
    reasoning: str = ""
    steps: list[PlanStep] = Field(default_factory=list)
    answer: str = ""
    needs_research: bool = False
    research_query: str = ""
    memory_writes: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    model_tier: ModelTier = ModelTier.ULTRA
    used_model: str = ""
    degraded: bool = False
    raw: str = ""


class Decision(BaseModel):
    verdict: Verdict
    risk: RiskLevel
    step_id: str | None = None
    reasons: list[str] = Field(default_factory=list)
    policies: list[str] = Field(default_factory=list)
    jev_notes: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    blocked_reason: str = ""


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class VerificationResult(BaseModel):
    verified: bool = False
    checks: list[CheckResult] = Field(default_factory=list)
    notes: str = ""
    skipped_reason: str = ""

    @property
    def summary(self) -> str:
        return "; ".join(f"{c.name}: {'ok' if c.passed else 'FAIL'}" for c in self.checks)


class ExecutionResult(BaseModel):
    ok: bool
    tool: str
    output: Any = None
    error: str | None = None
    duration_ms: int = 0
    side_effects: list[str] = Field(default_factory=list)
    raw_output: str = ""


class ScreenContext(BaseModel):
    application: str = ""
    window_title: str = ""
    selected_region: Region | None = None
    ocr_text: str = ""
    image_available: bool = False
    image_ref: str | None = None
    captured_at: datetime = Field(default_factory=utcnow)
    ocr_engine: str = ""


class MemoryHit(BaseModel):
    memory: MemoryRecord
    score: float = 0.0
    components: dict[str, float] = Field(default_factory=dict)


class EnvironmentSnapshot(BaseModel):
    platform: str = ""
    cwd: str = ""
    foreground_app: str = ""
    running_apps: list[str] = Field(default_factory=list)
    sandbox_provider: str = "local"
    agent_runtime: str = "local"
    screen_capture: str = "on_demand"
    shadow_mode: bool = False


class AttachmentBlock(BaseModel):
    """One accepted attachment, as the reasoning model will see it.

    An image is represented by what the vision model read out of it (plus the ephemeral file it was
    saved to); a text file is represented by its own content, capped and marked when cut.
    """

    name: str = ""
    kind: str = "text"  # image | text
    mime: str = ""
    bytes: int = 0
    text: str = ""
    image_ref: str | None = None
    engine: str = ""
    truncated: bool = False


class ContextBundle(BaseModel):
    task_id: str = ""
    user_message: str = ""
    screen: ScreenContext | None = None
    attachments: list[AttachmentBlock] = Field(default_factory=list)
    memories: list[MemoryHit] = Field(default_factory=list)
    open_tasks: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    environment: EnvironmentSnapshot = Field(default_factory=EnvironmentSnapshot)
    recent_activity: list[str] = Field(default_factory=list)
    #: Standing rules from the user's GENOME.md. Rendered first and never trimmed: a hard rule that
    #: fell off the end of a prompt budget is not a hard rule.
    standing_rules: list[str] = Field(default_factory=list)
    #: Long-standing facts injected without a search, so identity never has to win a retrieval contest.
    canonical_facts: list[str] = Field(default_factory=list)

    def as_prompt_context(self, max_chars: int = 6000) -> str:
        """Render the bundle into a compact, budgeted block for the model prompt."""
        lines: list[str] = []
        if self.standing_rules:
            lines.append("[STANDING RULES - from the user's GENOME.md, enforced in code]")
            lines.extend(f"- {rule}" for rule in self.standing_rules[:12])
        if self.canonical_facts:
            lines.append("[LONG-STANDING FACTS - treat as known, do not search for these]")
            lines.extend(f"- {fact}" for fact in self.canonical_facts)
        if self.attachments:
            # Placed before everything else the user did not write: an explicit attachment is the
            # most deliberate thing in the message, and it must never be the thing that got cut.
            lines.append("[ATTACHMENTS - supplied by the user with this message]")
            for block in self.attachments:
                size = f"{block.bytes / 1024:.1f} KB" if block.bytes >= 1024 else f"{block.bytes} B"
                if block.kind == "image":
                    described = block.text or "the vision model returned no description"
                    lines.append(f"- IMAGE {block.name} ({block.mime or 'image'}, {size}): {described}")
                else:
                    cut = " - TRUNCATED, the rest was not sent" if block.truncated else ""
                    lines.append(f"--- FILE {block.name} ({block.mime or 'text'}, {size}{cut}) ---")
                    lines.append(block.text)
        if self.screen and (self.screen.ocr_text or self.screen.application):
            lines.append("[SCREEN]")
            if self.screen.application or self.screen.window_title:
                lines.append(f"app: {self.screen.application} — {self.screen.window_title}")
            if self.screen.ocr_text:
                lines.append(f"selected text:\n{self.screen.ocr_text}")
        if self.memories:
            lines.append("[MEMORY]")
            for hit in self.memories:
                lines.append(f"- ({hit.memory.type.value}) {hit.memory.content}")
        if self.open_tasks:
            lines.append("[OPEN TASKS]")
            for task in self.open_tasks:
                lines.append(f"- {task.get('title')} [{task.get('status')}]")
        if self.skills:
            lines.append("[SKILLS] " + ", ".join(self.skills))
        env = self.environment
        lines.append(
            "[ENVIRONMENT] "
            f"platform={env.platform} agent_runtime={env.agent_runtime} "
            f"sandbox={env.sandbox_provider} cwd={env.cwd}"
            + (" shadow_mode=true" if env.shadow_mode else "")
        )
        text = "\n".join(lines)
        return text[:max_chars]


# -------------------------------------------------------------------------- records


class TaskStepRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("step"))
    sequence: int = 0
    description: str = ""
    status: StepStatus = StepStatus.PENDING
    tool: str = ""
    action: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    error: str | None = None


class TaskRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("task"))
    user_id: str = "local"
    title: str = ""
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    priority: str = "normal"
    source: str = "chat"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    deadline: datetime | None = None
    progress: int = 0
    steps: list[TaskStepRecord] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    answer: str = ""
    sources: list[dict[str, Any]] = Field(default_factory=list)
    background: bool = False


class MemoryRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mem"))
    user_id: str = "local"
    type: MemoryType = MemoryType.FACT
    content: str = ""
    importance: float = 0.5
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    source: str = "chat"
    embedding_id: str | None = None
    #: Recall bookkeeping, used by the decay model: use reinforces a memory, neglect fades it.
    last_access_at: datetime | None = None
    access_count: int = 0


class AutomationRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("auto"))
    user_id: str = "local"
    name: str = ""
    schedule: str = "0 18 * * 5"
    prompt: str = ""
    status: str = "active"
    kind: AutomationKind = AutomationKind.CRON
    created_at: datetime = Field(default_factory=utcnow)
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    run_count: int = 0


class ApprovalRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("appr"))
    task_id: str = ""
    step_id: str = ""
    action: str = ""
    risk: RiskLevel = RiskLevel.HIGH
    description: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    preview: list[str] = Field(default_factory=list)
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None
    decision_note: str = ""


class ActivityRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("log"))
    task_id: str = ""
    event_type: str = ""
    message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utcnow)


class SkillRecord(BaseModel):
    name: str
    description: str = ""
    trigger: str = ""
    required_tools: list[str] = Field(default_factory=list)
    safety: str = ""
    workflow: list[dict[str, Any]] = Field(default_factory=list)
    path: str = ""
    body: str = ""
    #: Result of the pre-activation scan: clean | warn | blocked | allowlisted | off.
    scan_status: str = "clean"
    scan_findings: list[dict[str, Any]] = Field(default_factory=list)


# ------------------------------------------------------------------------- requests


class ContextAttachment(BaseModel):
    """A file or image the user attached to a message (base64; limits enforced by the API)."""

    name: str = "attachment"
    mime: str = ""
    data: str = ""  # base64, optionally a full data: URL


class ContextRequest(BaseModel):
    screen: bool = False
    region: Region | None = None
    image: str | None = None
    task_id: str | None = None
    attachments: list[ContextAttachment] = Field(default_factory=list)


class ChatRequest(BaseModel):
    message: str
    context: ContextRequest = Field(default_factory=ContextRequest)
    shadow: bool | None = None
    background: bool = False
    source: str = "chat"
    #: ask = answer and plan only; assist = confirm anything beyond a read; agent = the spec default.
    mode: str | None = None


class ChatResponse(BaseModel):
    task_id: str
    status: TaskStatus
    answer: str = ""
    plan: Plan | None = None
    decisions: list[Decision] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    verifications: list[VerificationResult] = Field(default_factory=list)
    approvals: list[ApprovalRecord] = Field(default_factory=list)
    timeline: list[ActivityRecord] = Field(default_factory=list)
    error: str | None = None
    providers: dict[str, str] = Field(default_factory=dict)


class ScreenAnalyzeRequest(BaseModel):
    image: str | None = None
    region: Region | None = None
    question: str = "Explain this."
    application: str = ""
    window_title: str = ""
    with_reasoning: bool = True


class ScreenAnalyzeResponse(BaseModel):
    answer: str = ""
    context_id: str = ""
    screen: ScreenContext
    task_id: str | None = None


class ResearchRequest(BaseModel):
    query: str
    depth: Literal["quick", "deep"] = "quick"
    max_sources: int = 6
    synthesize: bool = True


class ResearchSource(BaseModel):
    title: str = ""
    url: str = ""
    snippet: str = ""
    score: float = 0.0


class ResearchResponse(BaseModel):
    status: str = "completed"
    summary: str = ""
    sources: list[ResearchSource] = Field(default_factory=list)
    sub_queries: list[str] = Field(default_factory=list)
    degraded: bool = False


class TaskCreateRequest(BaseModel):
    title: str
    description: str = ""
    priority: str = "normal"
    deadline: datetime | None = None


class AutomationCreateRequest(BaseModel):
    name: str
    schedule: str = "0 18 * * 5"
    task: str = ""
    kind: AutomationKind = AutomationKind.CRON


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject", "edit"]
    note: str = ""
    edits: dict[str, Any] | None = None


class MemoryCreateRequest(BaseModel):
    content: str
    type: MemoryType = MemoryType.FACT
    importance: float = 0.6
    tags: list[str] = Field(default_factory=list)


class ErrorBody(BaseModel):
    code: str
    message: str
    recoverable: bool = False


class ErrorResponse(BaseModel):
    success: bool = False
    error: ErrorBody


MemoryHit.model_rebuild()
