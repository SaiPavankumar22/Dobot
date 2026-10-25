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
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger

logger = get_logger(__name__)
SERVICE = "Dobot"


def _key(path: Path) -> bytes:
    identity = f"{getpass.getuser()}|{platform.node()}|{path.parent.resolve()}".encode()
    return base64.urlsafe_b64encode(hashlib.sha256(identity).digest())


def _fallback_path(state_dir: Path) -> Path:
    return state_dir / "secrets.enc"


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
        from cryptography.fernet import Fernet

        values = json.loads(Fernet(_key(path)).decrypt(path.read_bytes()))
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
            from cryptography.fernet import Fernet

            values = json.loads(Fernet(_key(path)).decrypt(path.read_bytes()))
        except Exception:  # noqa: BLE001
            values = {}
    values[name] = value
    from cryptography.fernet import Fernet

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(Fernet(_key(path)).encrypt(json.dumps(values).encode()))
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
        from cryptography.fernet import Fernet

        values = json.loads(Fernet(_key(path)).decrypt(path.read_bytes()))
        values.pop(name, None)
        path.write_bytes(Fernet(_key(path)).encrypt(json.dumps(values).encode()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("encrypted local secret delete failed: %s", exc)


def hydrate(settings: Any) -> None:
    """Load local keys only when environment configuration did not provide one."""
    for field in ("nebius_api_key", "tavily_api_key", "novita_api_key", "zilliz_token", "zilliz_uri"):
        configured = str(getattr(settings, field, "") or "").strip()
        stored = get(field, settings.state_dir)
        if configured and not stored:
            try:
                set_value(field, configured, settings.state_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not migrate %s into local secret storage: %s", field, exc)
        elif not configured and stored:
            setattr(settings, field, stored)
