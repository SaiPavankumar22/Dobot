"""WebSocket event stream.

The desktop client subscribes here for dot status, task progress, approval prompts, research progress
and completion events. Recent events are replayed on connect so a reconnecting client does not lose the
tail of a running task.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)
router = APIRouter()


def _origin_allowed(websocket: WebSocket) -> bool:
    """Browser provenance, checked by hand because CORS never sees a WebSocket upgrade.

    Any page can *open* ``ws://127.0.0.1:8756`` — the HTTP CORS middleware does not govern the
    upgrade — and this stream carries approval prompts that the same connection may then answer.
    So the CORS allowlist itself is re-applied here (imported lazily: app.main imports this
    router). No ``Origin`` at all means a non-browser client (curl, the selftest, tests): those
    remain subject to the token gate below.
    """
    origin = (websocket.headers.get("origin") or "").strip()
    if not origin:
        return True
    from app.main import ALLOWED_ORIGIN_REGEX, ALLOWED_ORIGINS

    return origin in ALLOWED_ORIGINS or re.match(ALLOWED_ORIGIN_REGEX, origin) is not None


@router.websocket("/ws")
async def event_stream(websocket: WebSocket) -> None:
    # The HTTP bearer middleware never sees a WebSocket upgrade, so the gate is enforced here
    # explicitly — a gated backend must not stream its event bus (or accept kill and approval
    # messages) from anyone who asks. The token arrives as `?access_token=` because a browser
    # WebSocket cannot carry an Authorization header; non-browser clients may send either form.
    #
    # Multi-user: a `dobot_u_...` token authenticates a person; their scope is attached to this
    # connection so approvals/kills act as them and the stream carries only their events plus
    # system-wide ones. The operator token (if any) still connects as the unscoped viewer.
    settings = getattr(websocket.app.state, "settings", None) or get_settings()
    if not _origin_allowed(websocket):
        # Accept-then-close, the same shape as the token refusals below: the client sees an
        # application-level 4403 rather than a mystery handshake failure.
        await websocket.accept()
        await websocket.close(code=4403)
        return
    token = (settings.dobot_api_token or "").strip()
    supplied = websocket.query_params.get("access_token", "").strip()
    if not supplied:
        header = websocket.headers.get("authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not supplied:
        supplied = websocket.headers.get("x-dobot-token", "").strip()

    services = getattr(websocket.app.state, "services", None)
    if services is None:  # pragma: no cover
        await websocket.close(code=1013)
        return

    viewer_uid = ""  # empty = operator/system viewer (sees every event)
    key_overrides: dict[str, str] = {}
    user_token = supplied if settings.multi_user and supplied.startswith("dobot_u_") else ""
    if user_token:
        manager = getattr(services, "user_manager", None)
        user = await manager.resolve_token(user_token) if manager else None
        if user is None:
            await websocket.accept()
            await websocket.close(code=4401)
            return
        viewer_uid = user.id
        from app.main import _load_user_key_overrides

        key_overrides = await _load_user_key_overrides(services, user.id)
    elif token and not secrets.compare_digest(supplied, token):
        # 4401 = application-level unauthorized: refused before it joins the bus.
        await websocket.accept()
        await websocket.close(code=4401)
        return
    elif not token and settings.multi_user and not supplied:
        # Multi-user with no operator gate: anonymous sockets are refused rather than
        # upgraded to an all-seeing viewer.
        await websocket.accept()
        await websocket.close(code=4401)
        return

    await websocket.accept()
    bus = services.bus
    queue = bus.subscribe()
    reconnect_replay = 50

    def _mine(event: Any) -> bool:
        """System events go to everyone; a user-stamped event goes only to its owner
        (the unscoped operator viewer sees all, matching the HTTP surface)."""
        if not viewer_uid or not event.user_id:
            return True
        return event.user_id == viewer_uid

    from app.core.tenancy import current_user_id, user_key_overrides

    scope = current_user_id.set(viewer_uid)
    keys_scope = user_key_overrides.set(key_overrides)
    try:
        await websocket.send_json(
            {
                "type": "hello",
                "message": "connected to Dobot",
                "providers": await services.provider_status(),
                # The replay can end on a stale status, so the authoritative current value rides along.
                "dot_status": bus.dot_status,
                "active_tasks": services.kill_switch.active_ids,
                "replay": [
                    event.model_dump(mode="json")
                    for event in bus.history(limit=reconnect_replay)
                    if _mine(event)
                ],
            }
        )

        async def sender() -> None:
            while True:
                event = await queue.get()
                if not _mine(event):
                    continue
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
                    scope = str(message.get("scope", "once"))
                    await services.orchestrator.handle_approval(
                        str(message.get("approval_id", "")),
                        str(message.get("decision", "reject")),
                        note=str(message.get("note", "")),
                        scope="lifetime" if scope == "lifetime" else "once",
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
        current_user_id.reset(scope)
        user_key_overrides.reset(keys_scope)
