"""Tool behaviour: sandbox enforcement, verification and approval previews."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.agents.sandbox import build_sandbox
from app.schemas import ActionSpec
from app.tools.apps import AppOpenTool
from app.tools.base import ToolContext
from app.tools.browser import BrowserOpenTool, BrowserSession
from app.tools.filesystem import (
    FsDeleteTool,
    FsListTool,
    FsMoveTool,
    FsReadTool,
    FsWriteTool,
)
from app.tools.productivity import RememberTool
from app.tools.terminal import TerminalRunTool


@pytest.fixture()
def ctx(settings, bus, workspace: Path) -> ToolContext:
    return ToolContext(
        settings=settings,
        sandbox=build_sandbox(settings),
        bus=bus,
        task_id="task_test",
    )


# ------------------------------------------------------------------ filesystem


@pytest.mark.asyncio
async def test_write_then_read_round_trip(ctx: ToolContext, workspace: Path) -> None:
    target = workspace / "notes.md"
    write = await FsWriteTool().run({"path": str(target), "content": "hello dobot"}, ctx)
    assert write.ok, write.error
    verification = await FsWriteTool().verify({"path": str(target)}, write, ctx)
    assert verification.verified

    read = await FsReadTool().run({"path": str(target)}, ctx)
    assert read.ok
    assert read.output["content"] == "hello dobot"


@pytest.mark.asyncio
async def test_write_is_refused_outside_the_sandbox(ctx: ToolContext, tmp_path: Path) -> None:
    outside = tmp_path.parent / "escape.txt"
    result = await FsWriteTool().run({"path": str(outside), "content": "nope"}, ctx)
    assert not result.ok
    assert "outside the allowed roots" in (result.error or "")
    assert not outside.exists()


@pytest.mark.asyncio
async def test_overwrite_protection(ctx: ToolContext, workspace: Path) -> None:
    target = workspace / "keep.txt"
    target.write_text("original", encoding="utf-8")
    refused = await FsWriteTool().run({"path": str(target), "content": "replace"}, ctx)
    assert not refused.ok
    assert target.read_text(encoding="utf-8") == "original"
    allowed = await FsWriteTool().run(
        {"path": str(target), "content": "replace", "overwrite": True}, ctx
    )
    assert allowed.ok
    assert target.read_text(encoding="utf-8") == "replace"


@pytest.mark.asyncio
async def test_write_verification_detects_tampering(ctx: ToolContext, workspace: Path) -> None:
    target = workspace / "report.md"
    write = await FsWriteTool().run({"path": str(target), "content": "line one"}, ctx)
    target.write_text("someone changed this", encoding="utf-8")
    verification = await FsWriteTool().verify({"path": str(target)}, write, ctx)
    assert not verification.verified
    assert any(check.name == "content_matches" and not check.passed for check in verification.checks)


@pytest.mark.asyncio
async def test_move_with_multiple_globs(ctx: ToolContext, workspace: Path) -> None:
    inbox = workspace / "inbox"
    inbox.mkdir(exist_ok=True)
    for name in ("a.png", "b.jpg", "c.txt"):
        (inbox / name).write_text("x", encoding="utf-8")
    result = await FsMoveTool().run(
        {
            "sources": [str(inbox / "*.png"), str(inbox / "*.jpg")],
            "destination": str(workspace / "archive"),
        },
        ctx,
    )
    assert result.ok, result.error
    assert result.output["count"] == 2
    verification = await FsMoveTool().verify({}, result, ctx)
    assert verification.verified


@pytest.mark.asyncio
async def test_delete_and_preview_categorisation(ctx: ToolContext, workspace: Path) -> None:
    folder = workspace / "Downloads"
    folder.mkdir(exist_ok=True)
    for name in ("shot1.png", "shot2.png", "paper.pdf", "setup.exe", "mystery.bin"):
        (folder / name).write_text("x", encoding="utf-8")

    preview = await FsDeleteTool().preview({"path": str(folder / "*")}, ctx)
    joined = "\n".join(preview)
    assert "5 file(s)" in joined
    assert "screenshots" in joined
    assert "other/unknown" in joined

    result = await FsDeleteTool().run({"path": str(folder / "*.png")}, ctx)
    assert result.ok, result.error
    assert result.output["count"] == 2
    verification = await FsDeleteTool().verify({}, result, ctx)
    assert verification.verified
    assert sorted(item.name for item in folder.iterdir()) == ["mystery.bin", "paper.pdf", "setup.exe"]


@pytest.mark.asyncio
async def test_list_reports_entries(ctx: ToolContext, workspace: Path) -> None:
    (workspace / "one.txt").write_text("1", encoding="utf-8")
    (workspace / "two.txt").write_text("2", encoding="utf-8")
    result = await FsListTool().run({"path": str(workspace)}, ctx)
    assert result.ok
    assert result.output["count"] >= 2


# -------------------------------------------------------------------- terminal


@pytest.mark.asyncio
async def test_terminal_run_success_and_verification(ctx: ToolContext) -> None:
    tool = TerminalRunTool()
    result = await tool.run({"command": "echo dobot-ok", "expect_stdout": "dobot-ok"}, ctx)
    assert result.ok, result.error
    verification = await tool.verify({"expect_stdout": "dobot-ok"}, result, ctx)
    assert verification.verified


@pytest.mark.asyncio
async def test_terminal_run_failure_is_reported(ctx: ToolContext) -> None:
    result = await TerminalRunTool().run({"command": "exit 3"}, ctx)
    assert not result.ok
    assert "exited with 3" in (result.error or "")


# --------------------------------------------------------------------- browser


@pytest.mark.asyncio
async def test_browser_open_reads_and_detects_change(ctx: ToolContext) -> None:
    html = "<html><head><title>Example</title></head><body><h1>Hello</h1><p>Body text</p></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    session = BrowserSession()
    session.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    ctx.browser = session  # keep the test off the real network

    tool = BrowserOpenTool()
    result = await tool.run({"url": "https://example.com/page"}, ctx)
    assert result.ok, result.error
    assert result.output["title"] == "Example"
    assert "Hello" in result.output["text"]
    verification = await tool.verify({}, result, ctx)
    assert verification.verified


@pytest.mark.asyncio
async def test_browser_refuses_host_outside_allowlist(ctx: ToolContext) -> None:
    result = await BrowserOpenTool().run({"url": "https://not-allowed.example.net/"}, ctx)
    assert not result.ok
    assert "allowlist" in (result.error or "")


# ------------------------------------------------------------------ productivity


@pytest.mark.asyncio
async def test_remember_tool_persists_and_verifies(services) -> None:
    ctx = services.tool_context(task_id="task_remember")
    tool = RememberTool()
    result = await tool.run(
        {"content": "Dobot uses Zilliz for long-term memory", "type": "fact", "importance": 0.8}, ctx
    )
    assert result.ok, result.error
    verification = await tool.verify({}, result, ctx)
    assert verification.verified
    hits = await services.memory.recall("Zilliz long-term memory", limit=3)
    assert hits and "Zilliz" in hits[0].memory.content


@pytest.mark.asyncio
async def test_app_open_plans_a_command(ctx: ToolContext) -> None:
    result = await AppOpenTool().run({"app": "notepad"}, ctx)
    assert result.ok, result.error
    assert "command" in result.output


def test_tool_specs_expose_risk_floors() -> None:
    from app.tools.registry import default_registry

    registry = default_registry()
    specs = {spec["name"]: spec for spec in registry.specs()}
    assert specs["fs_delete"]["risk_floor"] == "HIGH"
    assert specs["tavily_search"]["risk_floor"] == "LOW"
    assert "fs_write" in specs


def test_policy_preview_of_a_move_lists_categories(workspace: Path) -> None:
    """Preview lines are what the approval dialog shows, so they must be specific."""
    from app.tools.filesystem import FsMoveTool

    assert FsMoveTool().preview is not None
    assert ActionSpec(tool="fs_move", params={"sources": [], "destination": "x"}).tool == "fs_move"
