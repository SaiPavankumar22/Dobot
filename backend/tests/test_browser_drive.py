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
    _evidence,
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


# ------------------------------------------- identity-pinned resolution (no wrong clicks)


class RecordingHandle:
    """A locator handle that remembers which selector produced it."""

    def __init__(self, log: list[str], key: str, *, visible: bool = True,
                 live_name: str = "", click_error: str = "") -> None:
        self.log = log
        self.key = key
        self.visible = visible
        self.live_name = live_name
        self.click_error = click_error
        self.filled: str | None = None

    @property
    def first(self) -> RecordingHandle:
        return self

    async def wait_for(self, **_: Any) -> None:
        if not self.visible:
            raise RuntimeError("not visible")
        self.log.append(f"visible:{self.key}")

    async def evaluate(self, _js: str) -> str:
        return self.live_name

    async def click(self, **_: Any) -> None:
        if self.click_error:
            raise RuntimeError(self.click_error)
        self.log.append(f"click:{self.key}")

    async def fill(self, text: str, **_: Any) -> None:
        self.filled = text
        self.log.append(f"fill:{text}")

    async def input_value(self, **_: Any) -> str:
        return self.filled or ""

    async def inner_text(self, **_: Any) -> str:
        return self.filled or ""


async def test_locate_index_resolves_by_identity_stamp_before_css() -> None:
    """The stamp is the node the snapshot saw — CSS (which can drift) is never consulted."""
    log: list[str] = []
    stamp, css, role = (RecordingHandle(log, "stamp"), RecordingHandle(log, "css-path"),
                        RecordingHandle(log, "role"))

    class Page:
        def locator(self, selector: str) -> RecordingHandle:
            return stamp if "data-dobot-uid" in selector else css

        def get_by_role(self, *_a: Any, **_k: Any) -> RecordingHandle:
            return role

    session = DriveSession()
    session._page = Page()
    session._elements = [{"i": 0, "uid": "7", "role": "button", "name": "Send",
                          "text": "Send", "css": "nav > button"}]
    locator = await session._resolve_entry(session._elements[0])
    assert locator is stamp
    assert log == ["visible:stamp"]  # neither css nor role was even attempted


async def test_locate_index_falls_back_to_role_and_name_when_stamp_gone() -> None:
    """A re-render replaced the node: the same role+accessible name still means the target."""
    log: list[str] = []
    absent = RecordingHandle(log, "absent", visible=False)
    named = RecordingHandle(log, "named")
    recorded: dict[str, Any] = {}

    class Page:
        def locator(self, _selector: str) -> RecordingHandle:
            return absent  # the stamped node no longer exists

        def get_by_role(self, role: str, name: str) -> RecordingHandle:
            recorded.update(role=role, name=name)
            return named

    session = DriveSession()
    session._page = Page()
    session._elements = [{"i": 0, "uid": "7", "role": "button", "name": "Send",
                          "text": "Send", "css": "nav > button"}]
    locator = await session._resolve_entry(session._elements[0])
    assert locator is named
    assert recorded == {"role": "button", "name": "Send"}


async def test_locate_refuses_css_that_now_describes_a_different_element() -> None:
    """Last-resort CSS resolves, but to the wrong thing: refuse loudly instead of clicking it."""
    log: list[str] = []
    wrong = RecordingHandle(log, "wrong", live_name="Delete account")

    class Page:
        def locator(self, _selector: str) -> RecordingHandle:
            return wrong  # same path, different content than the snapshot recorded

        def get_by_role(self, *_a: Any, **_k: Any) -> RecordingHandle:
            raise AssertionError("role path must be skipped without a recorded role")

    session = DriveSession()
    session._page = Page()
    # No uid and no role: snapshots recorded before identity stamping existed still get
    # the identity check.
    session._elements = [{"i": 0, "role": "", "name": "Save", "text": "Save",
                          "css": "form > button"}]
    with pytest.raises(ValueError, match="stale handle"):
        await session._resolve_entry(session._elements[0])


async def test_locate_reports_gone_target_as_page_change() -> None:
    log: list[str] = []

    class Page:
        def locator(self, _selector: str) -> RecordingHandle:
            return RecordingHandle(log, "gone", visible=False)

        def get_by_role(self, *_a: Any, **_k: Any) -> RecordingHandle:
            return RecordingHandle(log, "gone", visible=False)

    session = DriveSession()
    session._page = Page()
    session._elements = [{"i": 0, "uid": "7", "role": "button", "name": "Send",
                          "text": "Send", "css": "nav > button"}]
    with pytest.raises(ValueError, match="no longer on the page"):
        await session._resolve_entry(session._elements[0])


