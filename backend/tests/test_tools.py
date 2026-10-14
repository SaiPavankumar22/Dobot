"""Tool behaviour: sandbox enforcement, verification and approval previews."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


def test_app_open_resolves_the_phrases_the_model_uses() -> None:
    """The planner said "File Explorer"; launching that literal string timed out and then
    failed its own process probe (tasklist can never match an image called "File Explorer")."""
    from app.tools.apps import _resolve_alias

    assert _resolve_alias("File Explorer") == "explorer"
    assert _resolve_alias("file explorer") == "explorer"
    assert _resolve_alias("VS Code") == "vscode"
    assert _resolve_alias("Visual Studio Code") == "vscode"
    assert _resolve_alias("web browser") == "browser"
    assert _resolve_alias("notepad") == "notepad"
    assert _resolve_alias("somethingelse") is None


def test_app_open_process_hint_is_an_image_name_or_nothing() -> None:
    from app.tools.apps import AppOpenTool

    assert AppOpenTool._process_hint("explorer") == "explorer.exe"
    assert AppOpenTool._process_hint("calc") == "CalculatorApp.exe"
    # A spaced phrase is not an image name — probe() must not be handed it.
    assert AppOpenTool._process_hint("File Explorer") == ""


@pytest.mark.asyncio
async def test_app_open_rejects_an_unknown_name_instead_of_launching_it(ctx: ToolContext) -> None:
    result = await AppOpenTool().run({"app": "File Explorer"}, ctx)
    # "File Explorer" now resolves to explorer, so this must be a *real* launch or a clean
    # rejection — never `start "" "File Explorer"`.
    if result.ok:
        assert "File Explorer" not in result.output["command"]
    else:
        assert "unknown application" in (result.error or "")


@pytest.mark.asyncio
async def test_app_open_unresolvable_name_fails_cleanly(ctx: ToolContext) -> None:
    result = await AppOpenTool().run({"app": "Banana Deluxe"}, ctx)
    assert not result.ok
    assert "unknown application" in (result.error or "")


@pytest.mark.asyncio
async def test_app_open_takes_a_url_target_for_browsers(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A URL used to go through check_path, which resolved it against the working directory and
    then refused it as outside the allowed roots — so opening a page in a browser never worked.

    A stub sandbox keeps this deterministic on any platform: no real browser is launched, and the
    assertion is that check_path is never asked to treat a URL as a path.
    """
    from app.tools import apps as apps_module

    monkeypatch.setattr(apps_module, "_platform_key", lambda: "win32")
    seen_paths: list[str] = []
    commands: list[str] = []

    class _StubSandbox:
        async def check_path(self, raw: str, *, write: bool = False) -> Path:
            seen_paths.append(raw)
            return Path(raw)

        async def run_command(self, command: str, **kwargs):
            commands.append(command)
            return SimpleNamespace(ok=True, exit_code=0, stdout="", stderr="", backend="stub")

    tool = AppOpenTool()
    stub_ctx = ToolContext(settings=ctx.settings, sandbox=_StubSandbox(), task_id="task_test")
    result = await tool.run({"app": "browser", "target": "https://gemini.google.com"}, stub_ctx)

    assert result.ok, result.error
    assert result.output["target_kind"] == "url"
    assert result.output["target"] == "https://gemini.google.com"
    assert seen_paths == [], "a URL must never reach the filesystem path check"
    launches = [c for c in commands if c.startswith("start ") or "msedge" in c and "tasklist" not in c]
    assert launches and "https://gemini.google.com" in launches[0]
    # ...and must never have been rewritten into a filesystem path.
    assert "D:" not in launches[0]


