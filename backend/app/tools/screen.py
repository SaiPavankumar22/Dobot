"""Screen capture, OCR and vision transcription.

Capture is strictly on demand and the resulting pixels are not persisted beyond the current task.
Three capabilities degrade independently: capture (needs a display), OCR (needs tesseract) and vision
transcription (needs a configured vision model).
"""

from __future__ import annotations

import asyncio
import base64
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.logging_setup import get_logger
from app.schemas import (
    CheckResult,
    ExecutionResult,
    Region,
    RiskLevel,
    VerificationResult,
)
from app.tools.base import Tool, ToolContext

logger = get_logger(__name__)


def capture_png(region: Region | None = None) -> bytes:
    """Capture the screen (or a region) as PNG bytes. Raises RuntimeError if unavailable."""
    try:
        import mss  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("screen capture requires the 'ocr' extra (mss, Pillow)") from exc

    import io

    with mss.mss() as sct:
        if region and region.width > 0 and region.height > 0:
            monitor = {
                "left": region.x,
                "top": region.y,
                "width": region.width,
                "height": region.height,
            }
        else:
            monitors = sct.monitors
            monitor = monitors[region.monitor + 1] if region and len(monitors) > 1 else monitors[0]
        shot = sct.grab(monitor)
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


def ocr_png(png: bytes, settings: Settings) -> tuple[str, str]:
    """Local OCR via tesseract. Returns (text, engine)."""
    if not settings.ocr_enabled:
        return "", "disabled"
    try:
        import io

        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError:
        return "", "unavailable"
    try:
        image = Image.open(io.BytesIO(png))
        text = pytesseract.image_to_string(image, lang=settings.ocr_language)
        return text.strip(), "tesseract"
    except Exception as exc:  # noqa: BLE001 - tesseract binary missing is common
        logger.info("OCR unavailable (%s)", exc)
        return "", "unavailable"


async def vision_transcribe(png: bytes, settings: Settings, question: str = "") -> tuple[str, str]:
    """Ask the configured vision model to read (or answer about) an image."""
    if not settings.has_nebius or not settings.nemotron_vision_model:
        return "", "unavailable"
    prompt = question.strip() or (
        "Transcribe all readable text in this screenshot, preserving structure. "
        "Then describe any diagram or chart in one sentence."
    )
    payload = {
        "model": settings.nemotron_vision_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{base64.b64encode(png).decode()}"},
                    },
                ],
            }
        ],
        "max_tokens": 1200,
    }
    try:
        async with httpx.AsyncClient(
            base_url=settings.nebius_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.nebius_api_key}"},
            timeout=httpx.Timeout(60.0),
        ) as client:
            response = await client.post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
        text = data["choices"][0]["message"]["content"]
        if isinstance(text, list):
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        return str(text).strip(), f"vision:{settings.nemotron_vision_model}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("vision transcription failed (%s)", exc)
        return "", "unavailable"


async def extract_screen_text(
    png: bytes, settings: Settings, question: str = ""
) -> tuple[str, str]:
    """OCR and vision run in parallel; whichever produces text wins (vision preferred for questions)."""
    ocr_task = asyncio.to_thread(ocr_png, png, settings) if settings.ocr_enabled else None
    vision_task = asyncio.create_task(vision_transcribe(png, settings, question))

    ocr_text, ocr_engine = ("", "unavailable")
    if ocr_task is not None:
        try:
            ocr_text, ocr_engine = await ocr_task
        except Exception as exc:  # noqa: BLE001
            logger.info("OCR failed (%s)", exc)
    vision_text, vision_engine = await vision_task

    if vision_engine != "unavailable" and vision_text:
        combined = vision_text
        if ocr_text and ocr_text not in vision_text:
            combined = f"{vision_text}\n\n[raw OCR]\n{ocr_text}"
            engine = f"{vision_engine}+{ocr_engine}"
        else:
            engine = vision_engine
        return combined[:12000], engine
    if ocr_text:
        return ocr_text[:12000], ocr_engine
    return "", "unavailable"


def save_ephemeral(png: bytes, settings: Settings) -> Path:
    """Write a capture to the state directory so it can be referenced by id during this task."""
    directory = settings.state_dir / "screens"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"screen_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S_%f')}.png"
    path.write_bytes(png)
    return path


class ScreenCaptureTool(Tool):
    name = "screen_capture"
    description = "Capture a region of the screen (or the whole screen) as an image for the current task."
    risk_floor = RiskLevel.LOW
    proactive_ok = True
    parameters = {
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "monitor": {"type": "integer", "default": 0},
        },
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        region = Region(
            x=int(params.get("x", 0) or 0),
            y=int(params.get("y", 0) or 0),
            width=int(params.get("width", 0) or 0),
            height=int(params.get("height", 0) or 0),
            monitor=int(params.get("monitor", 0) or 0),
        )
        try:
            png = await asyncio.to_thread(capture_png, region)
        except Exception as exc:  # noqa: BLE001
            return self.fail(self.name, f"screen capture unavailable: {exc}", started)
        path = save_ephemeral(png, ctx.settings)
        return self.ok(
            self.name,
            {"image_ref": str(path), "bytes": len(png), "region": region.model_dump()},
            started,
        )

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        path = Path(str(output.get("image_ref", "")))
        checks = [
            CheckResult(name="capture_written", passed=path.exists(), detail=str(path)),
            CheckResult(name="non_empty", passed=int(output.get("bytes", 0)) > 100),
        ]
        return VerificationResult(verified=all(check.passed for check in checks), checks=checks)


class ScreenAnalyzeTool(Tool):
    name = "screen_analyze"
    description = "Extract text from a screen region (OCR + vision) and optionally answer a question about it."
    risk_floor = RiskLevel.LOW
    proactive_ok = True
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "image_base64": {"type": "string", "description": "PNG supplied by the desktop client"},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
        },
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        question = str(params.get("question", "Explain this."))
        png: bytes | None = None
        if params.get("image_base64"):
            try:
                png = base64.b64decode(str(params["image_base64"]))
            except Exception as exc:  # noqa: BLE001
                return self.fail(self.name, f"invalid image payload: {exc}", started)
        else:
            region = Region(
                x=int(params.get("x", 0) or 0),
                y=int(params.get("y", 0) or 0),
                width=int(params.get("width", 0) or 0),
                height=int(params.get("height", 0) or 0),
            )
            try:
                png = await asyncio.to_thread(capture_png, region)
            except Exception as exc:  # noqa: BLE001
                return self.fail(self.name, f"screen capture unavailable: {exc}", started)

        text, engine = await extract_screen_text(png, ctx.settings, question)
        return self.ok(
            self.name,
            {
                "question": question,
                "text": text,
                "engine": engine,
                "bytes": len(png),
                "degraded": engine == "unavailable",
            },
            started,
        )

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        checks = [
            CheckResult(name="image_obtained", passed=int(output.get("bytes", 0)) > 100),
            CheckResult(
                name="text_extracted",
                passed=bool(output.get("text")),
                detail=output.get("engine", ""),
            ),
        ]
        return VerificationResult(
            verified=all(check.passed for check in checks),
            checks=checks,
            skipped_reason="" if output.get("text") else "no text could be read from the image",
        )
