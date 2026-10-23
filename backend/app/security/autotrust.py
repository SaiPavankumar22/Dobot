"""AutoTrust — the hosted decision model behind JEV (Novita `autotrust/JEV-27B-VL`).

JEV-27B-VL is not a chat model: a request submits one piece of content (`state`) and one question
(`question`), and the answer is a *probability distribution over options* — `noul` (yes/no),
`score` (0–5) or `choice` (a named list). There is no free text to parse and nothing to
hallucinate, which is exactly the property a judgement layer needs. Without thinking mode the
upstream answers in ~0.2 s and only input tokens are billed, so asking one question per proposed
action is affordable on the DECIDE path.

This module is a thin, honest client for `POST /v3/autotrust/v1/decide`:

* **Per-request credentials.** The Authorization header is read on every call (process setting or
  the asking user's own key in multi-user mode), never baked into a cached client — a key pasted
  into the Settings page takes effect on the next judgement, with no restart.
* **Images are first-class.** The model is vision-language: `state` accepts `{"image": ...}` blocks,
  so Dobot can ask it what it *sees* on the user's screen. Screenshots are downscaled to ~448 px
  and re-encoded as JPEG when Pillow is available (the model's own guidance), with a byte-capped
  fallback when it is not.
* **Never raises into a task.** Callers get exceptions with a readable reason; the judgement layer
  decides how to degrade. `health()` exists so the Doctor can distinguish "no key", "bad key",
  "empty balance" and "network" instead of showing one vague failure.
"""

from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

#: The decision-evaluation endpoint. `model` is fixed to autotrust/jev-27b-vl by the service.
DECIDE_PATH = "/v3/autotrust/v1/decide"

#: Data URLs up to this size are refused: past ~3 MB the model's image tokens dwarf the question.
MAX_IMAGE_BYTES = 3_000_000


class AutoTrustError(RuntimeError):
    """A decide call failed. ``reason`` is the upstream error code when one came back."""

    def __init__(self, message: str, *, reason: str = "", status_code: int = 0) -> None:
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code


@dataclass
class DecideResult:
    """One answer from the model. Every field is designed to be shown as-is."""

    choice: str = ""
    choice_index: int = -1
    options: list[str] = field(default_factory=list)
    probabilities: list[float] = field(default_factory=list)
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    num_model_requests: int = 1
    thinking: dict[str, Any] | None = None

    def probability_of(self, option: str) -> float:
        try:
            index = self.options.index(option)
        except ValueError:
            return 0.0
        return self.probabilities[index] if 0 <= index < len(self.probabilities) else 0.0

    def expected_index(self) -> float:
        """Probability-weighted position across ``options``, normalised to 0..1."""
        if not self.probabilities:
            return 0.0
        total = sum(self.probabilities)
        if total <= 0:
            return 0.0
        weighted = sum(index * probability for index, probability in enumerate(self.probabilities))
        return weighted / (len(self.probabilities) - 1) / total if len(self.probabilities) > 1 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "choice": self.choice,
            "choice_index": self.choice_index,
            "options": list(self.options),
            "probabilities": [round(p, 4) for p in self.probabilities],
            "model": self.model,
            "usage": dict(self.usage),
            "elapsed_seconds": self.elapsed_seconds,
            "num_model_requests": self.num_model_requests,
            "thinking": self.thinking,
        }


