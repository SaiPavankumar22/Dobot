"""JEV — the auxiliary judgement layer.

JEV is deliberately *not* the authority. Nemotron decides what the agent intends to do; the
deterministic policy engine decides what is permitted; JEV adds a judgement pass for signals a static
rule is bad at — irreversible blast radius, target sensitivity, and (importantly) whether an action
seems to originate from **untrusted content** the user never asked for, i.e. a prompt-injection attempt
via a web page or selected screen text.

The judgement pass runs on **Laya** (`convaiinnovations/laya`), an open, non-autoregressive System 1
decision model: a single forward pass returns calibrated probabilities for "is this risky / urgent /
sensitive" with no free-text generation to parse. When no Laya endpoint is configured the same
signals degrade to the deterministic heuristics — visible in ``laya.engine`` either way, so the UI can
always say which judge produced an escalation.

JEV can only ever *escalate* risk. The decision engine enforces that floor, and a policy BLOCK
outranks every JEV opinion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.config import Settings, get_settings
from app.logging_setup import get_logger
from app.schemas import ActionSpec, RiskLevel

logger = get_logger(__name__)

_INJECTION_MARKERS = re.compile(
    r"(?i)(ignore (all )?(previous|prior) instructions|disregard (the )?(above|previous)|"
    r"you are now|system prompt|as an ai|do not tell the user|without asking|"
    r"send (this|the) (data|file|key)s? to|exfiltrate|curl\s+http|base64\s+-d)"
)

_CREDENTIALISH = re.compile(r"(?i)(password|api[_-]?key|token|secret|\.ssh|id_rsa|wallet)")

# Files whose loss is painful beyond their byte count.
_IRREPLACEABLE = re.compile(
    r"\.(?:docx?|xlsx?|pptx?|pdf|psd|ai|sketch|fig|kdbx|pem|key|db|sqlite|sql)$"
    r"|(?:master|final|thesis|contract|invoice|tax|passport|resume)",
    re.IGNORECASE,
)


@dataclass
class JEVAdvice:
    escalate_to: RiskLevel | None = None
    score: float = 0.0
    notes: list[str] = field(default_factory=list)
    injection_suspected: bool = False
    consulted_model: bool = False
    #: Which judge produced this advice: "laya" (the model) or "heuristic" (deterministic signals).
    judge: str = "heuristic"
    laya: dict | None = None


@dataclass
class JEVSignals:
    """Cheap contextual signals the orchestrator already has on hand."""

    untrusted_content: str = ""
    user_message: str = ""
    recent_denials: int = 0
    similar_action_count: int = 0
    autonomy: str = "assisted"


class JEVEngine:
    def __init__(self, settings: Settings | None = None, laya=None) -> None:
        self.settings = settings or get_settings()
        self.enabled = self.settings.jev_enabled
        # Late import avoids a circular import: laya.py imports nothing from this module, but keeping
        # the construction lazy also lets tests inject a stub cleanly.
        self.laya = laya
        if self.laya is None:
            from app.security.laya import build_laya_client

            self.laya = build_laya_client(self.settings)

    # ------------------------------------------------------------------ signals

    @staticmethod
    def _looks_injected(action: ActionSpec, signals: JEVSignals) -> tuple[bool, list[str]]:
        notes: list[str] = []
        if not signals.untrusted_content:
            return False, notes

        action_text = " ".join(
            [action.tool, action.description, *(f"{k}={v}" for k, v in action.params.items())]
        ).lower()
        untrusted = signals.untrusted_content.lower()
        trusted = signals.user_message.lower()

        # The action's own target vocabulary shows up in untrusted content but not in what the user
        # actually said. That is the classic injected-instruction signature.
        tokens = [token for token in re.findall(r"[a-z]{4,}", action_text) if token not in trusted]
        overlap = sum(1 for token in set(tokens) if token in untrusted)
        marker = bool(_INJECTION_MARKERS.search(signals.untrusted_content))

        if overlap >= 2:
            notes.append("action vocabulary appears in untrusted on-screen/web content")
        if marker:
            notes.append("untrusted content contains instruction-like text")
        return (overlap >= 2 or marker), notes

    @staticmethod
    def _blast_radius(action: ActionSpec) -> tuple[float, list[str]]:
        notes: list[str] = []
        score = 0.0
        raw_count = action.params.get("count")
        try:
            count = int(raw_count) if raw_count is not None else 0
        except (TypeError, ValueError):
            count = 0
        targets = action.params.get("paths") or action.params.get("targets")
        if isinstance(targets, list):
            count = max(count, len(targets))
        if count > 50:
            score += 0.35
            notes.append(f"{count} targets in one action")
        elif count > 10:
            score += 0.2
            notes.append(f"{count} targets in one action")
        if action.reversible is False and action.tool.startswith("fs_"):
            score += 0.25
            notes.append("operation is not reversible")
        return min(score, 0.5), notes

    @staticmethod
    def _sensitivity(action: ActionSpec) -> tuple[float, list[str]]:
        notes: list[str] = []
        score = 0.0
        text = " ".join([action.description, *(str(v) for v in action.params.values())])
        if _CREDENTIALISH.search(text):
            score += 0.3
            notes.append("touches credential-like material")
        if _IRREPLACEABLE.search(text):
            score += 0.25
            notes.append("target looks irreplaceable (document/archive/key/database)")
        if any(str(value).strip() in {"~", "~/", "/", "C:\\", "C:/"} for value in action.params.values()):
            score += 0.3
            notes.append("target is a whole root directory")
        return min(score, 0.6), notes

    # ------------------------------------------------------------------ evaluation

    async def advise(
        self,
        action: ActionSpec,
        signals: JEVSignals,
        *,
        base_risk: RiskLevel,
    ) -> JEVAdvice:
        if not self.enabled:
            return JEVAdvice(notes=["JEV disabled"], score=0.0)

        score = 0.0
        notes: list[str] = []

        radius, radius_notes = self._blast_radius(action)
        sensitivity, sensitivity_notes = self._sensitivity(action)
        injected, injection_notes = self._looks_injected(action, signals)

        score += radius + sensitivity
        notes.extend(radius_notes)
        notes.extend(sensitivity_notes)
        notes.extend(injection_notes)

        if injected:
            score += 0.45
        if signals.recent_denials > 0:
            score += min(0.2, signals.recent_denials * 0.1)
            notes.append(f"user denied {signals.recent_denials} similar action(s) recently")
        # Repeating a familiar action the user has approved before is a mild trust signal.
        if signals.similar_action_count >= 3:
            score -= 0.1
            notes.append("similar actions were performed before")

        # The model judgement. Deterministic signals above describe *what the action looks like*;
        # Laya weighs what it *means* — calibrated probabilities for risk, urgency and sensitivity —
        # and its score blends into the heuristic one. Whichever judge is stronger wins, so the
        # model can only add risk signal, never cancel a heuristic warning.
        laya_opinion = None
        judge = "heuristic"
        try:
            action_text = " ".join(
                [action.tool, action.description, *(f"{k}={v}" for k, v in action.params.items())]
            )
            laya_opinion = await self.laya.judge(
                action_text=action_text,
                user_message=signals.user_message,
                untrusted_content=signals.untrusted_content,
            )
            if laya_opinion.available:
                judge = "laya"
                if laya_opinion.notes:
                    notes.extend(laya_opinion.notes)
                # The model's score and the heuristic score combine conservatively: take the max so
                # either judge alone can trigger an escalation.
                score = max(score, laya_opinion.score)
            elif laya_opinion.error:
                notes.append(f"Laya unavailable ({laya_opinion.error}); heuristic judgement used")
        except Exception as exc:  # noqa: BLE001 - judgement must never break a task
            notes.append(f"Laya judgement failed ({exc}); heuristic judgement used")

        score = max(0.0, min(1.0, score))

        escalate_to: RiskLevel | None = None
        if injected:
            escalate_to = RiskLevel.HIGH
        elif score >= 0.7:
            escalate_to = RiskLevel.CRITICAL
        elif score >= 0.45:
            escalate_to = RiskLevel.HIGH
        elif score >= 0.25:
            escalate_to = RiskLevel.MEDIUM

        # JEV never de-escalates below what the classifier already concluded.
        if escalate_to is not None and escalate_to.rank <= base_risk.rank:
            escalate_to = None

        return JEVAdvice(
            escalate_to=escalate_to,
            score=round(score, 3),
            notes=notes or ["no additional risk signals"],
            injection_suspected=injected,
            judge=judge,
            laya=laya_opinion.as_dict() if laya_opinion is not None else None,
        )


def build_jev_engine(settings: Settings | None = None, laya=None) -> JEVEngine:
    return JEVEngine(settings, laya=laya)
