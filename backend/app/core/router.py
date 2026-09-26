"""Model router.

Nemotron Ultra is the right model for planning and research, and the wrong model for answering
"what's the date". The router picks a tier from the request's shape so trivial turns stay fast and
cheap without sacrificing the deep ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas import ModelTier

RESEARCH_HINTS = re.compile(
    r"(?i)\b(research|latest|recent|news|current|today|papers?|arxiv|compare|comparison|"
    r"benchmark|state of the art|sources?|cite|citations?|look ?up|search|find out|"
    r"what happened|release[ds]?)\b"
)
PLANNING_HINTS = re.compile(
    r"(?i)\b(plan|schedule|every|daily|weekly|automate|automation|workflow|organize|organise|"
    r"clean|refactor|migrate|build|implement|multi[- ]step|break ?down|strategy|delegate)\b"
)
ACTION_HINTS = re.compile(
    r"(?i)\b(open|close|move|rename|create|write|save|delete|remove|install|run|execute|launch|"
    r"send|submit|download|upload|summarize|summarise|explain|translate|debug|fix|remember)\b"
)
SIMPLE_HINTS = re.compile(
    r"(?i)^\s*(hi|hello|hey|thanks|thank you|ok|okay|cool|nice|yes|no|what time|what.?s up)\b"
)


@dataclass
class RouteDecision:
    tier: ModelTier
    reason: str
    category: str
    requires_tools: bool = False
    requires_research: bool = False


class ModelRouter:
    """Heuristic tiering. Deliberately explainable: the reason string is shown in debug events."""

    def __init__(self, *, long_context_chars: int = 4000) -> None:
        self.long_context_chars = long_context_chars

    def route(
        self,
        message: str,
        *,
        context_chars: int = 0,
        has_screen: bool = False,
        explicit_research: bool = False,
        is_automation: bool = False,
        message_count: int = 1,
    ) -> RouteDecision:
        text = message.strip()
        length = len(text)

        if explicit_research or RESEARCH_HINTS.search(text):
            return RouteDecision(
                tier=ModelTier.ULTRA,
                category="research",
                reason="request needs current/verified external information",
                requires_tools=True,
                requires_research=True,
            )
        if is_automation or PLANNING_HINTS.search(text) or length > 400:
            return RouteDecision(
                tier=ModelTier.ULTRA,
                category="planning",
                reason="request needs multi-step planning",
                requires_tools=True,
            )
        if SIMPLE_HINTS.match(text) and length < 40 and not has_screen:
            return RouteDecision(
                tier=ModelTier.LIGHT,
                category="simple",
                reason="short conversational turn with no tool requirement",
            )
        if has_screen or ACTION_HINTS.search(text) or context_chars > self.long_context_chars:
            return RouteDecision(
                tier=ModelTier.SUPER,
                category="normal",
                reason="single-step task or screen-grounded question",
                requires_tools=bool(ACTION_HINTS.search(text)),
            )
        return RouteDecision(
            tier=ModelTier.SUPER,
            category="normal",
            reason="default balanced tier",
            requires_tools=False,
        )

    @staticmethod
    def describe(tier: ModelTier) -> str:
        return {
            ModelTier.LIGHT: "lightweight model — fast, no tool planning",
            ModelTier.SUPER: "Nemotron Super — balanced reasoning",
            ModelTier.ULTRA: "Nemotron 3 Ultra — deep planning, research, synthesis",
        }[tier]
