"""Filesystem tools.

Every path is resolved and checked against the sandbox allowlist before anything touches disk. Reads
are cheap and reversible; writes record a content hash so the verifier can prove the bytes landed;
deletes are HIGH risk and always go through approval.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from pathlib import Path
from typing import Any

from app.schemas import (
    CheckResult,
    ExecutionResult,
    RiskLevel,
    VerificationResult,
)
from app.tools.base import Tool, ToolContext

MAX_READ_BYTES = 2_000_000
MAX_LIST_ENTRIES = 500

#: Buckets used to summarise a bulk action for the approval dialog.
CATEGORY_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("screenshots", (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")),
    ("documents", (".pdf", ".docx", ".doc", ".txt", ".md", ".xlsx", ".csv", ".pptx")),
    ("installers", (".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm")),
    ("archives", (".zip", ".tar", ".gz", ".7z", ".rar")),
    ("media", (".mp4", ".mov", ".mkv", ".mp3", ".wav", ".flac")),
    ("code", (".py", ".ts", ".tsx", ".js", ".rs", ".go", ".java", ".json", ".yaml", ".yml")),
]


def _hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _categorise(paths: list[Path]) -> dict[str, int]:
    buckets: Counter[str] = Counter()
    for path in paths:
        suffix = path.suffix.lower()
        for label, extensions in CATEGORY_PATTERNS:
            if suffix in extensions:
                buckets[label] += 1
                break
        else:
            buckets["other/unknown"] += 1
    return dict(buckets)


def _human_size(total: int) -> str:
    size = float(total)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def _collect_targets(params: dict[str, Any], ctx: ToolContext) -> list[Path]:
    """Resolve the paths a bulk action refers to (for previews and counts).

    Handles globs as well as directories, because plans usually name ``~/Downloads/*`` rather than an
    enumerated file list.
    """
    raw = params.get("path") or params.get("directory")
    patterns = params.get("patterns") or []
    if not raw and not patterns:
        return []
    targets: list[Path] = []
    candidates: list[str] = [str(raw)] if raw else []
    candidates.extend(str(pattern) for pattern in patterns)
    for candidate in candidates:
        expanded = Path(candidate).expanduser()
        if any(char in candidate for char in "*?["):
            if expanded.parent.exists():
                targets.extend(sorted(path for path in expanded.parent.glob(expanded.name) if path.exists()))
        elif expanded.is_file():
            targets.append(expanded)
        elif expanded.exists():
            targets.extend(
                sorted(path for path in expanded.iterdir() if not path.name.startswith("."))
            )
    unique: list[Path] = []
    for path in targets:
        if path not in unique:
            unique.append(path)
    return unique


class FsListTool(Tool):
    name = "fs_list"
    description = "List files in a directory inside the allowed workspace, with sizes and modified times."
    risk_floor = RiskLevel.LOW
    proactive_ok = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list"},
            "glob": {"type": "string", "description": "Optional glob filter, e.g. *.pdf"},
        },
        "required": ["path"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        raw = self.require(params, "path")
        assert ctx.sandbox is not None
        try:
            path = await ctx.sandbox.check_path(raw)
        except Exception as exc:  # noqa: BLE001
            return self.fail(self.name, str(exc), started)
        if not path.exists():
            return self.fail(self.name, f"{path} does not exist", started)
        pattern = params.get("glob") or "*"
        entries: list[dict[str, Any]] = []
        for item in sorted(path.glob(str(pattern))):
            try:
                stat = item.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": item.name,
                    "path": str(item),
                    "is_dir": item.is_dir(),
                    "size": stat.st_size,
                    "modified": int(stat.st_mtime),
                }
            )
            if len(entries) >= MAX_LIST_ENTRIES:
                break
        return self.ok(self.name, {"path": str(path), "count": len(entries), "entries": entries}, started)

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        checks = [
            CheckResult(name="directory_readable", passed=result.ok),
            CheckResult(name="entries_returned", passed=output.get("count", 0) > 0
                        or output.get("count", 0) == 0, detail=f"{output.get('count', 0)} entries"),
        ]
        return VerificationResult(verified=result.ok, checks=checks)


class FsReadTool(Tool):
    name = "fs_read"
    description = "Read a text file inside the allowed workspace."
    risk_floor = RiskLevel.LOW
    proactive_ok = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_chars": {"type": "integer", "default": 20000},
        },
        "required": ["path"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        raw = self.require(params, "path")
        assert ctx.sandbox is not None
        try:
            path = await ctx.sandbox.check_path(raw)
        except Exception as exc:  # noqa: BLE001
            return self.fail(self.name, str(exc), started)
        if not path.exists() or not path.is_file():
            return self.fail(self.name, f"{path} is not a readable file", started)
        if path.stat().st_size > MAX_READ_BYTES:
            return self.fail(self.name, f"{path} is larger than {MAX_READ_BYTES} bytes", started)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return self.fail(self.name, f"read failed: {exc}", started)
        limit = int(params.get("max_chars", 20000) or 20000)
        return self.ok(
            self.name,
            {
                "path": str(path),
                "content": content[:limit],
                "truncated": len(content) > limit,
                "size": path.stat().st_size,
                "sha256": _hash_bytes(content.encode("utf-8", errors="replace")),
            },
            started,
        )

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        path = Path(str(output.get("path", "")))
        checks = [
            CheckResult(name="file_exists", passed=path.exists()),
            CheckResult(name="content_returned", passed=bool(output.get("content") or result.ok)),
        ]
        return VerificationResult(verified=all(check.passed for check in checks), checks=checks)


class FsWriteTool(Tool):
    name = "fs_write"
    description = "Create or overwrite a text file inside the allowed workspace."
    risk_floor = RiskLevel.MEDIUM
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "overwrite": {"type": "boolean", "default": False},
            "create_parents": {"type": "boolean", "default": True},
        },
        "required": ["path", "content"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        raw = self.require(params, "path")
        content = str(params.get("content", ""))
        overwrite = bool(params.get("overwrite", False))
        assert ctx.sandbox is not None
        try:
            path = await ctx.sandbox.check_path(raw, write=True)
        except Exception as exc:  # noqa: BLE001
            return self.fail(self.name, str(exc), started)
        if path.exists() and not overwrite:
            return self.fail(
                self.name,
                f"{path} already exists; pass overwrite=true to replace it",
                started,
            )
        existed_before = path.exists()
        try:
            if params.get("create_parents", True):
                path.parent.mkdir(parents=True, exist_ok=True)
            payload = content.encode("utf-8")
            path.write_bytes(payload)
        except OSError as exc:
            return self.fail(self.name, f"write failed: {exc}", started)
        return self.ok(
            self.name,
            {
                "path": str(path),
                "bytes": len(content.encode("utf-8")),
                "sha256": _hash_bytes(content.encode("utf-8")),
                "replaced_existing": existed_before,
            },
            started,
            side_effects=[str(path)],
        )

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        path = Path(str(output.get("path", "")))
        expected_hash = output.get("sha256")
        checks: list[CheckResult] = [
            CheckResult(name="file_exists", passed=path.exists(), detail=str(path)),
        ]
        if path.exists():
            try:
                actual = path.read_bytes()
                checks.append(CheckResult(name="file_readable", passed=True, detail=f"{len(actual)} bytes"))
                checks.append(
                    CheckResult(
                        name="content_matches",
                        passed=_hash_bytes(actual) == expected_hash,
                        detail="sha256 comparison",
                    )
                )
            except OSError as exc:
                checks.append(CheckResult(name="file_readable", passed=False, detail=str(exc)))
        return VerificationResult(
            verified=all(check.passed for check in checks),
            checks=checks,
            notes="verified on disk, not from the writer's own report",
        )


class FsMkdirTool(Tool):
    name = "fs_mkdir"
    description = "Create a directory inside the allowed workspace."
    risk_floor = RiskLevel.MEDIUM
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        raw = self.require(params, "path")
        assert ctx.sandbox is not None
        try:
            path = await ctx.sandbox.check_path(raw, write=True)
            path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return self.fail(self.name, str(exc), started)
        return self.ok(self.name, {"path": str(path), "created": True}, started, side_effects=[str(path)])

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        path = Path(str((result.output or {}).get("path", "")))
        checks = [CheckResult(name="directory_exists", passed=path.is_dir())]
        return VerificationResult(verified=path.is_dir(), checks=checks)


class FsMoveTool(Tool):
    name = "fs_move"
    description = "Move or rename files inside the allowed workspace. Supports a source glob."
    risk_floor = RiskLevel.MEDIUM
    parameters = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "File, directory or glob to move"},
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Several globs to move in one step, e.g. ['~/Downloads/*.png']",
            },
            "destination": {"type": "string", "description": "Target directory"},
            "on_conflict": {"type": "string", "enum": ["skip", "rename", "overwrite"], "default": "rename"},
        },
        "required": ["destination"],
    }

    @staticmethod
    def _expand(pattern: str) -> list[Path]:
        expanded = Path(pattern).expanduser()
        if any(char in pattern for char in "*?["):
            if not expanded.parent.exists():
                return []
            return sorted(expanded.parent.glob(expanded.name))
        return [expanded] if expanded.exists() else []

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        raw_sources: list[str] = []
        if params.get("source"):
            raw_sources.append(str(params["source"]))
        raw_sources.extend(str(item) for item in (params.get("sources") or []))
        dest_raw = self.require(params, "destination")
        if not raw_sources:
            return self.fail(self.name, "no source or sources supplied", started)
        on_conflict = str(params.get("on_conflict", "rename"))
        assert ctx.sandbox is not None
        try:
            destination = await ctx.sandbox.check_path(dest_raw, write=True)
            for pattern in raw_sources:
                await ctx.sandbox.check_path(pattern, write=True)
        except Exception as exc:  # noqa: BLE001
            return self.fail(self.name, str(exc), started)

        candidates: list[Path] = []
        for pattern in raw_sources:
            for path in self._expand(pattern):
                if path.is_dir():
                    continue
                if path not in candidates:
                    candidates.append(path)

        if not candidates:
            return self.fail(self.name, f"nothing matched {', '.join(raw_sources)}", started)

        destination.mkdir(parents=True, exist_ok=True)
        moved: list[str] = []
        skipped: list[str] = []
        for item in candidates:
            ctx.check_cancelled()
            target = destination / item.name
            if target.exists():
                if on_conflict == "skip":
                    skipped.append(str(item))
                    continue
                if on_conflict == "rename":
                    stem, suffix, counter = item.stem, item.suffix, 1
                    while target.exists():
                        target = destination / f"{stem} ({counter}){suffix}"
                        counter += 1
            try:
                item.rename(target)
                moved.append(str(target))
            except OSError as exc:
                skipped.append(f"{item}: {exc}")
        return self.ok(
            self.name,
            {"moved": moved, "skipped": skipped, "count": len(moved)},
            started,
            side_effects=moved,
        )

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        moved = [Path(path) for path in output.get("moved", [])]
        checks = [
            CheckResult(name="destinations_exist", passed=all(path.exists() for path in moved),
                        detail=f"{len(moved)} moved"),
            CheckResult(name="files_moved", passed=bool(moved), detail=f"{len(output.get('skipped', []))} skipped"),
        ]
        return VerificationResult(verified=all(check.passed for check in checks), checks=checks)

    async def preview(self, params: dict[str, Any], ctx: ToolContext) -> list[str]:
        patterns = ([str(params.get("source"))] if params.get("source") else []) + [
            str(item) for item in (params.get("sources") or [])
        ]
        candidates: list[Path] = []
        for pattern in patterns:
            candidates.extend(self._expand(pattern))
        buckets = _categorise(candidates)
        total_size = sum(path.stat().st_size for path in candidates if path.exists())
        lines = [f"{len(candidates)} file(s), {_human_size(total_size)}"]
        lines.extend(f"• {count} {label}" for label, count in sorted(buckets.items(), key=lambda kv: -kv[1]))
        lines.append(f"→ {params.get('destination')}")
        return lines


class FsDeleteTool(Tool):
    name = "fs_delete"
    description = (
        "Delete files inside the allowed workspace. Always requires user approval. "
        "Prefer moving files to an archive folder over deleting when the user has not asked for deletion."
    )
    risk_floor = RiskLevel.HIGH
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File, directory or glob to delete"},
            "recursive": {"type": "boolean", "default": False},
            "reason": {"type": "string"},
        },
        "required": ["path"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        raw = self.require(params, "path")
        recursive = bool(params.get("recursive", False))
        assert ctx.sandbox is not None
        try:
            path = await ctx.sandbox.check_path(raw, write=True)
        except Exception as exc:  # noqa: BLE001
            return self.fail(self.name, str(exc), started)

        if any(char in raw for char in "*?[") and path.parent.exists():
            targets = sorted(path.parent.glob(path.name))
        elif path.exists():
            targets = [path]
        else:
            return self.fail(self.name, f"{path} does not exist", started)

        deleted: list[str] = []
        errors: list[str] = []
        for target in targets:
            ctx.check_cancelled()
            try:
                if target.is_dir():
                    if not recursive and any(target.iterdir()):
                        errors.append(f"{target}: directory not empty (pass recursive=true)")
                        continue
                    for child in sorted(target.rglob("*"), reverse=True):
                        child.unlink() if child.is_file() else child.rmdir()
                    target.rmdir()
                else:
                    target.unlink()
                deleted.append(str(target))
            except OSError as exc:
                errors.append(f"{target}: {exc}")
        return self.ok(
            self.name,
            {"deleted": deleted, "count": len(deleted), "errors": errors},
            started,
            side_effects=deleted,
        )

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        deleted = [Path(path) for path in output.get("deleted", [])]
        checks = [
            CheckResult(name="targets_gone", passed=all(not path.exists() for path in deleted),
                        detail=f"{len(deleted)} removed"),
            CheckResult(
                name="no_errors",
                passed=not output.get("errors"),
                detail="; ".join(output.get("errors", [])[:3]),
            ),
        ]
        return VerificationResult(verified=all(check.passed for check in checks), checks=checks)

    async def preview(self, params: dict[str, Any], ctx: ToolContext) -> list[str]:
        targets = _collect_targets(params, ctx)
        buckets = _categorise(targets)
        total_size = 0
        for path in targets:
            try:
                total_size += path.stat().st_size
            except OSError:
                continue
        lines = [
            f"{len(targets)} file(s), {_human_size(total_size)} would be permanently deleted"
        ]
        lines.extend(f"✓ {count} {label}" for label, count in sorted(buckets.items(), key=lambda kv: -kv[1]))
        if buckets.get("other/unknown"):
            lines.append(f"⚠ {buckets['other/unknown']} unrecognised file(s) need review before deletion")
        return lines
