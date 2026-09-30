"""Permission grants — the user's own answer to a restricted action.

A policy that refuses everything is safe and useless: the person who owns the machine eventually
needs to move a file into a folder Dobot did not put on the allowlist. The old behaviour was a hard
refusal; this module turns refusal into a *question* with two honest answers:

* **allow once** — a short-lived, in-memory grant so the current attempt can finish without being
  asked again halfway through (and without surviving a restart);
* **always allow** — a persisted grant for a folder scope, revocable from the Security page.

Grants are only ever produced for policies marked ``grantable``. The file guard (credential
locations), destructive commands, shell evasion and system directories are *not* grantable, so no
approval, and no grant, can loosen them — asking is a courtesy, not a bypass.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from app.events import EventBus, get_event_bus
from app.logging_setup import get_logger
from app.memory.store import RecordStore
from app.schemas import PermissionGrant, utcnow

logger = get_logger(__name__)

#: Collection the persisted (lifetime) grants live in.
COLLECTION = "grants"

#: How long an "allow once" grant stays valid: long enough for the paused task to resume and its
#: tool calls to run, short enough that it is not a permission the user forgot they gave.
ONCE_TTL_SECONDS = 900


def _covered(root: str, path: Path) -> bool:
    try:
        path.relative_to(Path(root))
        return True
    except ValueError:
        return False


class GrantStore:
    """In-memory view of every active grant, backed by the record store for lifetime ones.

    Both consumers are synchronous lookups by design: the decision engine asks before it authorises a
    step, and the sandbox's ``check_path`` asks on the hot path of every tool call. Nothing here does
    I/O per check — persistence happens on add/revoke only.
    """

    def __init__(
        self,
        store: RecordStore | None = None,
        bus: EventBus | None = None,
        *,
        once_ttl_seconds: int = ONCE_TTL_SECONDS,
    ) -> None:
        self._store = store
        self._bus = bus
        self._once_ttl = once_ttl_seconds
        self._grants: dict[str, PermissionGrant] = {}
        self._lock = asyncio.Lock()
        self.loaded = store is None

    # ------------------------------------------------------------------ lifecycle

    async def load(self) -> int:
        """Seed the cache from persisted grants. Safe to call more than once."""
        if self._store is None:
            self.loaded = True
            return 0
        async with self._lock:
            docs = await self._store.all(COLLECTION, limit=500)
            for doc in docs:
                try:
                    grant = PermissionGrant.model_validate(doc)
                except Exception:  # noqa: BLE001 - a corrupt grant must not stop startup
                    logger.warning("skipping unreadable permission grant %s", doc.get("id"))
                    continue
                if grant.expired:
                    continue
                self._grants[grant.id] = grant
            self.loaded = True
        logger.info("loaded %d lifetime permission grants", len(self._grants))
        return len(self._grants)

    # ------------------------------------------------------------------ queries

    def _prune(self) -> None:
        for grant_id in [g.id for g in self._grants.values() if g.expired]:
            self._grants.pop(grant_id, None)

    def active(self) -> list[PermissionGrant]:
        self._prune()
        grants = sorted(self._grants.values(), key=lambda item: item.created_at, reverse=True)
        return grants

    def covering(self, roots: list[str], policy: str) -> PermissionGrant | None:
        """The grant that covers *every* requested root for this policy, if one exists.

        All-or-nothing on purpose: half a move being permitted is worse than asking again.
        """
        if not roots:
            return None
        for grant in self.active():
            if grant.policy != policy:
                continue
            if all(any(_covered(root, Path(target)) for root in grant.roots) for target in roots):
                return grant
        return None

    def allows_path(self, path: Path, *, policy: str = "path_outside_sandbox") -> bool:
        """Sandbox-side check: is this path inside a remembered permission?"""
        return self.covering([str(path)], policy) is not None

    def summary(self) -> list[dict]:
        return [
            {
                "id": grant.id,
                "policy": grant.policy,
                "roots": grant.roots,
                "description": grant.description,
                "kind": grant.kind,
                "created_at": grant.created_at.isoformat(),
                "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
                "source": grant.source,
            }
            for grant in self.active()
        ]

    # ------------------------------------------------------------------ mutation

    async def add(
        self,
        *,
        policy: str,
        roots: list[str],
        description: str = "",
        scope: str = "once",
        task_id: str = "",
        source: str = "",
    ) -> PermissionGrant:
        kind = "lifetime" if scope == "lifetime" else "once"
        grant = PermissionGrant(
            policy=policy,
            roots=[str(root) for root in roots],
            description=description,
            kind=kind,
            task_id=task_id,
            source=source,
            expires_at=None if kind == "lifetime" else utcnow() + timedelta(seconds=self._once_ttl),
        )
        async with self._lock:
            self._grants[grant.id] = grant
        if kind == "lifetime" and self._store is not None:
            await self._store.insert(COLLECTION, grant.model_dump(mode="json"))
        logger.info(
            "%s permission granted for %s (%s)", kind, ", ".join(grant.roots), policy
        )
        if self._bus is not None:
            from app.events import EventType

            await self._bus.emit(
                EventType.NOTIFICATION,
                message=(
                    f"{kind} permission for {description or ', '.join(grant.roots)} — "
                    "manage it on the Security page"
                ),
                title="Permission remembered",
                body=(
                    f"{kind.capitalize()} permission for "
                    f"{description or ', '.join(grant.roots)} — revoke it on the Security page."
                ),
                scope=grant.kind,
                roots=grant.roots,
                policy=grant.policy,
            )
        return grant

    async def revoke(self, grant_id: str) -> bool:
        async with self._lock:
            existed = self._grants.pop(grant_id, None) is not None
        if self._store is not None:
            existed = await self._store.delete(COLLECTION, grant_id) or existed
        if existed:
            logger.info("permission grant %s revoked", grant_id)
        return existed


def build_grant_store(store: RecordStore | None, bus: EventBus | None = None) -> GrantStore:
    return GrantStore(store, bus or get_event_bus())
