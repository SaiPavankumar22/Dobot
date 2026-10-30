"""The voice call's WebSocket: duplex audio in, streamed events out.

The gate mirrors ``/ws`` — origin checked by hand (CORS never sees an upgrade) and the operator
or user token compared explicitly — because this socket carries the same conversation surface and
opens with the same privileges. The protocol is deliberately small and JSON-only, shaped like the
reference pipeline's realtime channel:

    client → {"type":"audio","rate":48000,"data":<b64 pcm16>}   mic frames, any rate
             {"type":"cancel"}                                  the panel's own stop button
             {"type":"end"}                                     hang up
    server → {"type":"call_status", …}                          what each stage is
             {"type":"call_state","state":loading|listening|thinking|speaking}
             {"type":"call_transcript","role":"user","text":…}  a finished utterance
             {"type":"call_text","role":"assistant","delta":…}   streamed caption text
             {"type":"call_audio","rate":…,"data":<b64 pcm16>}  spoken audio as it is made
             {"type":"call_interrupted"}                        barge-in landed
             {"type":"call_error","code":…,"message":…}         honest failure, once
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)
router = APIRouter()


async def _gate(websocket: WebSocket) -> str:
    """Origin + token, exactly as ``/ws`` applies them.

    "" means the socket may proceed; a string of digits is the application-level close code the
    client should see after an accept-then-close refusal.
    """
    import secrets

    from app.api.ws import _origin_allowed

    if not _origin_allowed(websocket):
        return "4403"
    settings = getattr(websocket.app.state, "settings", None) or get_settings()
    token = (settings.dobot_api_token or "").strip()
    supplied = websocket.query_params.get("access_token", "").strip()
    if not supplied:
        header = websocket.headers.get("authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not supplied:
        supplied = websocket.headers.get("x-dobot-token", "").strip()

    services = getattr(websocket.app.state, "services", None)
    if services is None:
        return "1013"
    if settings.multi_user and supplied.startswith("dobot_u_"):
        manager = getattr(services, "user_manager", None)
        user = await manager.resolve_token(supplied) if manager else None
        return "" if user is not None else "4401"
    if token:
        return "" if secrets.compare_digest(supplied, token) else "4401"
    if settings.multi_user and not supplied:
        return "4401"
    return ""


@router.websocket("/voice/call")
async def voice_call(websocket: WebSocket) -> None:
    verdict = await _gate(websocket)
    if verdict:
        # Accept-then-close, the same shape as /ws: the client sees an application-level refusal
        # rather than a mystery handshake failure.
        await websocket.accept()
        await websocket.close(code=int(verdict))
        return

    settings = getattr(websocket.app.state, "settings", None) or get_settings()
    if not settings.voice_call_enabled:
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "call_error",
                "code": "disabled",
                "message": "Voice calls are off (VOICE_CALL_ENABLED=false).",
            }
        )
        await websocket.close(code=4404)
        return

    services = getattr(websocket.app.state, "services", None)
    if services is None:  # pragma: no cover - lifespan always installs them
        await websocket.accept()
        await websocket.close(code=1013)
        return

    await websocket.accept()
    await websocket.send_json({"type": "call_state", "state": "loading"})
    from app.agents.call.vad import build_detector, ensure_silero_model

    # The model fetch is blocking and one-time; off the event loop so the first call does not
    # stall every other connection for a second.
    model_path = await asyncio.to_thread(ensure_silero_model, settings)
    detector = await asyncio.to_thread(build_detector, settings, model_path)

    from app.agents.call.session import VoiceCallSession

    session = VoiceCallSession(settings, detector)
    await websocket.send_json(
        {"type": "call_status", "hints": session.status_hints(), **session.describe()}
    )

    answer_task = asyncio.create_task(session.answer_loop())
    sender_task: asyncio.Task[Any] | None = None

    async def sender() -> None:
        while True:
            message = await session.outbox.get()
            await websocket.send_json(message)

    sender_task = asyncio.create_task(sender())
    try:
        while True:
            message = await websocket.receive_json()
            kind = str(message.get("type", ""))
            if kind == "audio":
                try:
                    pcm = base64.b64decode(str(message.get("data", "")), validate=False)
                except Exception:  # noqa: BLE001 - one malformed frame must not kill the call
                    continue
                rate = int(message.get("rate") or 16000)
                session.feed_audio(pcm, rate)
            elif kind == "cancel":
                session.barge_in()
            elif kind == "end":
                break
            else:
                await websocket.send_json(
                    {"type": "call_error", "code": "protocol", "message": f"unknown message: {kind}"}
                )
    except WebSocketDisconnect:
        logger.debug("voice call: client left")
    except Exception as exc:  # noqa: BLE001
        logger.info("voice call closed (%s)", exc)
    finally:
        await session.close()
        answer_task.cancel()
        if sender_task is not None:
            sender_task.cancel()
        for task in (answer_task, sender_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    await task
