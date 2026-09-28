"""Attachments: the limits, the honest rejections, and how they reach the model's context."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.core.attachments import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    MAX_TEXT_FILE_BYTES,
    AttachmentError,
    decode_text,
    kind_for,
    prepare,
)
from app.main import create_app
from app.schemas import AttachmentBlock, ContextBundle


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def item(name: str, mime: str, data: bytes) -> dict:
    return {"name": name, "mime": mime, "data": b64(data)}


@pytest.fixture()
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# ------------------------------------------------------------------ classification


def test_kind_follows_mime_then_filename() -> None:
    assert kind_for("shot.png", "image/png") == "image"
    assert kind_for("photo.jpeg", "") == "image"
    assert kind_for("notes.txt", "text/plain") == "text"
    # Browsers hand over octet-stream for ordinary files; the extension is the fallback.
    assert kind_for("report.md", "application/octet-stream") == "text"
    # A PDF is not text we can honestly read, and guessing would poison the plan.
    assert kind_for("contract.pdf", "application/pdf") is None
    assert kind_for("archive.zip", "application/zip") is None
    assert kind_for("drawing.bmp", "image/bmp") is None


# ------------------------------------------------------------------ limits


def test_images_are_capped_by_count_and_size() -> None:
    image = item("shot.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    prepare([image] * MAX_IMAGES)
    with pytest.raises(AttachmentError) as too_many:
        prepare([image] * (MAX_IMAGES + 1))
    assert too_many.value.code == "TOO_MANY_IMAGES"

    huge = item("huge.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"0" * (MAX_IMAGE_BYTES + 1))
    with pytest.raises(AttachmentError) as too_big:
        prepare([huge])
    assert too_big.value.code == "IMAGE_TOO_LARGE"


def test_text_files_are_capped_and_unsupported_types_are_refused() -> None:
    with pytest.raises(AttachmentError) as unsupported:
        prepare([item("contract.pdf", "application/pdf", b"%PDF-1.4")])
    assert unsupported.value.code == "UNSUPPORTED_TYPE"
    assert "images" in unsupported.value.message  # the message tells the user what IS allowed

    with pytest.raises(AttachmentError) as too_big:
        prepare([item("big.txt", "text/plain", b"x" * (MAX_TEXT_FILE_BYTES + 1))])
    assert too_big.value.code == "FILE_TOO_LARGE"

    with pytest.raises(AttachmentError) as empty:
        prepare([item("nothing.txt", "text/plain", b"")])
    assert empty.value.code == "EMPTY_ATTACHMENT"


def test_bad_base64_is_rejected_before_decoding() -> None:
    with pytest.raises(AttachmentError) as bad:
        prepare([{"name": "a.txt", "mime": "text/plain", "data": "not base64!!"}])
    assert bad.value.code == "BAD_ENCODING"


def test_data_urls_are_tolerated() -> None:
    payload = b64(b"hello")
    prepared = prepare([{"name": "a.txt", "mime": "text/plain", "data": f"data:text/plain;base64,{payload}"}])
    assert prepared[0].data == b"hello"


def test_long_text_is_truncated_and_says_so() -> None:
    prepared = prepare([item("long.txt", "text/plain", ("word " * 4_000).encode())])
    text, truncated = decode_text(prepared[0])
    assert truncated is True
    assert len(text) <= 4_000


# ------------------------------------------------------------------ context rendering


def test_attachments_reach_the_prompt_before_everything_else() -> None:
    bundle = ContextBundle(
        task_id="t1",
        user_message="summarise this",
        attachments=[
            AttachmentBlock(name="notes.txt", kind="text", mime="text/plain", bytes=24, text="the answer is 42"),
            AttachmentBlock(name="shot.png", kind="image", mime="image/png", bytes=9, text="a red square"),
        ],
    )
    rendered = bundle.as_prompt_context()
    assert "[ATTACHMENTS" in rendered
    assert "the answer is 42" in rendered
    assert "IMAGE shot.png" in rendered and "a red square" in rendered
    # They are rendered above memory/screen/etc, so a long context cannot cut them first.
    assert rendered.index("[ATTACHMENTS") < rendered.index("[ENVIRONMENT]")


# ------------------------------------------------------------------ API


def test_attachment_limits_are_published(client: TestClient) -> None:
    body = client.get("/chat/attachments").json()
    assert body["images"]["max_count"] == MAX_IMAGES
    assert ".md" in body["files"]["extensions"]
    assert "image/png" in body["accepted"]["image_types"]


def test_chat_accepts_a_text_attachment(client: TestClient) -> None:
    response = client.post(
        "/chat",
        json={
            "message": "what does this say?",
            "context": {
                "screen": False,
                "attachments": [item("note.txt", "text/plain", b"the password is not here")],
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"


def test_chat_rejects_an_unsupported_attachment_with_a_reason(client: TestClient) -> None:
    response = client.post(
        "/chat",
        json={
            "message": "read this",
            "context": {"screen": False, "attachments": [item("contract.pdf", "application/pdf", b"%PDF-1.4")]},
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_TYPE"


def test_an_attachment_alone_is_a_valid_request(client: TestClient) -> None:
    response = client.post(
        "/chat",
        json={"message": "  ", "context": {"screen": False, "attachments": [item("a.txt", "text/plain", b"hi")]}},
    )
    assert response.status_code == 200
    # …but an empty message with nothing attached is still refused.
    assert client.post("/chat", json={"message": "  "}).status_code == 422
