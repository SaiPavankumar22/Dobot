"""Multi-user auth endpoints (opt-in via ``MULTI_USER=true``).

``POST /auth/register`` — the first account becomes the owner; later ones need an
owner invite (or ``MULTI_USER_OPEN_REGISTRATION=true``). ``POST /auth/login`` returns
a per-user API token that is presented as ``Authorization: Bearer dobot_u_...``.
The password never leaves the process: only its pbkdf2 hash is stored, and tokens
are stored as SHA-256 hashes — a leaked database still has no usable credentials.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import ServicesDep
from app.core.users import UserManager

router = APIRouter(prefix="/auth", tags=["auth"])


def _manager(services: ServicesDep) -> UserManager:
    if services.user_manager is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "MULTI_USER_DISABLED",
                "message": "set MULTI_USER=true to enable accounts (single-user mode is the default)",
                "recoverable": True,
            },
        )
    return services.user_manager


class RegisterRequest(BaseModel):
    name: str
    password: str
    invite_code: str = ""


class LoginRequest(BaseModel):
    name: str
    password: str


@router.post("/register")
async def register(payload: RegisterRequest, services: ServicesDep) -> dict:
    manager = _manager(services)
    if not services.settings.multi_user_open_registration and await manager.count() > 0 and not payload.invite_code:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "INVITE_REQUIRED",
                "message": "this Dobot has an owner already — ask them for an invite code",
                "recoverable": True,
            },
        )
    try:
        user = await manager.register(payload.name, payload.password, invite_code=payload.invite_code)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "REGISTRATION_REJECTED", "message": str(exc), "recoverable": True}
        ) from exc
    token = await manager.issue_token(user)
    return {"user": {"id": user.id, "name": user.name, "role": user.role}, "token": token}


@router.post("/login")
async def login(payload: LoginRequest, services: ServicesDep) -> dict:
    manager = _manager(services)
    user = await manager.authenticate(payload.name, payload.password)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "BAD_CREDENTIALS", "message": "no such user or wrong password", "recoverable": True},
        )
    token = await manager.issue_token(user)
    return {"user": {"id": user.id, "name": user.name, "role": user.role}, "token": token}


@router.get("/me")
async def me(services: ServicesDep) -> dict:
    from app.core.tenancy import current_user_id

    uid = current_user_id.get()
    if not uid or services.user_manager is None:
        return {"user": None, "multi_user": bool(services.settings.multi_user)}
    user = await services.user_manager.get(uid)
    return {
        "user": {"id": user.id, "name": user.name, "role": user.role} if user else {"id": uid},
        "multi_user": bool(services.settings.multi_user),
    }


@router.post("/invites")
async def create_invite(services: ServicesDep) -> dict:
    from app.core.tenancy import current_user_id

    manager = _manager(services)
    uid = current_user_id.get()
    if not uid:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_REQUIRED", "message": "authenticate to create invites", "recoverable": True}
        )
    user = await manager.get(uid)
    if user is None or user.role != "owner":
        raise HTTPException(
            status_code=403,
            detail={"code": "OWNER_ONLY", "message": "only the owner can create invite codes", "recoverable": True}
        )
    code = await manager.create_invite(user.id)
    return {"invite_code": code}
