"""Laya — the open judgement model behind JEV.

`convaiinnovations/laya` is a multilingual, non-autoregressive System 1 decision model: give it a
state and typed questions and it returns typed answers with calibrated probabilities in a single
forward pass (~33 ms on GPU). It never generates text, so there is nothing to parse and nothing to
hallucinate — exactly the property a *judgement* layer needs. The old JEV heuristic signals (blast
radius, sensitivity, injection markers) stay; Laya adds a probabilistic second opinion on top.

Three integration paths, chosen automatically at startup:

1. ``LAYA_SERVER_URL`` — the JEV-compatible HTTP server (`laya-serve`, POST /v1/systemone). Existing
   TypeSafe-style clients work by changing the base URL, and the server can run on another machine or
   a GPU box, which is the right shape for a laptop where every GB of VRAM is spoken for.
2. In-process ``laya`` python package — ``pip install laya``; the checkpoint loads into the backend's
   own process (~808 MB on disk for the English checkpoint).
3. Neither — JEV keeps its deterministic heuristics and honestly reports ``heuristic`` as its judge.
   Nothing else changes: the same signals fire, the same escalations happen.

Laya can only ever *escalate* risk, exactly like the rest of JEV. A heuristic BLOCK outranks
everything; the decision engine still enforces its own floor.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

_TIMEOUT = httpx.Timeout(10.0, connect=3.0)


@dataclass
class LayaOpinion:
    """One judgement pass. Every field is designed to be shown in the UI as-is."""

    available: bool = False
    engine: str = "heuristic"
    model: str = ""
    score: float = 0.0
    urgency: float = 0.0
    sensitive: float = 0.0
    latency_ms: int = 0
    detail: str = ""
    error: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "engine": self.engine,
            "model": self.model,
            "score": self.score,
            "urgency": self.urgency,
            "sensitive": self.sensitive,
            "latency_ms": self.latency_ms,
            "detail": self.detail,
            "error": self.error,
            "notes": list(self.notes),
        }


def _clamp(value: Any) -> float:
    """Coerce whatever came back into a 0..1 probability without trusting it."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


