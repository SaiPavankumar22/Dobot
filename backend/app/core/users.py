"""Users and per-user API tokens.

Opt-in multi-user layer (``MULTI_USER=true``). Design constraints:

* **No new dependencies.** Passwords are pbkdf2-hmac-sha256 via ``hashlib``; tokens are
  ``secrets`` randoms stored only as SHA-256 hashes.
* **Legacy unchanged.** With multi-user off, this module is never consulted and the
  single shared ``DOBOT_API_TOKEN`` path behaves exactly as before.
* **First user is the owner.** Later registrations need an invite code the owner
  created, unless ``MULTI_USER_OPEN_REGISTRATION=true``.

Identity reaches the rest of the app through two contextvars (see ``app.core.tenancy``):
the current user id, which ``UserScopedStore`` turns into data isolation, and the
user's API-key overrides, which the model clients read per request.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from app.logging_setup import get_logger
from app.memory.store import RecordStore

logger = get_logger(__name__)

PBKDF2_ITERATIONS = 390_000
_TOKEN_PREFIX = "dobot_u_"
USERS = "users"
INVITES = "invites"


def _hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2-sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class User:
    id: str
    name: str
    role: str  # "owner" | "member"
    created_at: float = field(default_factory=time.time)

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> User:
        return cls(
            id=str(doc.get("id")), name=str(doc.get("name")), role=str(doc.get("role", "member")),
            created_at=float(doc.get("created_at", 0.0)),
        )


class UserManager:
    """Users, credentials and API tokens, persisted in the shared (unscoped) store."""

    def __init__(self, store: RecordStore) -> None:
        # The tenancy wrapper passes "users"/"invites" through unscoped, so this works
        # whether or not the outer store is user-scoped.
        self._store = store

    async def count(self) -> int:
        return await self._store.count(USERS)

    async def get(self, user_id: str) -> User | None:
        doc = await self._store.get(USERS, user_id)
        return User.from_doc(doc) if doc else None

    async def list_users(self) -> list[User]:
        docs = await self._store.all(USERS, limit=500)
        return [User.from_doc(doc) for doc in docs]

    async def register(self, name: str, password: str, *, invite_code: str = "") -> User:
        name = name.strip()
        if not name or len(password or "") < 8:
            raise ValueError("a name and a password of at least 8 characters are required")
        first = await self.count() == 0
        if not first and invite_code:
            invite = await self._store.get(INVITES, invite_code)
            if invite is None or invite.get("used"):
                raise ValueError("that invite code is not valid")
            await self._store.update(INVITES, invite_code, {"used": True})
        # The first account owns the deployment: only an owner may mint invites. Later
        # accounts are members (the endpoint enforces the invite requirement).
        user = User(id=f"user_{secrets.token_hex(6)}", name=name, role="owner" if first else "member")
        await self._store.insert(
            USERS,
            {
                "id": user.id,
                "name": user.name,
                "role": user.role,
                "created_at": user.created_at,
                "password": _hash_password(password),
            },
        )
        logger.info("registered user %s (%s)", user.name, user.role)
        return user

    async def authenticate(self, name: str, password: str) -> User | None:
        name = name.strip().lower()
        for doc in await self._store.all(USERS, limit=500):
            if str(doc.get("name", "")).lower() == name and _verify_password(
                password, str(doc.get("password", ""))
            ):
                return User.from_doc(doc)
        return None

    async def issue_token(self, user: User) -> str:
        """One live token per user: issuing a new one invalidates the old."""
        token = f"{_TOKEN_PREFIX}{user.id}_{secrets.token_urlsafe(24)}"
        doc = await self._store.get(USERS, user.id) or {}
        doc["token_hash"] = _hash_token(token)
        await self._store.insert(USERS, doc)
        return token

    async def resolve_token(self, token: str) -> User | None:
        token = (token or "").strip()
        if not token.startswith(_TOKEN_PREFIX):
            return None
        digest = _hash_token(token)
        for doc in await self._store.all(USERS, limit=500):
            stored = str(doc.get("token_hash") or "")
            if stored and hmac.compare_digest(stored, digest):
                return User.from_doc(doc)
        return None

    async def create_invite(self, created_by: str) -> str:
        code = secrets.token_urlsafe(9)
        await self._store.insert(
            INVITES, {"id": code, "created_by": created_by, "used": False, "created_at": time.time()}
        )
        return code
