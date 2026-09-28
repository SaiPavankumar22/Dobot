"""WebSocket event stream.

The desktop client subscribes here for dot status, task progress, approval prompts, research progress
and completion events. Recent events are replayed on connect so a reconnecting client does not lose the
tail of a running task.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)
router = APIRouter()


@router.websocket("/ws")
async def event_stream(websocket: WebSocket) -> None:
    # The HTTP bearer middleware never sees a WebSocket upgrade, so an enabled DOBOT_API_TOKEN gate
    # is enforced here explicitly — a gated backend must not stream its event bus (or accept kill and
    # approval messages) from anyone who asks. The token arrives as `?access_token=` because a browser
    # WebSocket cannot carry an Authorization header; non-browser clients may send either form.
    settings = getattr(websocket.app.state, "settings", None) or get_settings()
    token = (settings.dobot_api_token or "").strip()
    if token:
        supplied = websocket.query_params.get("access_token", "").strip()
        if not supplied:
            header = websocket.headers.get("authorization", "")
            supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not supplied:
            supplied = websocket.headers.get("x-dobot-token", "").strip()
        if not secrets.compare_digest(supplied, token):
            # 4401 = application-level unauthorized: refused before it joins the bus.
            await websocket.accept()
            await websocket.close(code=4401)
            return

    services = getattr(websocket.app.state, "services", None)
    if services is None:  # pragma: no cover
        await websocket.close(code=1013)
        return

    await websocket.accept()
    bus = services.bus
    queue = bus.subscribe()
    reconnect_replay = 50

    try:
        await websocket.send_json(
            {
                "type": "hello",
                "message": "connected to Dobot",
                "providers": await services.provider_status(),
                # The replay can end on a stale status, so the authoritative current value rides along.
                "dot_status": bus.dot_status,
                "active_tasks": services.kill_switch.active_ids,
                "replay": [event.model_dump(mode="json") for event in bus.history(limit=reconnect_replay)],
            }
        )

        async def sender() -> None:
            while True:
                event = await queue.get()
                await websocket.send_json(event.model_dump(mode="json"))

        async def receiver() -> None:
            while True:
                message = await websocket.receive_json()
                kind = str(message.get("type", ""))
                if kind == "ping":
                    await websocket.send_json({"type": "pong"})
                elif kind == "kill":
                    killed = await services.orchestrator.kill(message.get("task_id"))
                    await websocket.send_json({"type": "kill_ack", "killed": killed})
                elif kind == "approval":
                    await services.orchestrator.handle_approval(
                        str(message.get("approval_id", "")),
                        str(message.get("decision", "reject")),
                        note=str(message.get("note", "")),
                    )
                elif kind == "status_request":
                    await websocket.send_json(
                        {"type": "dot_status", "message": "current", "status": "IDLE"}
                    )
                else:
                    await websocket.send_json({"type": "error", "message": f"unknown message type: {kind}"})

        sender_task = asyncio.create_task(sender())
        receiver_task = asyncio.create_task(receiver())
        done, pending = await asyncio.wait(
            {sender_task, receiver_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done | pending:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await task
    except WebSocketDisconnect:
        logger.debug("websocket client disconnected")
    except Exception as exc:  # noqa: BLE001
        logger.info("websocket closed (%s)", exc)
    finally:
        bus.unsubscribe(queue)
