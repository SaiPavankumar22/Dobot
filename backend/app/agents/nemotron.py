"""Nemotron 3 Ultra via Nebius Token Factory — Dobot's reasoning and planning model.

Served over the OpenAI-compatible chat completions API. When no key is configured (or the endpoint is
unreachable) Dobot degrades to a deterministic responder so the pipeline stays demonstrable, and every
response records ``degraded`` so the UI can say so plainly.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.core.cost import record_usage
from app.logging_setup import get_logger
from app.observability.langsmith import get_tracer
from app.schemas import ModelTier

logger = get_logger(__name__)

DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 2048


@dataclass
class ModelResponse:
    content: str
    model: str
    tier: ModelTier = ModelTier.ULTRA
    degraded: bool = False
    latency_ms: int = 0
    usage: dict[str, Any] = field(default_factory=dict)

    def json(self) -> dict[str, Any] | None:
        """Parse the response as JSON, tolerating fenced code blocks."""
        text = self.content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None


class NemotronClient:
    """Thin client over the Nebius OpenAI-compatible endpoint."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None
        self._reachable: bool | None = None

    @property
    def available(self) -> bool:
        return self.settings.has_nebius

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.nebius_base_url.rstrip("/"),
                headers={
                    "Authorization": f"Bearer {self.settings.nebius_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(180.0, connect=15.0),
            )
        return self._client

    def model_for(self, tier: ModelTier) -> str:
        return {
            ModelTier.LIGHT: self.settings.nemotron_light_model,
            ModelTier.SUPER: self.settings.nemotron_super_model,
            ModelTier.ULTRA: self.settings.nemotron_model,
        }.get(tier, self.settings.nemotron_model)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tier: ModelTier = ModelTier.ULTRA,
        model: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        json_mode: bool = False,
        thinking: bool | None = None,
    ) -> ModelResponse:
        target = model or self.model_for(tier)
        payload: dict[str, Any] = {
            "model": target,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # Nemotron 3's thinking trace is the dominant latency cost — often thousands of tokens before
        # the first word of the answer. Callers decide: simple turns and answer composition pass
        # thinking=False, deep planning keeps the default. The switch is per request, so nothing
        # about the deployment has to change.
        effective_thinking = self.settings.nemotron_always_think or (thinking is not False)
        if not effective_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        # Traced when LangSmith is configured; a null span otherwise. The call's behaviour is
        # identical either way — observability never becomes load-bearing.
        with get_tracer().span("nemotron.chat", model=target, tier=tier.value) as span:
            response = await self._http().post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            message = (data.get("choices") or [{}])[0].get("message", {})
            content = message.get("content")
            if isinstance(content, list):  # some providers return content parts
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            if not content:
                # Reasoning models sometimes emit only a reasoning field; keep whatever we got.
                content = message.get("reasoning_content") or ""
            usage = data.get("usage") or {}
            span.record(
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            return ModelResponse(
                content=str(content or ""),
                model=data.get("model", target),
                tier=tier,
                latency_ms=int((time.perf_counter() - started) * 1000),
                usage=usage,
            )

    async def health(self) -> bool:
        if not self.available:
            self._reachable = False
            return False
        if self._reachable is not None:
            return self._reachable
        try:
            response = await self._http().get("/models", timeout=httpx.Timeout(10.0))
            self._reachable = response.status_code < 400
        except Exception as exc:  # noqa: BLE001
            logger.warning("nebius endpoint unreachable (%s)", exc)
            self._reachable = False
        return self._reachable

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def reset_http(self) -> None:
        """Drop the cached HTTP client so the next call re-reads credentials."""
        self._client = None


OFFLINE_NOTICE = "Nemotron is unreachable, so this ran in Dobot's limited offline mode."


class Reasoner:
    """Reasoning facade: Nemotron when available, deterministic fallback otherwise."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = NemotronClient(self.settings)
        self._last_degraded = False

    @property
    def available(self) -> bool:
        return self.client.available

    @property
    def last_degraded(self) -> bool:
        return self._last_degraded

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tier: ModelTier = ModelTier.ULTRA,
        json_mode: bool = False,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        thinking: bool | None = None,
    ) -> ModelResponse:
        if not self.available:
            self._last_degraded = True
            record_usage(model="offline", tier=tier.value, usage={}, degraded=True, settings=self.settings)
            return ModelResponse(
                content="",
                model="offline",
                tier=tier,
                degraded=True,
            )
        try:
            response = await self.client.chat(
                messages,
                tier=tier,
                json_mode=json_mode,
                max_tokens=max_tokens,
                temperature=temperature,
                thinking=thinking,
            )
            self._last_degraded = False
            record_usage(
                model=response.model,
                tier=tier.value,
                usage=response.usage,
                latency_ms=response.latency_ms,
                settings=self.settings,
            )
            return response
        except Exception as exc:  # noqa: BLE001 - any transport/auth failure degrades
            logger.warning("nemotron call failed (%s); degrading", exc)
            self._last_degraded = True
            record_usage(model="offline", tier=tier.value, usage={}, degraded=True, settings=self.settings)
            return ModelResponse(content="", model="offline", tier=tier, degraded=True)

    async def text(
        self,
        prompt: str,
        *,
        system: str = "",
        tier: ModelTier = ModelTier.ULTRA,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        thinking: bool | None = None,
    ) -> ModelResponse:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return await self.chat(messages, tier=tier, max_tokens=max_tokens, thinking=thinking)

    async def health(self) -> bool:
        return await self.client.health()

    async def aclose(self) -> None:
        await self.client.aclose()

    def reset_http(self) -> None:
        self.client.reset_http()


def build_reasoner(settings: Settings | None = None) -> Reasoner:
    return Reasoner(settings)