def test_state_output_surfaces_marks_and_hides_the_stamp() -> None:
    state = _state(elements=[{
        "i": 0, "tag": "button", "type": "", "text": "Send", "css": "nav > button",
        "uid": "7", "role": "button", "name": "Send message", "disabled": True,
        "obscured": True,
    }])
    element = _state_output(state)["elements"][0]
    assert "uid" not in element, "identity stamps are server plumbing, not agent handles"
    assert element["role"] == "button" and element["name"] == "Send message"
    assert element["disabled"] is True and element["obscured"] is True
    assert "evidence" in _state_output(state)


def test_state_output_indexes_text_entry_elements_by_role() -> None:
    elements = [
        {"i": 0, "tag": "div", "type": "", "text": "", "css": "main > div", "role": "textbox",
         "name": "Message"},  # contenteditable composer: no input/textarea tag at all
        {"i": 1, "tag": "a", "type": "", "text": "Home", "css": "nav > a", "role": "link",
         "name": "Home"},
    ]
    assert _state_output(_state(elements=elements))["input_indices"] == [0]


# ------------------------------------------------- patient click/type + honest evidence


async def test_click_pins_the_original_element_across_a_rerender() -> None:
    """A fresh snapshot reorders the list; the agent's index must keep its element."""
    log: list[str] = []
    send = RecordingHandle(log, "send")
    decoy = RecordingHandle(log, "decoy")
    snapshots = [
        # 1: disabled, so the first attempt waits instead of clicking
        {"url": "u", "title": "t", "text": "page",
         "elements": [{"i": 0, "uid": "7", "role": "button", "name": "Send", "text": "Send",
                       "css": "nav > button", "disabled": True}]},
        # 2: re-rendered — a NEW element sits at index 0, the target moved to index 1
        {"url": "u", "title": "t", "text": "page",
         "elements": [
             {"i": 0, "uid": "9", "role": "button", "name": "Cancel", "text": "Cancel",
              "css": "nav > button"},
             {"i": 1, "uid": "7", "role": "button", "name": "Send", "text": "Send",
              "css": "nav > button"},
         ]},
    ]

    class Page:
        def __init__(self) -> None:
            self.reads = 0
            self.waits: list[int] = []

        def is_closed(self) -> bool:
            return False

        async def wait_for_timeout(self, ms: int) -> None:
            self.waits.append(ms)

        async def evaluate(self, _js: Any) -> dict[str, Any]:
            snapshot = snapshots[min(self.reads, len(snapshots) - 1)]
            self.reads += 1
            return snapshot

        def locator(self, selector: str) -> RecordingHandle:
            if 'data-dobot-uid="7"' in selector:
                return send
            if 'data-dobot-uid="9"' in selector:
                return decoy
            return RecordingHandle(log, selector)

        def get_by_role(self, _role: str, name: str) -> RecordingHandle:
            return send if name == "Send" else decoy

    page = Page()
    session = DriveSession()
    session._page = page
    session._elements = list(snapshots[0]["elements"])
    state = await session.click("0")
    assert 2500 in page.waits  # it waited for enablement instead of clicking blindly
    assert "click:send" in log and "click:decoy" not in log, (
        "the index was rebound to the decoy that now sits at position 0"
    )
    assert state["clicked"] == "0"
    assert state["evidence"]["url_changed"] is False  # observed, not asserted optimistically


async def test_type_text_waits_for_a_field_that_renders_late() -> None:
    """Type gets the same patience as click: one miss used to be a hard failure."""
    log: list[str] = []
    box = RecordingHandle(log, "box")

    class LateField:
        def __init__(self) -> None:
            self.reads = 0
            self.waits: list[int] = []

        def is_closed(self) -> bool:
            return False

        async def wait_for_timeout(self, ms: int) -> None:
            self.waits.append(ms)

        async def evaluate(self, _js: Any) -> dict[str, Any]:
            self.reads += 1
            elements = (
                [{"i": i, "uid": str(10 + i), "role": "link", "name": f"Nav {i}",
                  "text": f"Nav {i}", "css": f"nav > a:nth-of-type({i + 1})"} for i in range(3)]
                + [{"i": 3, "uid": "13", "role": "textbox", "name": "Message", "text": "",
                    "css": "main > div > textarea"}]
                if self.reads >= 2
                else []
            )
            return {"url": "u", "title": "t", "text": "page", "elements": elements}

        def locator(self, selector: str) -> RecordingHandle:
            return box if "data-dobot-uid=\"13\"" in selector else RecordingHandle(log, selector)

        def get_by_role(self, _role: str, name: str) -> RecordingHandle:
            return box if name == "Message" else RecordingHandle(log, f"role:{name}")

    page = LateField()
    session = DriveSession()
    session._page = page
    session._elements = []  # field index 3 does not exist in the (empty) first snapshot
    state = await session.type_text("3", "hello there")
    assert 2500 in page.waits
    assert box.filled == "hello there"
    assert state["confirmed"] is True
    assert state["target"] == "3"


