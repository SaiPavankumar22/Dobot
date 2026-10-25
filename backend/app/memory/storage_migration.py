"""Safe one-time migration from the legacy JSON store to SQLite."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger
from app.memory.sqlite_store import SQLiteRecordStore

logger = get_logger(__name__)


async def migrate_json_store(
    json_directory: Path,
    sqlite: SQLiteRecordStore,
    *,
    backup_directory: Path,
) -> dict[str, Any]:
    """Import legacy collections without overwriting newer SQLite records.

    A marker makes repeated starts cheap and a complete backup is taken before the first write.
    Invalid collections are reported rather than preventing Dobot from starting.
    """
    files = sorted(json_directory.glob("*.json")) if json_directory.exists() else []
    marker = sqlite.path.with_suffix(".json-migration-v1")
    if marker.exists() or not files:
        return {"migrated": 0, "skipped": 0, "backup": "", "already_complete": marker.exists()}

    backup = backup_directory / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(json_directory, backup)
    migrated = 0
    skipped = 0
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            documents = raw.items() if isinstance(raw, dict) else ()
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not migrate %s (%s)", path.name, exc)
            skipped += 1
            continue
        for key, value in documents:
            if not isinstance(value, dict):
                skipped += 1
                continue
            document = dict(value)
            document.setdefault("id", str(key))
            if await sqlite.get(path.stem, str(document["id"])) is None:
                await sqlite.insert(path.stem, document)
                migrated += 1
    marker.write_text(
        json.dumps({"version": 1, "backup": str(backup), "migrated": migrated}, indent=2),
        encoding="utf-8",
    )
    return {"migrated": migrated, "skipped": skipped, "backup": str(backup), "already_complete": False}
