"""MongoDB record-store management, without a real MongoDB.

What these tests pin, in the order the user meets it: set ``MONGODB_URI`` → the store
selection actually chooses Mongo (the driver is a core dependency now, not an opt-in
extra that silently misses) → existing local data — **including accounts** — migrates in
without ever rolling newer database state backwards → an unreachable server degrades to
the local files and the Doctor can see that, even under multi-user scoping.

Fakes implement the small ``RecordStore`` interface; nothing here opens a socket.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.memory import store as store_module
from app.memory.store import (
    COLLECTIONS,
    FallbackRecordStore,
    LocalFileStore,
    build_record_store,
    migrate_local_into,
)


class MemStore:
    """In-memory store implementing the RecordStore interface (stands in for Mongo)."""

    name = "mongodb"

    def __init__(self) -> None:
        self.data: dict[str, dict[str, dict[str, Any]]] = {}

    async def insert(self, collection: str, doc: dict[str, Any]) -> dict[str, Any]:
        doc_id = str(doc.get("id") or doc.get("_id"))
        self.data.setdefault(collection, {})[doc_id] = dict(doc)
        return doc

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        doc = self.data.get(collection, {}).get(doc_id)
        return dict(doc) if doc else None

    async def all(self, collection: str, limit: int = 500) -> list[dict[str, Any]]:
        return [dict(doc) for doc in list(self.data.get(collection, {}).values())[-limit:]]

    async def update(
        self, collection: str, doc_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        doc = self.data.get(collection, {}).get(doc_id)
        if doc is None:
            return None
        doc.update(patch)
        return dict(doc)

    async def update_many(self, collection: str, patches: dict[str, dict[str, Any]]) -> int:
        changed = 0
        for doc_id, patch in patches.items():
            if await self.update(collection, doc_id, patch) is not None:
                changed += 1
        return changed

    async def delete(self, collection: str, doc_id: str) -> bool:
        return self.data.get(collection, {}).pop(doc_id, None) is not None

    async def delete_many(self, collection: str, predicate: Any) -> int:
        docs = await self.all(collection, limit=10_000)
        doomed = [d["id"] for d in docs if predicate(d) and d.get("id")]
        for doc_id in doomed:
            self.data.get(collection, {}).pop(doc_id, None)
        return len(doomed)

    async def count(self, collection: str) -> int:
        return len(self.data.get(collection, {}))

    async def close(self) -> None:
        pass


class FakeMongo(MemStore):
    """MongoRecordStore stand-in: constructor shape + ping, no network."""

    made: list[tuple[str, str]] = []

    def __init__(self, uri: str, database: str) -> None:
        super().__init__()
        FakeMongo.made.append((uri, database))
        self.ping_ok = FakeMongo.next_ping
        self.closed = False
        self.uri = uri
        self.database = database

    next_ping = True

    async def ping(self) -> bool:
        return self.ping_ok

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake_mongo() -> Any:
    FakeMongo.made = []
    FakeMongo.next_ping = True
    yield


def _settings_with_uri(tmp_path: Path) -> Settings:
    from app.config import Settings as SettingsCls

    settings = SettingsCls()
    object.__setattr__(settings, "dobot_state_dir", str(tmp_path / "state"))
    object.__setattr__(settings, "mongodb_uri", "mongodb://user:pass@new-host:27017")
    object.__setattr__(settings, "mongodb_db", "dobot_users_test")
    return settings


# ------------------------------------------------------------------ selection


async def test_mongodb_uri_selects_mongo_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: MONGODB_URI set → records (and accounts) are served by Mongo."""
    monkeypatch.setattr(store_module, "MongoRecordStore", FakeMongo)
    settings = _settings_with_uri(tmp_path)
    store = await build_record_store(settings)
    assert isinstance(store, FallbackRecordStore)
    assert FakeMongo.made == [("mongodb://user:pass@new-host:27017", "dobot_users_test")]
    assert store.name == "mongodb", "name must show the physical primary before any failure"
    assert store.degraded is False


