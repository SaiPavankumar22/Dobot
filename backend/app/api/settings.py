"""Settings and provider status.

The UI only ever learns *whether* a provider is configured — never a key (keys are returned masked).
Runtime toggles that are safe to change without a restart (shadow mode, screen capture policy) live
here, and so do credentials: `PUT /settings/keys/{name}` persists a key to the local `.env` and
hot-reloads every client that caches it, so keys can be added from the app's Settings page even in a
packaged install where no terminal or `.env` editor is assumed.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import ServicesDep

router = APIRouter(prefix="/settings", tags=["settings"])

MUTABLE = {"shadow_mode", "screen_capture", "jev_enabled", "ocr_enabled", "voice_enabled"}

# ------------------------------------------------------------------ who sets what
#
# Two kinds of credential, and the separation is deliberate:
#
# * **User keys** (below) are the ones a person brings for themselves. The app's Settings page
#   writes them to the backend's `.env` and applies them live.
# * **Operator values** are infrastructure: the tracing account and local API gate. They belong
#   in the backend environment and the app must not solicit them from an end user.

# Credential names the API may manage, mapped to the Settings field they populate.
KEY_FIELDS = {
    "nebius": "nebius_api_key",
    "tavily": "tavily_api_key",
    "novita": "novita_api_key",
    "sarvam": "sarvam_api_key",
}

# Connection strings rather than secrets (masked differently, still writable by the user).
#: Empty on purpose: semantic memory runs on the local index, so there is no hosted endpoint to
#: configure. Kept as a mapping so the API's key list stays uniform.
URI_FIELDS: dict[str, str] = {}

# Environment-only values. Present in the request payload as a name so the UI can explain why they
# are absent, never as a value, and never writable through this API. Wording stays neutral about
# *where* that environment is: `.env` for a backend you run yourself, a Space secret when the
# backend is hosted for you. Either way the app does not ask an end user for these.
OPERATOR_FIELDS = {
    "dobot_api_token": "backend access gate — backend environment only",
}


def _operator_reason(name: str) -> str | None:
    """Why this name is not writable from the app, or None when it is not operator-managed."""
    return OPERATOR_FIELDS.get(name)

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]} ({len(value)} chars)"


@router.get("")
async def get_settings(services: ServicesDep) -> dict:
    settings = services.settings
    return {
        "env": settings.dobot_env,
        "startup": True,
        "always_on_top": True,
        "screen_capture": settings.screen_capture,
        "voice_enabled": settings.voice_enabled,
        "shadow_mode": settings.shadow_mode,
        "jev_enabled": settings.jev_enabled,
        "kill_switch_hotkey": settings.kill_switch_hotkey,
        "models": {
            "provider": settings.reasoning_provider,
            "primary": settings.nemotron_model,
            "super": settings.nemotron_super_model,
            "light": settings.nemotron_light_model,
            "vision": settings.nemotron_vision_model,
        },
        "research": {"provider": "tavily"},
        "agent": {
            "runtime": settings.agent_runtime,
            "command": settings.hermes_command,
            "endpoint": settings.hermes_endpoint or "http://127.0.0.1:8642",
            "api_key_set": bool((settings.hermes_api_key or "").strip()),
        },
        "sandbox": {"provider": settings.sandbox_provider, "sandbox": settings.nemoclaw_sandbox},
        "memory": {
            "store": "sqlite",
            "vectors": "local",
        },
    }


@router.patch("")
async def update_settings(payload: dict, services: ServicesDep) -> dict:
    applied: dict[str, object] = {}
    for key, value in payload.items():
        if key not in MUTABLE:
            continue
        setattr(services.settings, key, value)
        applied[key] = value
    if not applied:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "NO_MUTABLE_SETTING",
                "message": f"allowed keys: {', '.join(sorted(MUTABLE))}",
                "recoverable": True,
            },
        )
    if "shadow_mode" in applied:
        services.decision.settings.shadow_mode = bool(applied["shadow_mode"])
    return {"applied": applied, "settings": await get_settings(services)}


@router.get("/providers")
async def providers(services: ServicesDep) -> dict:
    """Connection status only. Raw credentials never cross this boundary."""
    return await services.provider_status()


@router.get("/onboarding")
async def onboarding(services: ServicesDep) -> dict:
    status = await services.provider_status()
    provider = str(status.get("reasoning_provider", "cloud"))
    reasoning_detail = {
        "key_rejected": (
            "Nebius refused the saved key (HTTP 401). Paste a valid Token Factory API key under "
            "Your API keys — the one saved now is wrong, expired or revoked."
        ),
        "unreachable": "A key is saved, but the Nebius endpoint did not answer. Check the backend's network access.",
    }.get(
        status["nemotron"],
        "Set LOCAL_MODEL and REASONING_PROVIDER=local for an OpenAI-compatible local server, or paste "
        "your Nebius key under Your API keys for cloud reasoning.",
    )
    if provider in {"local", "hybrid"}:
        reasoning_detail = (
            "Local reasoning is selected. Confirm the local model server is running and LOCAL_MODEL "
            "matches an installed model."
            if status["nemotron"] != "connected"
            else "Local model server is connected."
        )
    steps = [
        {
            "id": "reasoning",
            "title": (
                "Connect a local reasoning model"
                if provider in {"local", "hybrid"}
                else "Connect Nemotron via Nebius Token Factory"
            ),
            "done": status["nemotron"] == "connected",
            "state": status["nemotron"],
            "detail": reasoning_detail,
        },
        {
            "id": "research",
            "title": "Connect Tavily for web research",
            "done": status["tavily"] == "connected",
            "detail": "Set TAVILY_API_KEY to enable source-aware research.",
        },
        {
            "id": "judgement",
            "title": "Connect AutoTrust JEV for action judgement",
            "done": status.get("autotrust") == "connected",
            "detail": (
                "Paste your Novita key under Your API keys (or set NOVITA_API_KEY in .env) — "
                "JEV-27B-VL then judges every proposed action with calibrated probabilities, "
                "including whether untrusted on-screen text is a prompt injection and whether a "
                "credential is visible on screen."
            ),
        },
        {
            "id": "execution",
            "title": "Connect the Hermes agent for desktop control",
            "done": "available" in status["agent_runtime"],
            "detail": (
                "Run the real Hermes gateway (hermes gateway with API_SERVER_ENABLED=true), then "
                "set AGENT_RUNTIME=remote, HERMES_ENDPOINT and HERMES_API_KEY."
            ),
        },
        {
            "id": "sandbox",
            "title": "Run execution inside NanoClaw / OpenShell",
            "done": status["sandbox"].endswith(":openshell"),
            "detail": "Set SANDBOX_PROVIDER=nemoclaw and OPENSHIELD_GATEWAY_URL for real isolation.",
        },
    ]
    return {"providers": status, "steps": steps, "complete": all(step["done"] for step in steps)}


# ---------------------------------------------------------------------- credentials
# Local credentials are stored in Windows Credential Manager (or an encrypted development fallback)
# and hot-reloaded into the running process. They never enter the SQLite database.


class KeyValue(BaseModel):
    value: str


def _apply_key(services: ServicesDep, field: str, value: str) -> None:
    """Set the field on the live Settings and hot-reload every client that cached credentials."""
    settings = services.settings
    setattr(settings, field, value)
    # Clients cache an httpx.AsyncClient with the old Authorization header — drop them so the next
    # call re-reads credentials. Cheap: a client is rebuilt lazily on demand.
    for client_attr, reset in (
        ("reasoner", lambda r: r.reset_http()),
        ("tavily", lambda t: t.reset_http()),
    ):  # noqa: B007 - readability over comprehension cleverness
        client = getattr(services, client_attr, None)
        if client is not None:
            try:
                reset(client)
            except Exception:  # noqa: BLE001 - a failed reset just means rebuild-on-next-use
                pass
    decision = getattr(services, "decision", None)
    jev = getattr(decision, "jev", None) if decision is not None else None
    autotrust = getattr(jev, "client", None)
    if autotrust is not None and hasattr(autotrust, "reset_http"):
        try:
            autotrust.reset_http()
        except Exception:  # noqa: BLE001
            pass


@router.get("/keys")
async def list_keys(services: ServicesDep) -> dict:
    """Masked presence of every user credential, plus the names that live in the environment.

    Raw values never leave the process. ``operator`` carries names and reasons only, so the UI can
    say *where* those values come from instead of showing an empty field nobody can fill.
    """
    settings = services.settings
    from app.core.tenancy import current_user_id

    uid = current_user_id.get()
    user_keys: dict[str, str] = {}
    if settings.multi_user and uid:
        doc = await services.store.get("settings", "user_keys") or {}
        user_keys = {str(k): str(v) for k, v in (doc.get("keys") or {}).items()}
    keys = []
    for name, field in {**KEY_FIELDS, **URI_FIELDS}.items():
        # Multi-user: show the person's own key first; fall back to the deployment's
        # presence (masked) so the UI can still say "a shared key is configured".
        value = str(user_keys.get(field) or getattr(settings, field, "") or "")
        keys.append(
            {
                "id": name,
                "field": field,
                "kind": "secret" if name in KEY_FIELDS else "uri",
                "set": bool(value.strip()),
                "masked": _mask(value) if value.strip() else "",
                "source": "user" if str(user_keys.get(field, "")).strip() else ("operator" if value.strip() else "unset"),
            }
        )
    operator = [
        {
            "id": field,
            "set": bool(str(getattr(settings, field, "") or "").strip()),
            "why": why,
        }
        for field, why in OPERATOR_FIELDS.items()
    ]
    return {"keys": keys, "operator": operator}


@router.put("/keys/{name}")
async def set_key(name: str, payload: KeyValue, services: ServicesDep) -> dict:
    """Store a credential: persisted to .env and applied to the live process immediately.

    Multi-user: the key belongs to the *person*, not the deployment — it is stored in their own
    scoped settings collection and applied per request via the tenancy overrides, never written
    to the shared .env (one user's key must not silently become another user's).
    """
    name = name.strip().lower()
    from app.core.tenancy import current_user_id

    uid = current_user_id.get()
    if services.settings.multi_user and uid:
        field = KEY_FIELDS.get(name) or URI_FIELDS.get(name)
        if field is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "UNKNOWN_KEY", "message": name, "recoverable": True},
            )
        value = payload.value.strip()
        if not value:
            raise HTTPException(
                status_code=422,
                detail={"code": "EMPTY_VALUE", "message": "send the key in the body", "recoverable": True},
            )
        doc = await services.store.get("settings", "user_keys") or {"id": "user_keys"}
        keys = dict(doc.get("keys") or {})
        keys[field] = value
        doc["keys"] = keys
        await services.store.insert("settings", doc)
        return {"ok": True, "id": name, "field": field, "kind": "secret", "persisted": "user-scoped"}
    field = KEY_FIELDS.get(name)
    kind = "secret"
    if field is None:
        field = URI_FIELDS.get(name)
        kind = "uri"
    reason = _operator_reason(name)
    if field is None and reason:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "OPERATOR_MANAGED",
                "message": f"{name} is set on the backend itself ({reason}), not from the app",
                "recoverable": False,
            },
        )
    if field is None or not _KEY_RE.match(name):
        raise HTTPException(
            status_code=404,
            detail={"code": "UNKNOWN_KEY", "message": f"manageable keys: {', '.join(sorted({**KEY_FIELDS, **URI_FIELDS}))}", "recoverable": True},
        )
    value = payload.value.strip()
    if not value:
        raise HTTPException(
            status_code=422,
            detail={"code": "EMPTY_VALUE", "message": "send the key in the body: {\"value\": \"...\"}", "recoverable": True},
        )
    try:
        from app.security.local_secrets import set_value

        persisted = set_value(field, value, services.settings.state_dir)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail={"code": "SECRET_WRITE_FAILED", "message": str(exc)[:200], "recoverable": False},
        ) from exc
    _apply_key(services, field, value)
    return {"ok": True, "id": name, "field": field, "kind": kind, "persisted": persisted}


@router.delete("/keys/{name}")
async def clear_key(name: str, services: ServicesDep) -> dict:
    """Remove a credential from the live process (the .env line is blanked, not deleted)."""
    name = name.strip().lower()
    field = KEY_FIELDS.get(name) or URI_FIELDS.get(name)
    if field is None and _operator_reason(name):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "OPERATOR_MANAGED",
                "message": f"{name} is set on the backend itself, not from the app",
                "recoverable": False,
            },
        )
    if field is None:
        raise HTTPException(status_code=404, detail={"code": "UNKNOWN_KEY", "message": name, "recoverable": True})
    from app.core.tenancy import current_user_id

    uid = current_user_id.get()
    if services.settings.multi_user and uid:
        # The key is the person's own: drop it from their scoped store. Never touch the
        # shared .env here — that would delete the deployment's key, not theirs.
        doc = await services.store.get("settings", "user_keys") or {"id": "user_keys"}
        keys = dict(doc.get("keys") or {})
        keys.pop(field, None)
        doc["keys"] = keys
        await services.store.insert("settings", doc)
        return {"ok": True, "id": name, "field": field}
    try:
        from app.security.local_secrets import delete

        delete(field, services.settings.state_dir)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail={"code": "SECRET_DELETE_FAILED", "message": str(exc)[:200], "recoverable": False}) from exc
    _apply_key(services, field, "")
    return {"ok": True, "id": name, "field": field}
