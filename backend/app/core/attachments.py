"""Attachments: what may be attached to a chat message, and how big it may be.

Two kinds are supported, both on purpose:

* **Images** ride the same path as a selected screen region — the configured vision model
  (``NEMOTRON_VISION_MODEL``) reads them, and the description becomes context.
* **Text documents** are decoded and inlined into the prompt. No binary office formats: a PDF or a
  .docx that arrives as garbage bytes would poison the plan, and the honest alternative is to say so.

Every cap lives here, and ``GET /chat/attachments`` publishes them, so the desktop UI pre-checks
against the same numbers the server enforces. A limit that only exists in the client is a lie the
server tells later; a limit that only exists on the server is a bad error message.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- limits

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # decoded, per image (~5 MP PNG)
MAX_TEXT_FILES = 4
MAX_TEXT_FILE_BYTES = 256 * 1024  # decoded, per file
MAX_TEXT_FILE_CHARS = 4_000  # injected per file; longer text is truncated and marked as such
MAX_ATTACHMENT_CHARS = 6_000  # total budget for everything attached, inside the prompt
MAX_REQUEST_BYTES = 24 * 1024 * 1024  # refuse absurd payloads before decoding anything

IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})

TEXT_MIMES = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/toml",
        "application/x-ndjson",
        "application/x-yaml",
        "application/xml",
        "application/yaml",
        "text/calendar",
    }
)
TEXT_EXTENSIONS = frozenset(
    {
        ".bat", ".c", ".cfg", ".conf", ".cpp", ".cs", ".css", ".csv", ".env", ".go", ".h", ".hpp",
        ".htm", ".html", ".ini", ".java", ".js", ".json", ".jsonl", ".jsx", ".kt", ".log", ".lua",
        ".markdown", ".md", ".php", ".pl", ".properties", ".ps1", ".py", ".rb", ".rs", ".rst",
        ".scss", ".sh", ".sql", ".swift", ".toml", ".ts", ".tsv", ".tsx", ".txt", ".vue", ".xml",
        ".yaml", ".yml", ".zsh",
    }
)


class AttachmentError(Exception):
    """A rejected attachment. ``code`` is the machine-readable reason the UI shows."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Prepared:
    """One accepted attachment, decoded."""

    name: str
    kind: str  # "image" | "text"
    mime: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


def limits() -> dict:
    """The contract the UI mirrors (published on ``GET /chat/attachments``)."""
    return {
        "images": {
            "max_count": MAX_IMAGES,
            "max_bytes": MAX_IMAGE_BYTES,
            "types": sorted(IMAGE_MIMES),
        },
        "files": {
            "max_count": MAX_TEXT_FILES,
            "max_bytes": MAX_TEXT_FILE_BYTES,
            "extensions": sorted(TEXT_EXTENSIONS),
        },
        "max_chars_in_prompt": MAX_ATTACHMENT_CHARS,
    }


def kind_for(name: str, mime: str) -> str | None:
    """Classify an attachment, or return ``None`` when it is not something we can honestly read.

    Mime type wins, the filename is the fallback (browsers hand over ``application/octet-stream``
    for perfectly ordinary files more often than anyone would like).
    """
    clean_mime = (mime or "").strip().lower()
    suffix = Path(name or "").suffix.lower()
    if clean_mime in IMAGE_MIMES:
        return "image"
    if clean_mime.startswith("image/"):
        return None  # a real image we cannot promise the vision model will read
    if not clean_mime and suffix in IMAGE_EXTENSIONS:
        return "image"
    if clean_mime in TEXT_MIMES or clean_mime.startswith("text/"):
        return "text"
    if clean_mime in {"", "application/octet-stream"} and suffix in TEXT_EXTENSIONS:
        return "text"
    if suffix in TEXT_EXTENSIONS and not clean_mime:
        return "text"
    return None


def decode(data_base64: str) -> bytes:
    """Decode an attachment payload, tolerating a full ``data:`` URL. Raises AttachmentError."""
    raw = (data_base64 or "").strip()
    if not raw:
        raise AttachmentError("EMPTY_ATTACHMENT", "an attachment carried no data")
    if len(raw) > MAX_REQUEST_BYTES:
        raise AttachmentError(
            "ATTACHMENT_TOO_LARGE",
            f"an attachment encoded larger than {MAX_REQUEST_BYTES // (1024 * 1024)} MB",
        )
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
        raw = raw.strip()
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError("BAD_ENCODING", f"an attachment was not valid base64 ({exc})") from exc


def prepare(attachments: list) -> list[Prepared]:
    """Validate and decode a request's attachments, or raise the first honest reason it cannot.

    ``attachments`` are the request models (anything with ``name``/``mime``/``data`` attributes) or
    plain dicts with the same keys — the API hands over models, tests hand over dicts.
    """
    prepared: list[Prepared] = []
    images = 0
    files = 0
    for item in attachments or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "")
            mime = str(item.get("mime") or "")
            data = str(item.get("data") or "")
        else:
            name = str(getattr(item, "name", "") or "")
            mime = str(getattr(item, "mime", "") or "")
            data = str(getattr(item, "data", "") or "")
        label = name or "attachment"
        kind = kind_for(name, mime)
        if kind is None:
            raise AttachmentError(
                "UNSUPPORTED_TYPE",
                f"{label}: only images (PNG/JPEG/WebP/GIF) and text files are supported",
            )
        payload = decode(data)
        if not payload:
            raise AttachmentError("EMPTY_ATTACHMENT", f"{label} was empty")
        if kind == "image":
            images += 1
            if images > MAX_IMAGES:
                raise AttachmentError("TOO_MANY_IMAGES", f"at most {MAX_IMAGES} images per message")
            if len(payload) > MAX_IMAGE_BYTES:
                raise AttachmentError(
                    "IMAGE_TOO_LARGE",
                    f"{label} is {len(payload) / 1_048_576:.1f} MB — the limit is "
                    f"{MAX_IMAGE_BYTES // 1_048_576} MB per image",
                )
        else:
            files += 1
            if files > MAX_TEXT_FILES:
                raise AttachmentError("TOO_MANY_FILES", f"at most {MAX_TEXT_FILES} files per message")
            if len(payload) > MAX_TEXT_FILE_BYTES:
                raise AttachmentError(
                    "FILE_TOO_LARGE",
                    f"{label} is {len(payload) / 1024:.0f} KB — the limit is "
                    f"{MAX_TEXT_FILE_BYTES // 1024} KB per text file",
                )
        prepared.append(Prepared(name=label, kind=kind, mime=mime, data=payload))
    return prepared


def decode_text(prepared: Prepared) -> tuple[str, bool]:
    """Decode a text attachment to ``(text, truncated)``. Never raises: bad bytes are replaced."""
    text = prepared.data.decode("utf-8", errors="replace")
    # Nulls and other control characters would derail the prompt without adding meaning.
    text = "".join(char for char in text if char in "\n\t\r" or ord(char) >= 32)
    if len(text) > MAX_TEXT_FILE_CHARS:
        return text[:MAX_TEXT_FILE_CHARS], True
    return text, False