class AutoTrustClient:
    """Thin client over the decide endpoint. Credentials are resolved per call."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ capability

    @property
    def key(self) -> str:
        """The key this call would use: the asking user's own key, else the process setting."""
        from app.core.tenancy import override_key

        return override_key("novita_api_key", self.settings.novita_api_key).strip()

    @property
    def available(self) -> bool:
        return bool(self.key)

    # ------------------------------------------------------------------ transport

    def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self.settings.novita_base_url.rstrip("/"))
        return self._http

    def reset_http(self) -> None:
        """Drop the cached HTTP client so the next call re-reads credentials."""
        self._http = None

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------ judging

    async def decide(
        self,
        *,
        kind: str,
        question: str,
        state: Any = "",
        options: list[str] | None = None,
        thinking: str | None = None,
        threshold: float | None = None,
        reasoning_effort: str | None = None,
        think_budget: int | None = None,
        timeout: float | None = None,
    ) -> DecideResult:
        """Ask one question. Raises :class:`AutoTrustError` with the upstream reason on failure."""
        if not self.available:
            raise AutoTrustError("no Novita API key configured", reason="MISSING_API_KEY")
        body: dict[str, Any] = {
            "model": self.settings.jev_model,
            "kind": kind,
            "state": state,
            "question": question,
        }
        if options:
            body["options"] = list(options)
        if thinking:
            body["thinking"] = thinking
            if threshold is not None:
                body["threshold"] = threshold
            if reasoning_effort:
                body["reasoning_effort"] = reasoning_effort
            if think_budget:
                body["think_budget"] = think_budget
        started = time.perf_counter()
        try:
            response = await self._http_client().post(
                DECIDE_PATH,
                json=body,
                headers={"Authorization": f"Bearer {self.key}"},
                timeout=httpx.Timeout(float(timeout or self.settings.jev_timeout_seconds), connect=5.0),
            )
        except httpx.HTTPError as exc:
            raise AutoTrustError(f"AutoTrust unreachable: {exc}", reason="UNREACHABLE") from exc
        latency = time.perf_counter() - started
        if response.status_code >= 400:
            reason, message = _error_from(response)
            raise AutoTrustError(
                f"AutoTrust refused the request ({reason or response.status_code}): {message}",
                reason=reason,
                status_code=response.status_code,
            )
        payload = response.json()
        return DecideResult(
            choice=str(payload.get("choice", "")),
            choice_index=int(payload.get("choice_index", -1)),
            options=[str(option) for option in payload.get("options") or []],
            probabilities=_clamp_list(payload.get("probabilities")),
            model=str(payload.get("model", "")),
            usage=dict(payload.get("usage") or {}),
            elapsed_seconds=float(payload.get("elapsed_seconds") or latency),
            num_model_requests=int(payload.get("num_model_requests") or 1),
            thinking=payload.get("thinking"),
        )

    async def noul(self, *, question: str, state: Any = "", **kwargs: Any) -> float:
        """A yes/no question. Returns P(\"true\"), 0..1."""
        result = await self.decide(kind="noul", question=question, state=state, **kwargs)
        return result.probability_of("true")

    async def health(self) -> dict[str, Any]:
        """One live, almost-free probe (a handful of input tokens). Never raises."""
        if not self.available:
            return {"ok": False, "configured": False, "error": "no Novita API key configured"}
        started = time.perf_counter()
        try:
            result = await self.decide(
                kind="noul",
                question="Is this a connectivity probe?",
                state="probe",
                thinking="off",
                timeout=min(float(self.settings.jev_timeout_seconds), 10.0),
            )
        except AutoTrustError as exc:
            return {
                "ok": False,
                "configured": True,
                "error": str(exc),
                "reason": exc.reason,
                "status_code": exc.status_code,
            }
        return {
            "ok": True,
            "configured": True,
            "model": result.model,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "usage": dict(result.usage),
        }


def _error_from(response: httpx.Response) -> tuple[str, str]:
    """(reason code, human message) from the platform error envelope, whatever shape it came in."""
    try:
        payload = response.json()
    except ValueError:
        return "", response.text[:200]
    if isinstance(payload, dict):
        return str(payload.get("reason") or ""), str(payload.get("message") or "")[:200]
    return "", ""


def _clamp_list(values: Any) -> list[float]:
    """Probabilities from the wire, clamped to 0..1 and coerced to float — never trusted raw."""
    if not isinstance(values, list):
        return []
    clamped: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 0.0
        clamped.append(max(0.0, min(1.0, number)))
    return clamped


def image_data_url(path: str | Path) -> str | None:
    """A screenshot as a data URL the model can read, or None when it is too big to send.

    The model's own guidance is "a long edge of about 448 pixels is enough for most judgments", so
    with Pillow installed (the OCR extra) the image is downscaled and re-encoded as JPEG — both
    fewer image tokens and a fraction of the upload. Without Pillow the original bytes go as-is,
    capped, so the vision check degrades instead of disappearing.
    """
    file = Path(str(path))
    if not file.is_file():
        return None
    try:
        from PIL import Image  # optional dependency (the OCR extra) — imported on use

        with Image.open(file) as image:
            image = image.convert("RGB")
            image.thumbnail((448, 448))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=70)
        payload = buffer.getvalue()
        return f"data:image/jpeg;base64,{base64.b64encode(payload).decode('ascii')}"
    except ImportError:
        pass
    except OSError:
        return None
    try:
        payload = file.read_bytes()
    except OSError:
        return None
    if len(payload) > MAX_IMAGE_BYTES:
        return None
    suffix = file.suffix.lower().lstrip(".")
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "webp": "webp", "gif": "gif"}.get(suffix, "png")
    return f"data:image/{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def build_autotrust_client(settings: Settings | None = None) -> AutoTrustClient:
    return AutoTrustClient(settings)