@pytest.mark.asyncio
async def test_app_open_refuses_a_url_target_for_a_file_manager(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """explorer cannot open a URL; say so instead of launching it with a nonsense argument.

    The platform is pinned to win32 so this holds on the Linux CI runner too.
    """
    from app.tools import apps as apps_module

    monkeypatch.setattr(apps_module, "_platform_key", lambda: "win32")
    result = await AppOpenTool().run(
        {"app": "explorer", "target": "https://gemini.google.com"}, ctx
    )
    assert not result.ok
    assert "cannot open a URL" in (result.error or "")


def test_url_target_is_not_routed_through_the_path_check() -> None:
    from app.tools.apps import _URL_SCHEME

    assert _URL_SCHEME.match("https://gemini.google.com")
    assert _URL_SCHEME.match("http://localhost:3000/x?y=1")
    assert not _URL_SCHEME.match("C:/Users/HP/notes.md")
    assert not _URL_SCHEME.match("~/dobot-test")


_TASKLIST_V = """\
Image Name                     PID Session Name        Session# Mem Usage Status          User Name       CPU Time  Window Title
========================= ======== ================ =========== ============ =============== ============ ============
msedge.exe                   21256 Console                    6   1,80,832 K Running         DESKTOP\\HP      0:00:32 Google Gemini and 3 more pages - Personal - Microsoft Edge
msedge.exe                   18140 Console                    6     12,228 K Running         DESKTOP\\HP      0:00:00 N/A
"""


@pytest.mark.asyncio
async def test_window_titles_are_parsed_from_tasklist_v(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The title is the field after CPU time — parsing must not swallow it or keep 'N/A'."""
    from app.tools import apps as apps_module

    monkeypatch.setattr(apps_module, "_platform_key", lambda: "win32")

    class _Stub:
        async def run_command(self, command: str, **kwargs):
            return SimpleNamespace(ok=True, exit_code=0, stdout=_TASKLIST_V, stderr="")

    stub_ctx = ToolContext(settings=ctx.settings, sandbox=_Stub(), task_id="task_test")
    titles = await apps_module.AppOpenTool._window_titles(stub_ctx, "msedge.exe")
    assert titles == {"Google Gemini and 3 more pages - Personal - Microsoft Edge"}


@pytest.mark.asyncio
async def test_url_verification_never_claims_success_from_a_process_alone(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edge may already be running; process_running alone is not evidence the page opened.

    With no readable window titles the step must come back *unverifiable*, not verified.
    """
    from app.tools import apps as apps_module

    monkeypatch.setattr(apps_module, "_platform_key", lambda: "win32")

    async def _no_titles(*args, **kwargs):
        return set()

    monkeypatch.setattr(apps_module.AppOpenTool, "_window_titles", staticmethod(_no_titles))

    class _Stub:
        async def run_command(self, command: str, **kwargs):
            return SimpleNamespace(ok=True, exit_code=0, stdout="msedge.exe  1", stderr="")

    tool = apps_module.AppOpenTool()
    stub_ctx = ToolContext(settings=ctx.settings, sandbox=_Stub(), task_id="task_test")
    result = tool.ok(
        "app_open",
        {
            "process_hint": "msedge.exe",
            "target_kind": "url",
            "target": "https://gemini.google.com",
            "window_titles_before": [],
        },
        0.0,
    )
    verification = await tool.verify({}, result, stub_ctx)
    assert verification.verified is False
    assert verification.skipped_reason, "an inconclusive probe must say so"
    assert all(check.passed for check in verification.checks), "no check may fail spuriously"


@pytest.mark.asyncio
async def test_url_verification_reports_a_new_window_as_evidence(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.tools import apps as apps_module

    monkeypatch.setattr(apps_module, "_platform_key", lambda: "win32")

    async def _titles(*args, **kwargs):
        return {"Wikipedia and 4 more pages - Personal - Microsoft Edge"}

    monkeypatch.setattr(apps_module.AppOpenTool, "_window_titles", staticmethod(_titles))

    class _Stub:
        async def run_command(self, command: str, **kwargs):
            return SimpleNamespace(ok=True, exit_code=0, stdout="msedge.exe  1", stderr="")

    tool = apps_module.AppOpenTool()
    stub_ctx = ToolContext(settings=ctx.settings, sandbox=_Stub(), task_id="task_test")
    result = tool.ok(
        "app_open",
        {
            "process_hint": "msedge.exe",
            "target_kind": "url",
            "target": "https://wikipedia.org",
            "window_titles_before": ["Google Gemini and 3 more pages - Personal - Edge"],
        },
        0.0,
    )
    verification = await tool.verify({}, result, stub_ctx)
    assert verification.verified is True
    assert any(check.name == "window_opened_for_target" and check.passed for check in verification.checks)


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