async def test_unreachable_server_falls_back_to_local_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed/wrong connection string must not stop the app — local files take over."""
    FakeMongo.next_ping = False
    monkeypatch.setattr(store_module, "MongoRecordStore", FakeMongo)
    store = await build_record_store(_settings_with_uri(tmp_path))
    assert isinstance(store, LocalFileStore)
    assert store.name == "local-file-store"


async def test_missing_driver_or_bad_uri_falls_back_to_local_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExplodingMongo:
        def __init__(self, _uri: str, _db: str) -> None:
            raise ImportError("No module named 'motor'")

    monkeypatch.setattr(store_module, "MongoRecordStore", ExplodingMongo)
    store = await build_record_store(_settings_with_uri(tmp_path))
    assert isinstance(store, LocalFileStore)


async def test_no_uri_keeps_the_local_store(tmp_path: Path) -> None:
    from app.config import Settings as SettingsCls

    settings = SettingsCls()
    object.__setattr__(settings, "dobot_state_dir", str(tmp_path / "state"))
    object.__setattr__(settings, "mongodb_uri", "")
    store = await build_record_store(settings)
    assert isinstance(store, LocalFileStore)
    assert FakeMongo.made == []  # nothing even constructed without a URI


# ------------------------------------------------------------------ migration


async def test_migration_carries_accounts_into_the_new_database(tmp_path: Path) -> None:
    """Users and invites are part of the migration set — a fresh DB receives them too."""
    assert "users" in COLLECTIONS and "invites" in COLLECTIONS
    local = LocalFileStore(tmp_path / "store")
    await local.insert("users", {"id": "user_ada", "name": "ada", "role": "owner"})
    await local.insert("invites", {"id": "code123", "created_by": "user_ada", "used": False})
    await local.insert("tasks", {"id": "task_1", "title": "legacy task"})

    primary = MemStore()
    moved = await migrate_local_into(primary, local)

    assert moved == 3
    assert await primary.get("users", "user_ada") == {"id": "user_ada", "name": "ada", "role": "owner"}
    assert await primary.get("invites", "code123") is not None
    assert await primary.get("tasks", "task_1") is not None


async def test_migration_is_idempotent_and_never_clobbers_newer_database_state(
    tmp_path: Path,
) -> None:
    """Runs every startup: stale local snapshots must not roll live database state back."""
    local = LocalFileStore(tmp_path / "store")
    # (a) stale local copy of a doc the database has moved past
    await local.insert(
        "tasks",
        {"id": "task_stale", "title": "old title", "updated_at": "2026-01-01T00:00:00+00:00"},
    )
    # (b) doc updated locally AFTER its database copy (outage write catching up)
    await local.insert(
        "memories",
        {"id": "mem_fresh", "content": "new", "created_at": "2026-02-01T00:00:00+00:00"},
    )
    primary = MemStore()
    await primary.insert(
        "tasks",
        {"id": "task_stale", "title": "live title", "updated_at": "2026-06-01T00:00:00+00:00"},
    )
    await primary.insert(
        "memories",
        {"id": "mem_fresh", "content": "old", "created_at": "2026-01-15T00:00:00+00:00"},
    )

    moved = await migrate_local_into(primary, local)
    assert moved == 1, "only the genuinely-newer local doc should count as merged"
    assert (await primary.get("tasks", "task_stale"))["title"] == "live title", (
        "a stale local snapshot rolled the database backwards"
    )
    assert (await primary.get("memories", "mem_fresh"))["content"] == "new"

    # Second run (the normal restart path): nothing changes, nothing errors.
    assert await migrate_local_into(primary, local) == 0
    assert (await primary.get("tasks", "task_stale"))["title"] == "live title"


async def test_migration_skips_docs_without_timestamps_when_primary_already_has_them() -> None:
    """No timestamp means no evidence of recency — the database wins (never guess)."""
    primary = MemStore()
    local = MemStore()
    await local.insert("settings", {"id": "user_keys", "keys": {"nebius_api_key": "local-one"}})
    await primary.insert("settings", {"id": "user_keys", "keys": {"nebius_api_key": "live-one"}})
    await migrate_local_into(primary, local)
    assert (await primary.get("settings", "user_keys"))["keys"]["nebius_api_key"] == "live-one"


# ------------------------------------------------------------------ degradation


async def test_fallback_store_rotates_and_reports_degradation() -> None:
    failing = MemStore()
    failing.name = "mongodb"

    async def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("connection reset")

    failing.insert = boom  # type: ignore[method-assign]
    real_local = MemStore()
    real_local.name = "local-file-store"

    store = FallbackRecordStore(failing, real_local)
    await store.insert("tasks", {"id": "t1", "title": "written during outage"})
    assert store.degraded is True
    assert store.name == "mongodb->local-file-store"
    assert await real_local.get("tasks", "t1") is not None


def test_scoped_store_exposes_physical_store_and_degradation() -> None:
    """Doctor/stats read `store.name`/`store.degraded` — the wrapper must not hide them."""
    from app.core.tenancy import UserScopedStore

    physical = MemStore()  # name = "mongodb"
    scoped = UserScopedStore(physical)
    assert scoped.name == "user-scoped(mongodb)"
    assert scoped.degraded is False

    # Simulate an already-degraded fallback chain as the wrapped base.
    chain = MemStore()
    chain.name = "mongodb->local-file-store"
    chained = UserScopedStore(chain)
    assert chained.name == "user-scoped(mongodb->local-file-store)"
