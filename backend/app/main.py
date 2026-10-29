"""Dobot gateway (FastAPI).

Wires the service graph into an application, exposes the HTTP + WebSocket surface, and reports its own
posture honestly: which providers are live, which are degraded, and what is currently running.
"""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api import (
    activity,
    approvals,
    auth,
    automations,
    chat,
    conversations,
    memory,
    research,
    screen,
    security,
    skills,
    system,
    tasks,
    voice_call,
    ws,
)
from app.api import (
    settings as settings_api,
)
from app.config import Settings, get_settings
from app.logging_setup import configure_logging, get_logger
from app.services import build_services

logger = get_logger(__name__)

#: Reachable without a bearer token. `/health` stays open so a supervisor or the desktop shell can
#: always ask "are you alive?" without holding a credential.
AUTH_EXEMPT_PATHS = frozenset({"/", "/health", "/docs", "/redoc", "/openapi.json", "/auth/status"})

#: With MULTI_USER=true these are reachable without a token (register/login are the way in).
MULTI_USER_PUBLIC_PATHS = frozenset({"/auth/register", "/auth/login"})

ALLOWED_ORIGINS = [
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
]

#: The regex half of the same policy — only the supported desktop/dev origins.
#: One string, two gates: CORS applies it to fetch/XHR, and `/ws` re-applies it to WebSocket
#: upgrades, which the CORS middleware never sees. Do not broaden this to arbitrary localhost
#: ports: any local web page could otherwise read and mutate the API when the optional bearer gate
#: is disabled.
ALLOWED_ORIGIN_REGEX = (
    r"^(http://localhost:1420|http://127\.0\.0\.1:1420|http://localhost:5173|"
    r"http://127\.0\.0\.1:5173|tauri://localhost|https?://tauri\.localhost)$"
)


