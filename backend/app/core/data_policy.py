"""Data classification used before choosing a local or remote model."""

from __future__ import annotations

import re
from enum import StrEnum


class DataClass(StrEnum):
    PUBLIC = "public"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"
    SECRET = "secret"


_SECRET = re.compile(r"(?i)(api[_-]?key|token|password|secret|private[_-]?key|authorization)\s*[:=]")
_SENSITIVE = re.compile(r"(?i)(\.env|credential|cookie|session|ssn|passport|bank|medical)")


def classify(text: str) -> DataClass:
    """Conservative text-only classifier; callers should combine it with file/screen metadata."""
    value = str(text or "")
    if _SECRET.search(value):
        return DataClass.SECRET
    if _SENSITIVE.search(value):
        return DataClass.SENSITIVE
    if len(value) > 0:
        return DataClass.PERSONAL
    return DataClass.PUBLIC


def allows_remote(data_class: DataClass, *, policy: str = "hybrid") -> bool:
    policy = policy.strip().lower()
    if policy in {"local", "local_only", "never_upload"}:
        return False
    return data_class not in {DataClass.SECRET, DataClass.SENSITIVE}
