"""Local secret storage.

Packaged Windows builds use the OS Credential Manager through ``keyring``. Development and
non-Windows environments use an encrypted file under the Dobot state directory as a fallback.
Raw values never enter SQLite, logs, or API responses.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import platform
import secrets
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger

logger = get_logger(__name__)
SERVICE = "Dobot"


def _legacy_key(path: Path) -> bytes:
    identity = f"{getpass.getuser()}|{platform.node()}|{path.parent.resolve()}".encode()
    return base64.urlsafe_b64encode(hashlib.sha256(identity).digest())


def _fallback_path(state_dir: Path) -> Path:
    return state_dir / "secrets.enc"


def _fallback_key_path(state_dir: Path) -> Path:
    return state_dir / "secrets.key"


def _fallback_key(state_dir: Path) -> bytes:
    """Load a random fallback key, creating it with restrictive permissions.

    The key is intentionally separate from ``secrets.enc``. Existing deterministic files are
    accepted once for migration and immediately rewritten with this random key.
    """
    path = _fallback_key_path(state_dir)
    if path.exists():
        key = path.read_bytes().strip()
        if len(key) == 44:
            return key
    key = base64.urlsafe_b64encode(secrets.token_bytes(32))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def _read_values(path: Path, state_dir: Path) -> dict[str, Any]:
    from cryptography.fernet import Fernet

    encrypted = path.read_bytes()
    try:
        return json.loads(Fernet(_fallback_key(state_dir)).decrypt(encrypted))
    except Exception:
        # One-time compatibility path for files created before random fallback keys.
        values = json.loads(Fernet(_legacy_key(path)).decrypt(encrypted))
        path.write_bytes(Fernet(_fallback_key(state_dir)).encrypt(json.dumps(values).encode()))
        return values


def _write_values(path: Path, state_dir: Path, values: dict[str, Any]) -> None:
    from cryptography.fernet import Fernet

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(Fernet(_fallback_key(state_dir)).encrypt(json.dumps(values).encode()))
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _keyring():
    try:
        import keyring

        return keyring
    except Exception:  # noqa: BLE001 - optional in minimal development environments
        return None


def get(name: str, state_dir: Path) -> str:
    backend = _keyring()
    if backend is not None:
        try:
            return str(backend.get_password(SERVICE, name) or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("credential manager read failed: %s", exc)
    path = _fallback_path(state_dir)
    if not path.exists():
        return ""
    try:
        values = _read_values(path, state_dir)
        return str(values.get(name, ""))
    except Exception as exc:  # noqa: BLE001
        logger.warning("encrypted local secret read failed: %s", exc)
        return ""


def set_value(name: str, value: str, state_dir: Path) -> str:
    backend = _keyring()
    if backend is not None:
        try:
            backend.set_password(SERVICE, name, value)
            return "credential-manager"
        except Exception as exc:  # noqa: BLE001
            logger.warning("credential manager write failed: %s", exc)
    path = _fallback_path(state_dir)
    values: dict[str, Any] = {}
    if path.exists():
        try:
            values = _read_values(path, state_dir)
        except Exception:  # noqa: BLE001
            values = {}
    values[name] = value
    _write_values(path, state_dir, values)
    return "encrypted-file"


def delete(name: str, state_dir: Path) -> None:
    backend = _keyring()
    if backend is not None:
        try:
            backend.delete_password(SERVICE, name)
        except Exception:
            pass
    path = _fallback_path(state_dir)
    if not path.exists():
        return
    try:
        values = _read_values(path, state_dir)
        values.pop(name, None)
        _write_values(path, state_dir, values)
    except Exception as exc:  # noqa: BLE001
        logger.warning("encrypted local secret delete failed: %s", exc)


def hydrate(settings: Any) -> None:
    """Load local keys only when environment configuration did not provide one."""
    for field in ("nebius_api_key", "tavily_api_key", "novita_api_key"):
        configured = str(getattr(settings, field, "") or "").strip()
        stored = get(field, settings.state_dir)
        if configured and not stored:
            try:
                set_value(field, configured, settings.state_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not migrate %s into local secret storage: %s", field, exc)
        elif not configured and stored:
            setattr(settings, field, stored)
