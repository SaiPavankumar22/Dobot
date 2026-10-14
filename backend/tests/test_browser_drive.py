"""Tests for the local Playwright browser driver tools.

No real browser is launched here (CI must stay browser-free): tools take an injected session,
and DriveSession internals are exercised against fakes that mimic the Playwright surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.agents.sandbox import build_sandbox
from app.schemas import RiskLevel
from app.tools.base import ToolContext
from app.tools.browser import BrowserSubmitTool
from app.tools.browser_drive import (
    BrowserClickTool,
    BrowserNavigateTool,
    BrowserSnapshotTool,
    BrowserTypeTool,
    DriveSession,
    _clone_chrome_profile,
    _state_output,
    drive_submit,
)
from app.tools.registry import default_registry


@pytest.fixture()
def ctx(settings, bus, workspace: Path) -> ToolContext:
    return ToolContext(
        settings=settings,
        sandbox=build_sandbox(settings),
        bus=bus,
        task_id="task_test",
    )


def _state(**overrides: Any) -> dict[str, Any]:
    state = {
        "url": "https://example.com/page",
        "title": "Example Page",
        "text": "hello world",
        "elements": [{"i": 0, "tag": "a", "type": "", "text": "More", "css": "body > a"}],
        "element_count": 1,
        "changed": True,
        "downloads": [],
    }
    state.update(overrides)
    return state


class FakeDrive:
    """Stands in for DriveSession; records calls, never launches a browser."""

    def __init__(self, *, state: dict[str, Any] | None = None, ensure_error: str = "",
                 submit: dict[str, Any] | None = None) -> None:
        self.state = state or _state()
        self.ensure_error = ensure_error
        self.submit_result = submit or {"ok": True, "confirmation": "done", "page_changed": True}
        self.calls: list[tuple[str, Any]] = []
        self.snapshot_waits: list[int] = []

    async def ensure(self) -> str:
        return self.ensure_error

    async def goto(self, url: str) -> dict[str, Any]:
        if self.ensure_error:
            raise RuntimeError(self.ensure_error)
        self.calls.append(("goto", url))
        return self.state

    async def snapshot(self, fresh: bool = False, wait_seconds: float = 0) -> dict[str, Any]:
        if self.ensure_error:
            raise RuntimeError(self.ensure_error)
        self.calls.append(("snapshot", fresh))
        self.snapshot_waits.append(int(wait_seconds))
        return self.state

    async def click(self, target: str) -> dict[str, Any]:
        if self.ensure_error:
            raise RuntimeError(self.ensure_error)
        self.calls.append(("click", target))
        return self.state

    async def type_text(self, target: str, text: str, press_enter: bool = False) -> dict[str, Any]:
        if self.ensure_error:
            raise RuntimeError(self.ensure_error)
        self.calls.append(("type", (target, text, press_enter)))
        return self.state | {"confirmed": self.state.get("confirmed", True)}

    async def submit(self, url: str, fields: dict[str, Any], submit_selector: str) -> dict[str, Any]:
        self.calls.append(("submit", (url, fields, submit_selector)))
        return self.submit_result


class FakePage:
    """Minimal page handle so DriveSession.ensure() short-circuits without launching."""

    def is_closed(self) -> bool:
        return False


# ------------------------------------------------------------------ registration


def test_registry_exposes_the_local_browser_tools() -> None:
    registry = default_registry()
    for name in ("browser_navigate", "browser_snapshot", "browser_click", "browser_type"):
        tool = registry.get(name)
        assert tool is not None, f"{name} missing from registry"
    assert registry.get("browser_navigate").risk_floor is RiskLevel.LOW
    assert registry.get("browser_click").risk_floor is RiskLevel.MEDIUM
    assert registry.get("browser_type").risk_floor is RiskLevel.MEDIUM


# ------------------------------------------------------------------ navigate / snapshot


async def test_browser_navigate_returns_indexed_elements(ctx: ToolContext) -> None:
    tool = BrowserNavigateTool()
    tool.session = FakeDrive()
    result = await tool.run({"url": "https://example.com/page"}, ctx)
    assert result.ok
    assert result.output["element_count"] == 1
    assert result.output["elements"][0]["css"] == "body > a"
    assert tool.session.calls == [("goto", "https://example.com/page")]

    verification = await tool.verify({"url": "https://example.com/page"}, result, ctx)
    assert verification.verified
    assert {c.name for c in verification.checks} == {"page_loaded", "interactive_elements_indexed"}


async def test_browser_navigate_fails_cleanly_when_browser_unavailable(ctx: ToolContext) -> None:
    tool = BrowserNavigateTool()
    tool.session = FakeDrive(ensure_error="could not launch a browser: no channel")
    result = await tool.run({"url": "https://example.com"}, ctx)
    assert not result.ok
    assert "could not launch" in result.error


async def test_browser_snapshot_uses_fresh_indices(ctx: ToolContext) -> None:
    tool = BrowserSnapshotTool()
    tool.session = FakeDrive()
    result = await tool.run({}, ctx)
    assert result.ok
    assert tool.session.calls == [("snapshot", True)]


# ------------------------------------------------------------------ type / click


async def test_browser_type_reports_read_back_confirmation(ctx: ToolContext) -> None:
    tool = BrowserTypeTool()
    tool.session = FakeDrive()
    result = await tool.run({"target": "3", "text": "generate a banner", "press_enter": True}, ctx)
    assert result.ok
    assert result.output["confirmed"] is True
    assert tool.session.calls == [("type", ("3", "generate a banner", True))]

    verification = await tool.verify({"target": "3", "text": "generate a banner"}, result, ctx)
    assert verification.verified
    assert any(c.name == "text_read_back" and c.passed for c in verification.checks)


async def test_browser_type_unverified_when_field_not_readable(ctx: ToolContext) -> None:
    tool = BrowserTypeTool()
    tool.session = FakeDrive(state=_state(confirmed=False))
    result = await tool.run({"target": "box", "text": "hi"}, ctx)
    assert result.ok
    verification = await tool.verify({"target": "box", "text": "hi"}, result, ctx)
    assert not verification.verified
    # The note must not read like a success story when the read-back failed.
    assert "did not land" in verification.notes


async def test_browser_click_reports_download_path_in_notes(ctx: ToolContext) -> None:
    tool = BrowserClickTool()
    tool.session = FakeDrive(
        state=_state(downloads=[{"file": "banner.png", "path": "/downloads/banner.png"}])
    )
    result = await tool.run({"target": "12", "description": "download the banner"}, ctx)
    assert result.ok
    verification = await tool.verify({"target": "12"}, result, ctx)
    assert verification.verified
    assert "banner.png" in verification.notes


async def test_browser_click_flags_unchanged_page(ctx: ToolContext) -> None:
    tool = BrowserClickTool()
    tool.session = FakeDrive(state=_state(changed=False))
    result = await tool.run({"target": "12"}, ctx)
    verification = await tool.verify({"target": "12"}, result, ctx)
    assert not verification.verified


# ------------------------------------------------------------------ submit fallback chain


async def test_submit_prefers_local_driver(ctx: ToolContext) -> None:
    tool = BrowserSubmitTool()
    tool.session = FakeDrive()
    result = await tool.run({"url": "https://example.com/login",
                             "fields": {"user": "ada"}}, ctx)
    assert result.ok
    assert result.output["confirmation"] == "done"
    assert tool.session.calls[0][0] == "submit"


async def test_submit_fails_not_fakes_when_no_backend(ctx: ToolContext) -> None:
    tool = BrowserSubmitTool()
    tool.session = FakeDrive(ensure_error="playwright is not installed")
    ctx.hermes = None
    result = await tool.run({"url": "https://example.com/login"}, ctx)
    assert not result.ok
    assert "playwright is not installed" in result.error
    assert result.output is None  # a refused submit never produces a success payload


async def test_drive_submit_surfaces_ensure_error() -> None:
    result = await drive_submit(FakeDrive(ensure_error="no browser"), "https://x.test", {}, "")
    assert result["ok"] is False
    assert result["error"] == "no browser"


# ------------------------------------------------------------------ DriveSession internals


async def test_ensure_short_circuits_when_page_alive() -> None:
    session = DriveSession()
    session._page = FakePage()
    assert await session.ensure() == ""


async def test_locate_index_outside_snapshot_raises_helpful_error() -> None:
    session = DriveSession()
    session._page = FakePage()
    session._elements = []
    with pytest.raises(ValueError, match="browser_navigate"):
        await session._locate("4")


async def test_locate_text_with_no_match_lists_strategies() -> None:
    class NeverVisible:
        """A locator handle whose wait_for always fails, like an element nobody rendered."""

        @property
        def first(self) -> NeverVisible:
            return self

        async def wait_for(self, **_: Any) -> None:
            raise RuntimeError("not visible")

    class DeadPage:
        def locator(self, *a: Any, **k: Any) -> NeverVisible:
            return NeverVisible()

        def get_by_text(self, *a: Any, **k: Any) -> NeverVisible:
            return NeverVisible()

        def get_by_placeholder(self, *a: Any, **k: Any) -> NeverVisible:
            return NeverVisible()

        def get_by_label(self, *a: Any, **k: Any) -> NeverVisible:
            return NeverVisible()

        def get_by_role(self, *a: Any, **k: Any) -> NeverVisible:
            return NeverVisible()

    session = DriveSession()
    session._page = DeadPage()
    with pytest.raises(ValueError, match="visible text"):
        await session._locate("no such thing")


async def test_locate_falls_back_to_snapshot_handle_substring() -> None:
    """A model asking for 'textarea' should land on the rich-textarea div the snapshot listed."""

    class ChoosyPage:
        def locator(self, selector: str) -> Any:
            class Handle:
                @property
                def first(self) -> Handle:
                    return self

                async def wait_for(self, **_: Any) -> None:
                    if selector != "rich-textarea > div":
                        raise RuntimeError("not visible")

            return Handle()

        def get_by_text(self, *a: Any, **k: Any) -> Any:
            return self.locator("never-text")

        def get_by_placeholder(self, *a: Any, **k: Any) -> Any:
            return self.locator("never-ph")

        def get_by_label(self, *a: Any, **k: Any) -> Any:
            return self.locator("never-label")

        def get_by_role(self, *a: Any, **k: Any) -> Any:
            return self.locator("never-role")

    session = DriveSession()
    session._page = ChoosyPage()
    session._elements = [{"i": 0, "tag": "div", "text": "", "css": "rich-textarea > div"}]
    locator = await session._locate("textarea")
    assert locator is not None  # matched the recorded handle containing "textarea"


def test_state_output_trims_snapshot() -> None:
    output = _state_output(_state(text="x" * 5000, elements=[{"i": 0}] * 3))
    assert len(output["text"]) <= 2000
    assert output["element_count"] == 1  # original count preserved, not recomputed
    assert output["url"] == "https://example.com/page"


def test_state_output_flags_text_entry_elements() -> None:
    elements = [
        {"i": 0, "tag": "a", "type": "", "text": "Sign in", "css": "body > a"},
        {"i": 1, "tag": "div", "type": "", "text": "", "css": "main > rich-textarea > div"},
        {"i": 2, "tag": "input", "type": "text", "text": "q", "css": "body > input"},
        {"i": 3, "tag": "button", "type": "button", "text": "Send", "css": "body > button"},
    ]
    output = _state_output(_state(elements=elements))
    assert output["input_indices"] == [1, 2]


def test_clone_skips_when_marker_present(tmp_path: Path) -> None:
    dest = tmp_path / "clone"
    dest.mkdir()
    (dest / ".dobot-clone-ready").write_text("ok", encoding="utf-8")
    assert _clone_chrome_profile(dest) == ""


def test_clone_reports_missing_chrome_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))  # no Google/Chrome/User Data inside
    err = _clone_chrome_profile(tmp_path / "clone")
    assert "no Chrome profile found" in err
    assert not (tmp_path / "clone" / ".dobot-clone-ready").exists()


# -------------------------------------------------- purged-clone login guidance


class _CookieContext:
    """Fake Playwright context that only answers cookies()."""

    def __init__(self, cookies: list[dict[str, Any]]) -> None:
        self._cookies = cookies

    async def cookies(self) -> list[dict[str, Any]]:
        return self._cookies


async def test_purged_clone_sets_login_note() -> None:
    session = DriveSession()
    session.launched_with = "chrome"
    session._context = _CookieContext([])  # first launch after the purge: zero cookies
    await session._check_login_carry()
    assert "Sign in once inside this window" in session._login_note
    assert "persists for every future Dobot run" in session._login_note


async def test_clone_with_login_cookies_sets_no_note() -> None:
    session = DriveSession()
    session.launched_with = "chrome"
    session._context = _CookieContext([{"name": "__Secure-1PSID"}] * 25)
    await session._check_login_carry()
    assert session._login_note == ""


async def test_snapshot_carries_login_note_into_output() -> None:
    class NotePage:
        def is_closed(self) -> bool:
            return False

        async def evaluate(self, _js: Any) -> dict[str, Any]:
            return {"url": "https://gemini.google.com", "title": "Gemini", "text": "hi",
                    "elements": []}

    session = DriveSession()
    session._page = NotePage()
    session._login_note = "sign in once inside this window"
    state = await session.snapshot()
    output = _state_output(state)
    assert output["login_note"] == "sign in once inside this window"


async def test_navigate_verify_notes_include_login_note(ctx: ToolContext) -> None:
    tool = BrowserNavigateTool()
    tool.session = FakeDrive(state=_state(login_note="sign in once inside this window"))
    result = await tool.run({"url": "https://example.com"}, ctx)
    verification = await tool.verify({"url": "https://example.com"}, result, ctx)
    assert "sign in once inside this window" in verification.notes


def test_state_output_defaults_login_note_empty() -> None:
    assert _state_output(_state())["login_note"] == ""


# ------------------------------------------------- slow content: wait + retry


async def test_click_waits_and_retries_when_element_appears_late() -> None:
    class Handle:
        def __init__(self, page: SlowPage) -> None:
            self.page = page

        @property
        def first(self) -> Handle:
            return self

        async def wait_for(self, **_: Any) -> None:
            if not self.page.ready:
                raise RuntimeError("not visible yet")

        async def click(self, **_: Any) -> None:
            self.page.clicks += 1

    class SlowPage:
        """The Download button only exists on the third read (content still generating)."""

        def __init__(self) -> None:
            self.ready = False
            self.clicks = 0
            self.waits: list[int] = []
            self.reads = 0

        def is_closed(self) -> bool:
            return False

        async def wait_for_timeout(self, ms: int) -> None:
            self.waits.append(ms)

        async def evaluate(self, _js: Any) -> dict[str, Any]:
            self.reads += 1
            if self.reads >= 2:  # ready from the second read: the retry refresh must find it
                self.ready = True
            elements = (
                [{"i": 0, "tag": "button", "type": "", "text": "Download", "css": "body > button"}]
                if self.ready
                else []
            )
            return {"url": "u", "title": "t", "text": "page text", "elements": elements}

        def locator(self, _selector: str) -> Handle:
            return Handle(self)

        def get_by_text(self, *a: Any, **k: Any) -> Handle:
            return self.locator("text")

        def get_by_placeholder(self, *a: Any, **k: Any) -> Handle:
            return self.locator("placeholder")

        def get_by_label(self, *a: Any, **k: Any) -> Handle:
            return self.locator("label")

        def get_by_role(self, *a: Any, **k: Any) -> Handle:
            return self.locator("role")

    page = SlowPage()
    session = DriveSession()
    session._page = page
    state = await session.click("Download")
    assert page.clicks == 1
    assert 2500 in page.waits and 4000 in page.waits  # it waited instead of failing fast
    assert state["clicked"] == "Download"


async def test_snapshot_honours_wait_seconds() -> None:
    class WaitPage:
        def __init__(self) -> None:
            self.waited: list[int] = []

        def is_closed(self) -> bool:
            return False

        async def wait_for_timeout(self, ms: int) -> None:
            self.waited.append(ms)

        async def evaluate(self, _js: Any) -> dict[str, Any]:
            return {"url": "u", "title": "t", "text": "x", "elements": []}

    page = WaitPage()
    session = DriveSession()
    session._page = page
    await session.snapshot(fresh=True, wait_seconds=5)
    assert page.waited == [5000]


async def test_click_waits_for_download_record_landed_after_click() -> None:
    class Handle:
        def __init__(self, page: LatePage) -> None:
            self.page = page

        @property
        def first(self) -> Handle:
            return self

        async def wait_for(self, **_: Any) -> None:
            pass

        async def click(self, **_: Any) -> None:
            self.page.clicks += 1

    class LatePage:
        """The download finishes saving shortly after the click, not before the snapshot."""

        def __init__(self) -> None:
            self.session: DriveSession | None = None
            self.clicks = 0

        def is_closed(self) -> bool:
            return False

        async def wait_for_timeout(self, ms: int) -> None:
            if ms == 500 and self.session is not None and not self.session._downloads:
                self.session._downloads.append(
                    {"file": "banner.png", "url": "blob:x", "path": r"C:\u\Downloads\banner.png"}
                )

        async def evaluate(self, _js: Any) -> dict[str, Any]:
            return {"url": "u", "title": "t", "text": "page",
                    "elements": [{"i": 0, "tag": "button", "type": "", "text": "Download",
                                  "css": "body > button"}]}

        def locator(self, _selector: str) -> Handle:
            return Handle(self)

        def get_by_text(self, *a: Any, **k: Any) -> Handle:
            return self.locator("text")

        def get_by_placeholder(self, *a: Any, **k: Any) -> Handle:
            return self.locator("placeholder")

        def get_by_label(self, *a: Any, **k: Any) -> Handle:
            return self.locator("label")

        def get_by_role(self, *a: Any, **k: Any) -> Handle:
            return self.locator("role")

    page = LatePage()
    session = DriveSession()
    session._page = page
    page.session = session
    state = await session.click("Download")
    assert page.clicks == 1
    assert state["downloads"] and state["downloads"][0]["file"] == "banner.png"


async def test_browser_snapshot_tool_passes_wait_seconds(ctx: ToolContext) -> None:
    tool = BrowserSnapshotTool()
    drive = FakeDrive()
    tool.session = drive
    result = await tool.run({"wait_seconds": 7}, ctx)
    assert result.ok
    assert drive.snapshot_waits == [7]
    assert "wait_seconds" in tool.parameters["properties"]