def test_evidence_reports_observed_deltas() -> None:
    pre = {"url": "https://a.test", "title": "A", "text": "one", "element_count": 3}
    post = {"url": "https://b.test", "title": "A", "text": "two", "element_count": 5}
    assert _evidence(pre, post, 2) == {
        "url_changed": True,
        "title_changed": False,
        "text_changed": True,
        "elements_delta": 2,
        "downloads": 2,
    }
    same = _evidence(pre, dict(pre), 0)
    assert not any(value for value in same.values() if isinstance(value, bool))
    assert same["downloads"] == 0 and same["elements_delta"] == 0


def test_click_diagnosis_explains_disabled_obscured_and_bare_failures() -> None:
    disabled = DriveSession._click_diagnosis("5", {"disabled": True}, RuntimeError("Timeout 3000ms"))
    assert "disabled" in disabled and "wait for it to enable" in disabled
    obscured = DriveSession._click_diagnosis(
        "5", {"obscured": True}, RuntimeError("element intercepts pointer events")
    )
    assert "covers" in obscured
    bare = DriveSession._click_diagnosis("5", None, RuntimeError("boom"))
    assert bare == "click on '5' failed: boom"


async def test_click_wraps_raw_playwright_failures_with_the_target() -> None:
    log: list[str] = []

    class Page:
        def is_closed(self) -> bool:
            return False

        async def wait_for_timeout(self, _ms: int) -> None:
            pass

        async def evaluate(self, _js: Any) -> dict[str, Any]:
            return {"url": "u", "title": "t", "text": "page",
                    "elements": [{"i": 0, "uid": "1", "role": "button", "name": "Go",
                                  "text": "Go", "css": "body > button"}]}

        def locator(self, selector: str) -> RecordingHandle:
            return RecordingHandle(log, selector, click_error="element is not enabled")

        def get_by_role(self, _role: str, name: str) -> RecordingHandle:
            return RecordingHandle(log, name, click_error="element is not enabled")

    session = DriveSession()
    session._page = Page()
    session._elements = [{"i": 0, "uid": "1", "role": "button", "name": "Go", "text": "Go",
                          "css": "body > button"}]
    with pytest.raises(RuntimeError, match="click on '0' failed"):
        await session.click("0")


# ------------------------------------------------------- evidence-aware verification


async def test_click_verify_treats_a_download_as_a_response_without_change(ctx: ToolContext) -> None:
    tool = BrowserClickTool()
    tool.session = FakeDrive(state=_state(
        changed=False,  # url/title/text identical: the download IS the page response
        downloads=[{"file": "report.pdf", "path": "/downloads/report.pdf"}],
        evidence={"downloads": 1},
    ))
    result = await tool.run({"target": "12"}, ctx)
    verification = await tool.verify({"target": "12"}, result, ctx)
    assert verification.verified
    assert "report.pdf" in verification.notes


async def test_click_verify_explains_a_click_that_landed_but_did_nothing(ctx: ToolContext) -> None:
    tool = BrowserClickTool()
    tool.session = FakeDrive(state=_state(changed=False))
    result = await tool.run({"target": "12"}, ctx)
    verification = await tool.verify({"target": "12"}, result, ctx)
    assert not verification.verified
    assert "nothing changed" in verification.notes
    assert "browser_snapshot" in verification.notes  # says what to do next, not just what failed


async def test_type_result_carries_evidence_block(ctx: ToolContext) -> None:
    tool = BrowserTypeTool()
    tool.session = FakeDrive(state=_state(evidence={"url_changed": False, "downloads": 0}))
    result = await tool.run({"target": "2", "text": "hi"}, ctx)
    assert result.ok
    assert result.output["evidence"] == {"url_changed": False, "downloads": 0}