class LayaClient:
    """Thin client over the laya-serve HTTP API, with in-process fallback when installed."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._http: httpx.AsyncClient | None = None
        self._agent: Any = None  # in-process Router, only when LAYA_INPROCESS=1
        self._probed = False

    # ------------------------------------------------------------------ capability

    @property
    def mode(self) -> str:
        """Which engine this client will actually use: server | inprocess | heuristic."""
        if (self.settings.laya_server_url or "").strip():
            return "server"
        if self.settings.laya_inprocess and self._agent is not None:
            return "inprocess"
        return "heuristic"

    async def _probe_inprocess(self) -> bool:
        """Try to build the in-process Router once. Never raises."""
        if self._probed:
            return self._agent is not None
        self._probed = True
        if not self.settings.laya_inprocess:
            return False
        try:
            from laya import Router  # type: ignore[import-not-found]

            self._agent = Router()  # lazy: checkpoints build on first predict
            logger.info("laya: in-process router loaded")
            return True
        except Exception as exc:  # noqa: BLE001 - ImportError or a bad download
            logger.info("laya: in-process model unavailable (%s)", exc)
            self._agent = None
            return False

    # ------------------------------------------------------------------ transport

    def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            headers: dict[str, str] = {}
            token = (self.settings.laya_api_key or "").strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            self._http = httpx.AsyncClient(
                base_url=self.settings.laya_server_url.rstrip("/"),
                headers=headers,
                timeout=_TIMEOUT,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def reset_http(self) -> None:
        """Drop the cached HTTP client so the next call re-reads credentials."""
        self._http = None

    # ------------------------------------------------------------------ judging

    @staticmethod
    def _questions() -> dict[str, dict[str, Any]]:
        """The judgement schema JEV asks of every action, in Laya's typed-question format."""
        return {
            "risky": {
                "type": "noul",
                "instructions": (
                    "Would letting an automated assistant perform this action on the user's computer "
                    "carry real risk of data loss, privacy harm, or irreversible damage?"
                ),
            },
            "urgent": {
                "type": "noul",
                "instructions": "Does this action have urgent or time-critical consequences?",
            },
            "sensitive": {
                "type": "noul",
                "instructions": (
                    "Does this action touch sensitive material such as credentials, personal "
                    "documents, financial records, or private communications?"
                ),
            },
        }

    async def judge(
        self,
        *,
        action_text: str,
        user_message: str,
        untrusted_content: str = "",
    ) -> LayaOpinion:
        """Ask the model to judge one action. Always returns an opinion; never raises."""
        if self.mode == "server":
            try:
                return await self._judge_server(action_text, user_message, untrusted_content)
            except Exception as exc:  # noqa: BLE001 - degrade to heuristics, never break a task
                return LayaOpinion(
                    available=False,
                    engine="server",
                    error=f"laya-serve unreachable: {exc}",
                    detail="Falling back to heuristic judgement for this action.",
                )
        if await self._probe_inprocess():
            try:
                return self._judge_inprocess(action_text, user_message, untrusted_content)
            except Exception as exc:  # noqa: BLE001
                return LayaOpinion(
                    available=False,
                    engine="inprocess",
                    error=f"in-process laya failed: {exc}",
                    detail="Falling back to heuristic judgement for this action.",
                )
        return LayaOpinion(engine="heuristic", detail="No Laya endpoint configured; heuristics only.")

    async def _judge_server(self, action_text: str, user_message: str, untrusted_content: str) -> LayaOpinion:
        state = {
            "user_request": user_message[:2000],
            "action": action_text[:2000],
        }
        if untrusted_content:
            state["untrusted_content"] = untrusted_content[:4000]
        started = time.perf_counter()
        response = await self._http_client().post(
            "/v1/systemone",
            json={"state": state, "questions": self._questions()},
        )
        response.raise_for_status()
        payload = response.json()
        latency = int((time.perf_counter() - started) * 1000)
        answers = payload.get("answers") or {}
        return self._opinion_from_answers(answers, engine="server", latency_ms=latency)

    def _judge_inprocess(self, action_text: str, user_message: str, untrusted_content: str) -> LayaOpinion:
        state = {
            "user_request": user_message[:2000],
            "action": action_text[:2000],
        }
        if untrusted_content:
            state["untrusted_content"] = untrusted_content[:4000]
        started = time.perf_counter()
        result = self._agent.predict(state, self._questions())  # type: ignore[union-attr]
        latency = int((time.perf_counter() - started) * 1000)
        answers = result.get("answers") or {}
        return self._opinion_from_answers(answers, engine="inprocess", latency_ms=latency)

    @staticmethod
    def _opinion_from_answers(answers: dict[str, Any], *, engine: str, latency_ms: int) -> LayaOpinion:
        def probability(name: str) -> float:
            answer = answers.get(name) or {}
            # noul questions answer as {"noul": probability}; accept a bare float too.
            value = answer.get("noul", answer) if isinstance(answer, dict) else answer
            return _clamp(value)

        risky = probability("risky")
        urgent = probability("urgent")
        sensitive = probability("sensitive")
        # The blended judgement score: risk dominates, sensitivity sharpens it, urgency sharpens
        # escalations (a rushed action is more dangerous than an unhurried one).
        score = max(risky, min(1.0, sensitive * 0.85 + risky * 0.15))
        notes: list[str] = []
        if risky >= 0.6:
            notes.append(f"Laya: risky action (p={risky:.2f})")
        if sensitive >= 0.6:
            notes.append(f"Laya: touches sensitive material (p={sensitive:.2f})")
        if urgent >= 0.6:
            notes.append(f"Laya: time-critical (p={urgent:.2f}) — verify before irreversible steps")
        model = ""
        if isinstance(answers.get("_routing"), dict):
            model = str(answers["_routing"].get("model", ""))
        return LayaOpinion(
            available=True,
            engine=engine,
            model=model,
            score=round(score, 3),
            urgency=round(urgent, 3),
            sensitive=round(sensitive, 3),
            latency_ms=latency_ms,
            detail=f"{engine} judgement in {latency_ms} ms",
            notes=notes,
        )

    # ------------------------------------------------------------------ health

    async def health(self) -> dict[str, Any]:
        mode = self.mode
        info: dict[str, Any] = {"mode": mode, "server_url": self.settings.laya_server_url or ""}
        if mode == "server":
            try:
                response = await self._http_client().get("/", timeout=_TIMEOUT)
                info["reachable"] = response.status_code < 500
            except Exception as exc:  # noqa: BLE001
                info["reachable"] = False
                info["error"] = str(exc)
        elif mode == "inprocess":
            info["reachable"] = await self._probe_inprocess()
        else:
            info["reachable"] = False
        return info


def build_laya_client(settings: Settings | None = None) -> LayaClient:
    return LayaClient(settings)