async def _load_user_key_overrides(services: Any, user_id: str) -> dict[str, str]:
    """The asking user's own provider keys, stored per user under the settings collection."""
    if services is None:
        return {}
    # Read the user's copy directly through the base store: the scoped wrapper would
    # prefix with the *ambient* user, which is unset this early in the request.
    from app.core.tenancy import scoped_name

    base = getattr(services.store, "_base", services.store)
    doc = await base.get(scoped_name(user_id, "settings"), "user_keys")
    keys = (doc or {}).get("keys") if isinstance(doc, dict) else None
    return {str(k): str(v) for k, v in (keys or {}).items() if str(v).strip()}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        services = await build_services(settings)
        app.state.services = services
        await services.startup()
        logger.info("Dobot gateway listening on %s:%s", settings.dobot_host, settings.dobot_port)
        try:
            yield
        finally:
            await services.shutdown()

    app = FastAPI(
        title="Dobot",
        version=__version__,
        description="Always-on personal AI operating layer — gateway, orchestrator, decision engine.",
        lifespan=lifespan,
    )
    # The exact settings instance this app runs with (tests mutate it; ws.py needs the same gate).
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_origin_regex=ALLOWED_ORIGIN_REGEX,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for router in (
        chat.router,
        screen.router,
        research.router,
        tasks.router,
        conversations.router,
        auth.router,
        automations.router,
        approvals.router,
        memory.router,
        skills.router,
        settings_api.router,
        security.router,
        activity.router,
        system.router,
        ws.router,
        voice_call.router,
    ):
        app.include_router(router)

    # ------------------------------------------------------------------ auth

    @app.middleware("http")
    async def require_token(request: Request, call_next: Any):
        """Bearer-token gate: the operator token, and (with MULTI_USER=true) user tokens.

        Off by default: on a single-user laptop the API is only reachable from this machine, and a
        token nobody sets is security theatre. It is worth turning on the moment this machine shares a
        network, and the Doctor page reports plainly which of those two worlds you are in.

        Multi-user: a ``dobot_u_...`` token identifies a person; their id and their own API keys
        are attached to the request context, so every store read/write below is theirs alone.
        """
        from app.core.tenancy import current_user_id, user_key_overrides

        path = request.url.path
        if request.method == "OPTIONS" or path in AUTH_EXEMPT_PATHS or path.startswith("/docs"):
            return await call_next(request)
        token = (settings.dobot_api_token or "").strip()
        header = request.headers.get("authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not supplied:
            supplied = request.headers.get("x-dobot-token", "").strip()

        multi = bool(settings.multi_user)
        if multi and path in MULTI_USER_PUBLIC_PATHS:
            return await call_next(request)

        if multi and supplied.startswith("dobot_u_"):
            services = getattr(request.app.state, "services", None)
            manager = getattr(services, "user_manager", None) if services else None
            user = await manager.resolve_token(supplied) if manager else None
            if user is None:
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "error": {"code": "INVALID_USER_TOKEN", "message": "log in again", "recoverable": True},
                    },
                )
            overrides = await _load_user_key_overrides(services, user.id)
            id_token = current_user_id.set(user.id)
            key_token = user_key_overrides.set(overrides)
            try:
                return await call_next(request)
            finally:
                current_user_id.reset(id_token)
                user_key_overrides.reset(key_token)

        if not token:
            if multi:
                # Multi-user on, no operator gate configured: an anonymous request would
                # fall through unscoped — passthrough reads of every legacy collection.
                # Accounts are the way in, so refuse instead.
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "error": {
                            "code": "AUTH_REQUIRED",
                            "message": "log in (POST /auth/login) — this Dobot runs with MULTI_USER=true",
                            "recoverable": True,
                        },
                    },
                )
            return await call_next(request)
        if not secrets.compare_digest(supplied, token):
            return JSONResponse(
                status_code=401,
                content={
                    "success": False,
                    "error": {
                        "code": "UNAUTHORIZED",
                        "message": "This Dobot API requires a bearer token (DOBOT_API_TOKEN).",
                        "recoverable": True,
                    },
                },
            )
        return await call_next(request)

    # ------------------------------------------------------------------ errors

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        detail: Any = exc.detail
        if isinstance(detail, dict) and {"code", "message"} <= set(detail):
            body = {"success": False, "error": detail}
        else:
            body = {
                "success": False,
                "error": {"code": f"HTTP_{exc.status_code}", "message": str(detail), "recoverable": False},
            }
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": str(exc)[:400],
                    "recoverable": True,
                },
            },
        )

    # ------------------------------------------------------------------ meta

    @app.get("/", tags=["meta"])
    async def index() -> dict:
        return {
            "name": "Dobot",
            "version": __version__,
            "tagline": "An AI that doesn't just answer you. It works for you.",
            "endpoints": [
                "POST /chat",
                "POST /screen/analyze",
                "POST /research",
                "GET|POST /tasks",
                "GET|POST /automations",
                "GET|POST /approvals",
                "GET|POST /memory",
                "GET|POST /skills",
                "GET /settings/providers",
                "GET /security/status",
                "POST /security/kill",
                "GET /activity",
                "WS /ws",
            ],
        }

    @app.get("/health", tags=["meta"])
    async def health() -> dict:
        services = getattr(app.state, "services", None)
        if services is None:  # pragma: no cover
            return {"status": "starting", "version": __version__}
        providers = await services.provider_status()
        degraded = [
            name
            for name, value in providers.items()
            if value in {"not_configured", "unavailable"} or value.endswith(":unavailable")
        ]
        return {
            "status": "ok",
            "version": __version__,
            "checked_at": datetime.now(UTC).isoformat(),
            "providers": providers,
            "degraded": degraded,
            "active_tasks": services.kill_switch.active_ids,
            "scheduled_jobs": services.scheduler.jobs_summary(),
            "tools": len(services.registry),
            "auth_required": bool((settings.dobot_api_token or "").strip()),
            "interceptor_rules": len(services.interceptors.rules),
            "interceptor_errors": services.interceptors.errors,
            "voice_engine": services.voice.plan.engine or None,
            "tokenjuice": services.tokenjuice.summary(),
            "usage": services.cost.summary(),
        }

    @app.get("/auth/status", tags=["meta"])
    async def auth_status() -> dict:
        """Tells the desktop shell how to get in. Never reveals any token.

        ``multi_user`` lets the app show its sign-in/sign-up screen instead of the manual
        token field, and ``accounts`` (null when accounts are off) says whether this
        deployment has an owner yet — 0 means the first run goes straight to sign-up,
        where the first account becomes the owner without an invite.
        """
        services = getattr(app.state, "services", None)
        manager = getattr(services, "user_manager", None) if services else None
        accounts: int | None = None
        if settings.multi_user and manager is not None:
            try:
                accounts = await manager.count()
            except Exception:  # noqa: BLE001 - a store hiccup must not break the probe
                accounts = None
        return {
            "required": bool((settings.dobot_api_token or "").strip()),
            "header": "Authorization: Bearer <token>",
            "exempt": sorted(AUTH_EXEMPT_PATHS),
            "multi_user": bool(settings.multi_user),
            "open_registration": bool(settings.multi_user_open_registration),
            "accounts": accounts,
        }

    @app.get("/events", tags=["meta"])
    async def events_after(after: int = -1) -> dict:
        """Cursor-based event poll — the desktop shell's fallback when the WebSocket is blocked.

        This endpoint carries the same events over plain HTTP when a WebSocket reconnects.

        ``after`` is the last sequence the client saw: ``-1`` means "give me the recent replay"
        (mirroring the WebSocket hello), otherwise only newer events come back. A cursor from the
        future — the server restarted and its counter started over — is treated as ``-1`` instead of
        swallowing every event until the counter catches up.
        """
        services = getattr(app.state, "services", None)
        if services is None:  # pragma: no cover - startup window
            return {"events": [], "dot_status": "UNKNOWN", "after": -1}
        bus = services.bus
        from app.core.tenancy import current_user_id

        viewer = current_user_id.get()
        # System events go to everyone; user-stamped events only to their owner
        # (the unscoped operator viewer sees all — same rule as the WebSocket).
        recent = [e for e in bus.history(limit=400) if not viewer or not e.user_id or e.user_id == viewer]
        latest = recent[-1].sequence if recent else 0
        if after > latest:
            after = -1
        fresh = recent[-50:] if after < 0 else [event for event in recent if event.sequence > after]
        return {
            "events": [event.model_dump(mode="json") for event in fresh],
            "dot_status": bus.dot_status,
            "after": fresh[-1].sequence if fresh else after,
        }

    @app.get("/dashboard", tags=["meta"])
    async def dashboard() -> dict:
        """Everything the Overview page needs in one round trip."""
        services = getattr(app.state, "services", None)
        if services is None:  # pragma: no cover
            return {}
        tasks_docs = await services.store.all("tasks", limit=500)
        counts: dict[str, int] = {}
        for doc in tasks_docs:
            status = str(doc.get("status", "UNKNOWN"))
            counts[status] = counts.get(status, 0) + 1
        automations = await services.scheduler.list()
        pending = await services.approvals.pending()
        memory_stats = await services.memory.stats()
        return {
            "dot": "IDLE",
            "tasks": {
                "total": len(tasks_docs),
                "by_status": counts,
                "in_progress": [
                    {"id": doc.get("id"), "title": doc.get("title"), "progress": doc.get("progress", 0)}
                    for doc in tasks_docs
                    if str(doc.get("status")) in {"IN_PROGRESS", "PLANNING", "WAITING_APPROVAL"}
                ][:5],
            },
            "automations": {"total": len(automations), "active": sum(1 for a in automations if a.status == "active")},
            "approvals": {"pending": len(pending), "items": [item.model_dump(mode="json") for item in pending[:5]]},
            "memory": memory_stats,
            "providers": await services.provider_status(),
            "skills": services.skills.names(),
            "usage": services.cost.summary(),
            "tokenjuice": services.tokenjuice.summary(),
            "canonical": await services.canonical.stats(),
            "identity": await services.identity.summarise(),
            "interceptors": services.interceptors.summarise(),
            "voice": services.voice.summarise(),
        }

    return app


app = create_app()


def main() -> None:  # pragma: no cover - manual entrypoint
    import uvicorn

    settings = get_settings()
    configure_logging()
    with contextlib.suppress(KeyboardInterrupt):
        uvicorn.run(
            "app.main:app",
            host=settings.dobot_host,
            port=settings.dobot_port,
            log_level=settings.log_level.lower(),
        )


if __name__ == "__main__":  # pragma: no cover
    main()
