"""Settings and provider status.

The UI only ever learns *whether* a provider is configured — never a key (keys are returned masked).
Runtime toggles that are safe to change without a restart (shadow mode, screen capture policy) live
here, and so do credentials: `PUT /settings/keys/{name}` persists a key to the local `.env` and
hot-reloads every client that caches it, so keys can be added from the app's Settings page even in a
packaged install where no terminal or `.env` editor is assumed.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import ServicesDep

router = APIRouter(prefix="/settings", tags=["settings"])

MUTABLE = {"shadow_mode", "screen_capture", "jev_enabled", "ocr_enabled"}

# Credential names the API may manage, mapped to the Settings field they populate.
KEY_FIELDS = {
    "nebius": "nebius_api_key",
    "tavily": "tavily_api_key",
    "zilliz_token": "zilliz_token",
    "langsmith": "langsmith_api_key",
    "dobot_api_token": "dobot_api_token",
    "laya": "laya_api_key",
}

# Fields that hold connection strings rather than secrets (masked differently, still writable).
URI_FIELDS = {
    "mongodb_uri": "mongodb_uri",
    "zilliz_uri": "zilliz_uri",
    "langsmith_api_url": "langsmith_api_url",
    "laya_server_url": "laya_server_url",
    "langsmith_project": "langsmith_project",
}

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
        "voice_enabled": False,
        "shadow_mode": settings.shadow_mode,
        "jev_enabled": settings.jev_enabled,
        "kill_switch_hotkey": settings.kill_switch_hotkey,
        "models": {
            "primary": settings.nemotron_model,
            "super": settings.nemotron_super_model,
            "light": settings.nemotron_light_model,
            "vision": settings.nemotron_vision_model,
        },
        "research": {"provider": "tavily"},
        "agent": {"runtime": settings.agent_runtime, "command": settings.hermes_command},
        "sandbox": {"provider": settings.sandbox_provider, "sandbox": settings.nemoclaw_sandbox},
        "memory": {"store": settings.mongodb_db if settings.has_mongo else "local", "vectors": "zilliz" if settings.has_zilliz else "local"},
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
    steps = [
        {
            "id": "reasoning",
            "title": "Connect Nemotron via Nebius Token Factory",
            "done": status["nemotron"] == "connected",
            "detail": "Set NEBIUS_API_KEY in .env — without it Dobot runs in limited offline mode.",
        },
        {
            "id": "research",
            "title": "Connect Tavily for web research",
            "done": status["tavily"] == "connected",
            "detail": "Set TAVILY_API_KEY to enable source-aware research.",
        },
        {
            "id": "execution",
            "title": "Install the Hermes runtime for desktop control",
            "done": "available" in status["agent_runtime"],
            "detail": "Set AGENT_RUNTIME=cli once the Hermes CLI is installed.",
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
# Keys are persisted to the repo-root .env and hot-reloaded into the running process, so a packaged
# install (no terminal, no editor) can still be configured entirely from the Settings page.


class KeyValue(BaseModel):
    value: str


def _env_path(services: ServicesDep) -> Path:
    from app.config import REPO_ROOT

    return REPO_ROOT / ".env"


def _write_env_key(path: Path, name: str, value: str) -> None:
    """Create-or-update one KEY=VALUE line, preserving everything else in the file.

    ``name`` is the Settings field (snake_case); the .env convention is its upper-case form.
    """
    env_name = name.upper()
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    pattern = re.compile(rf"^\s*#?\s*{re.escape(env_name)}\s*=.*$")
    replacement = f"{env_name}={value}"
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    laya = getattr(jev, "laya", None)
    if laya is not None and hasattr(laya, "reset_http"):
        try:
            laya.reset_http()
        except Exception:  # noqa: BLE001
            pass


@router.get("/keys")
async def list_keys(services: ServicesDep) -> dict:
    """Masked presence of every manageable credential. Raw values never leave the process."""
    settings = services.settings
    keys = []
    for name, field in {**KEY_FIELDS, **URI_FIELDS}.items():
        value = str(getattr(settings, field, "") or "")
        keys.append(
            {
                "id": name,
                "field": field,
                "kind": "secret" if name in KEY_FIELDS else "uri",
                "set": bool(value.strip()),
                "masked": _mask(value) if value.strip() else "",
            }
        )
    return {"keys": keys}


@router.put("/keys/{name}")
async def set_key(name: str, payload: KeyValue, services: ServicesDep) -> dict:
    """Store a credential: persisted to .env and applied to the live process immediately."""
    name = name.strip().lower()
    field = KEY_FIELDS.get(name)
    kind = "secret"
    if field is None:
        field = URI_FIELDS.get(name)
        kind = "uri"
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
    path = _env_path(services)
    try:
        _write_env_key(path, field, value)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "ENV_WRITE_FAILED", "message": str(exc)[:200], "recoverable": False},
        ) from exc
    _apply_key(services, field, value)
    return {"ok": True, "id": name, "field": field, "kind": kind, "persisted": str(path)}


@router.delete("/keys/{name}")
async def clear_key(name: str, services: ServicesDep) -> dict:
    """Remove a credential from the live process (the .env line is blanked, not deleted)."""
    name = name.strip().lower()
    field = KEY_FIELDS.get(name) or URI_FIELDS.get(name)
    if field is None:
        raise HTTPException(status_code=404, detail={"code": "UNKNOWN_KEY", "message": name, "recoverable": True})
    path = _env_path(services)
    try:
        _write_env_key(path, field, "")
    except OSError as exc:
        raise HTTPException(status_code=500, detail={"code": "ENV_WRITE_FAILED", "message": str(exc)[:200], "recoverable": False}) from exc
    _apply_key(services, field, "")
    return {"ok": True, "id": name, "field": field}
