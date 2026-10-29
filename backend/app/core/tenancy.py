"""Tenancy: which user is asking, and what that isolates.

Two contextvars, both defaulting to "nobody" so every code path keeps working in
single-user mode:

* ``current_user_id`` — set by the auth middleware per request. ``UserScopedStore``
  prefixes every collection with ``u:<id>:`` so memory, tasks, approvals, activity,
  grants, journal and automations are per-user with no changes at any call site.
* ``user_key_overrides`` — the requesting user's own provider keys (stored per user
  by ``PUT /settings/keys`` in multi-user mode). The Nemotron and Tavily clients
  prefer these over the process-wide settings, so one deployment can serve people
  who each bring their own key without sharing it.

Background work (the scheduler) sets the same vars around each automation so a
scheduled task runs as its owner, not as "nobody".
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextvars import ContextVar
from typing import Any

current_user_id: ContextVar[str] = ContextVar("current_user_id", default="")
# None (not a shared dict — ContextVar defaults must be immutable): every writer sets a
# fresh dict (the middleware and the WS handshake both do), readers treat None as "no overrides".
user_key_overrides: ContextVar[dict[str, str] | None] = ContextVar("user_key_overrides", default=None)

#: Collections that are NOT per-user: identity itself, and the durable product-level
#: record of skills (shipped skills are part of the deployment, not a person's data).
UNSCOPED_COLLECTIONS = frozenset({"users", "invites"})


def scoped_name(uid: str, collection: str) -> str:
    """The physical name for a user's collection.

    ``__`` rather than ``:`` because the file store maps one collection to one
    ``<name>.json`` file and colons are illegal in Windows filenames.
    """
    if not uid or collection in UNSCOPED_COLLECTIONS:
        return collection
    return f"u__{uid}__{collection}"


def scoped_collection(collection: str) -> str:
    return scoped_name(current_user_id.get(), collection)


def override_key(field: str, process_value: str) -> str:
    """The value a client should use: the asking user's key, else the process setting."""
    overrides = user_key_overrides.get() or {}
    value = overrides.get(field)
    return value if value else process_value


class UserScopedStore:
    """Prefix collections with the current user. Degrades to passthrough when nobody."""

    def __init__(self, base: Any) -> None:
        self._base = base

    @property
    def name(self) -> str:
        # The scoping layer must never hide which physical store is in use.
        # is serving reads (mongo vs local-file-store); the startup log, /memory/stats and
        # the Doctor all read this.
        return f"user-scoped({getattr(self._base, 'name', '?')})"

    @property
    def degraded(self) -> bool:
        # Doctor checks `store.degraded` to explain a storage outage; without this proxy it
        # silently reported healthy whenever multi-user wrapping was active.
        return bool(getattr(self._base, "degraded", False))

    # --- identity passthrough ------------------------------------------------
    async def insert(self, collection: str, doc: dict[str, Any]) -> dict[str, Any]:
        return await self._base.insert(scoped_collection(collection), doc)

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        return await self._base.get(scoped_collection(collection), doc_id)

    async def all(self, collection: str, limit: int = 500) -> list[dict[str, Any]]:
        return await self._base.all(scoped_collection(collection), limit)

    async def update(self, collection: str, doc_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        return await self._base.update(scoped_collection(collection), doc_id, patch)

    async def update_many(self, collection: str, patches: dict[str, dict[str, Any]]) -> int:
        return await self._base.update_many(scoped_collection(collection), patches)

    async def delete(self, collection: str, doc_id: str) -> bool:
        return await self._base.delete(scoped_collection(collection), doc_id)

    async def delete_many(self, collection: str, predicate: Callable[[dict[str, Any]], bool]) -> int:
        return await self._base.delete_many(scoped_collection(collection), predicate)

    async def count(self, collection: str) -> int:
        return await self._base.count(scoped_collection(collection))

    async def close(self) -> None:
        await self._base.close()


def as_user(user_id: str) -> Any:
    """Token for running background work under a specific user's scope."""
    return _ScopeToken(user_id)


class _ScopeToken:
    def __init__(self, user_id: str) -> None:
        self._user_id = user_id
        self._tokens: list[Any] = []

    def __enter__(self) -> _ScopeToken:
        self._tokens.append(current_user_id.set(self._user_id))
        return self

    def __exit__(self, *_exc: object) -> None:
        while self._tokens:
            current_user_id.reset(self._tokens.pop())
        return None


def iter_user_ids(users: Iterable[Any]) -> list[str]:
    return [str(getattr(user, "id", "")) for user in users]
