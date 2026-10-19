"""Interactive browser control — Dobot's local Playwright driver.

How browser-use and Agent S actually make an agent *use* a page: drive a real browser over an
automation protocol, snapshot the interactive elements with stable indices, then click/type by
index (or text/selector). This module is Dobot's implementation of that pattern without any
external runtime: a visible Edge/Chrome window controlled through Playwright, shared across
tool calls. Downloads triggered by clicks are saved and their path is reported.

Brittleness is fought at three seams, each observable in the outputs:

* **Identity-pinned handles.** Every element is stamped with an inert ``data-dobot-uid``
  attribute (monotonic per page load) and carries its ARIA role + accessible name. An index
  re-resolves stamp → role+name → identity-checked CSS, so a re-rendered page yields either
  the same element, a same-role/same-name replacement, or a loud "stale handle" refusal —
  never a silent click on a same-path sibling.
* **Patient, honest actions.** click *and* type wait/retry for late content, wait for
  enablement of disabled targets, and diagnose failures (``disabled`` / ``obscured`` marks
  recorded by the snapshot).
* **Observed evidence.** Actions snapshot before and after: ``changed`` compares real states
  (fresh-digests used to make it unconditionally true) and ``evidence`` reports url/title/
  text/delta/downloads as measured, so "nothing happened" is a fact the verifier can act on.

Hermes' `browser_exec` toolset stays supported in BrowserSubmitTool as an alternative backend;
this driver is the local path that always works.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from app.schemas import CheckResult, ExecutionResult, RiskLevel, VerificationResult
from app.tools.base import Tool, ToolContext

# Collect visible interactive elements, each with a reproducible CSS path used as its handle.
# Every kept element is also stamped with an inert `data-dobot-uid` attribute whose counter is
# monotonic per page lifetime: re-resolution after a re-render finds *that node* or reports
# "not found" (a safe retry) — never a same-path sibling (a wrong click).
_SNAPSHOT_JS = """
() => {
  const SEL = 'a, button, input, textarea, select, [role=button], [role=link], [role=tab], ' +
              '[role=menuitem], [role=checkbox], [role=radio], [contenteditable="true"], summary';
  const cssPath = (el) => {
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 7) {
      let part = el.tagName.toLowerCase();
      if (el.id) { parts.unshift(part + '#' + CSS.escape(el.id)); break; }
      const parent = el.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter((c) => c.tagName === el.tagName);
        if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(el) + 1) + ')';
      }
      parts.unshift(part);
      el = parent;
    }
    return parts.join(' > ');
  };
  // Condensed accessible name (ARIA AccName): aria-label > aria-labelledby > native
  // label > submit-button value > alt > title > text > placeholder > icon alt.
  const accName = (el) => {
    const clean = (s) => (s || '').trim().replace(/\\s+/g, ' ').slice(0, 120);
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) return aria;
    const ids = el.getAttribute('aria-labelledby');
    if (ids) {
      let out = '';
      for (const id of ids.split(/\\s+/)) {
        const ref = document.getElementById(id);
        if (ref) out += ' ' + (ref.innerText || ref.value || '');
      }
      const byId = clean(out);
      if (byId) return byId;
    }
    if (el.labels && el.labels.length) { const l = clean(el.labels[0].innerText); if (l) return l; }
    if (el.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes(el.type)) {
      const v = clean(el.value); if (v) return v;
    }
    const alt = clean(el.getAttribute('alt')); if (alt) return alt;
    const title = clean(el.getAttribute('title')); if (title) return title;
    const text = clean(el.innerText || el.value); if (text) return text;
    const ph = clean(el.getAttribute('placeholder')); if (ph) return ph;
    const icon = el.querySelector && el.querySelector('img[alt]');
    if (icon) return clean(icon.getAttribute('alt'));
    return '';
  };
  const roleOf = (el) => {
    const explicit = (el.getAttribute('role') || '').trim();
    if (explicit) return explicit;
    const tag = el.tagName;
    if (tag === 'A') return 'link';
    if (tag === 'BUTTON' || tag === 'SUMMARY') return 'button';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'SELECT') return 'combobox';
    if (tag === 'INPUT') {
      const t = (el.type || 'text').toLowerCase();
      if (['button', 'submit', 'reset'].includes(t)) return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'range') return 'slider';
      if (t === 'number') return 'spinbutton';
      if (t === 'search') return 'searchbox';
      if (t === 'hidden') return '';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return '';
  };
  if (!window.__dobotUidSeq) window.__dobotUidSeq = 0;
  const elements = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(SEL)) {
    if (elements.length >= 120) break;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (r.width < 1 || r.height < 1 || style.visibility === 'hidden' || style.display === 'none') continue;
    if (el.tagName === 'INPUT' && el.type === 'hidden') continue;
    const raw = (el.innerText || el.value || el.getAttribute('aria-label') ||
                 el.getAttribute('placeholder') || el.getAttribute('alt') || '');
    const text = raw.trim().replace(/\\s+/g, ' ').slice(0, 120);
    const css = cssPath(el);
    const key = css + '|' + text;
    if (seen.has(key)) continue;
    seen.add(key);
    let uid = el.getAttribute('data-dobot-uid');
    if (!uid) {
      window.__dobotUidSeq += 1;
      uid = String(window.__dobotUidSeq);
      el.setAttribute('data-dobot-uid', uid);
    }
    let obscured = false;
    try {
      const cx = Math.min(Math.max(r.left + r.width / 2, 0), Math.max(window.innerWidth - 1, 0));
      const cy = Math.min(Math.max(r.top + r.height / 2, 0), Math.max(window.innerHeight - 1, 0));
      const hit = document.elementFromPoint(cx, cy);
      obscured = !!hit && hit !== el && !el.contains(hit) && !hit.contains(el);
    } catch (e) {}
    const entry = {
      i: elements.length,
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').slice(0, 24),
      text: text,
      css: css,
      uid: uid,
      role: roleOf(el),
      name: accName(el),
    };
    if (el.disabled === true || el.getAttribute('aria-disabled') === 'true') entry.disabled = true;
    if (obscured) entry.obscured = true;
    elements.push(entry);
  }
  const body = document.body;
  const text = body ? body.innerText.replace(/\\n{3,}/g, '\\n\\n').slice(0, 4000) : '';
  return { url: location.href, title: document.title, text: text, elements: elements };
}
"""

_DIGITS = re.compile(r"^\d+$")

#: Heavy local caches are not worth cloning into Dobot's Chrome profile.
_CHROME_SKIP_DIRS = (
    "Cache",
    "Code Cache",
    "GPUCache",
    "GrShaderCache",
    "DawnCache",
    "Service Worker",
    "Extensions",
    "Crashpad",
    "component_crx_cache",
    "extensions_crx_cache",
    "optimization_guide_model_store",
    "OnDeviceHeadSuggestModel",
    "blob_storage",
)


def chrome_user_data() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(local) / "Google" / "Chrome" / "User Data"


#: Chrome profile directory whose logins Dobot should carry (override: DOBOT_CHROME_PROFILE).
def chrome_profile_name() -> str:
    return os.environ.get("DOBOT_CHROME_PROFILE", "Default")


def _clone_chrome_profile(dest: Path) -> str:
    """One-time clone of the user's Chrome profile, so their logins (Gemini) come along.

    Chrome >= 136 ignores --remote-debugging-port on the default profile, and Playwright cannot
    launch with a profile Chrome itself is using — cloning is the route browser-use settled on
    too. Only the auth-relevant files are copied: a full Default clone takes minutes, while
    cookies + storage take seconds. Returns "" on success or an error detail.
    """
    marker = dest / ".dobot-clone-ready"
    if marker.exists():
        return ""
    user_data = chrome_user_data()
    profile_name = chrome_profile_name()
    src_profile = user_data / profile_name
    if not (user_data / "Local State").exists():
        return f"no Chrome profile found at {user_data}"
    if not src_profile.exists():
        return f"no Chrome profile named '{profile_name}' at {src_profile}"
    # A partial clone from an interrupted run must not survive into this attempt.
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    dest_profile = dest / profile_name
    dest_profile.mkdir(parents=True, exist_ok=True)
    copy_dirs = (
        "Network",  # the cookie jar lives here
        "Local Storage",
        "Session Storage",
        "Sessions",
        "Device Bound Session Credentials",
    )
    copy_files = ("Preferences", "Cookies")
    try:
        if os.name == "nt":
            jobs: list[list[str]] = [
                ["robocopy", str(user_data), str(dest), "Local State", "/R:1", "/W:1",
                 "/NFL", "/NDL", "/NJH", "/NJS"],
            ]
            for name in copy_dirs:
                if (src_profile / name).exists():
                    jobs.append(
                        ["robocopy", str(src_profile / name), str(dest_profile / name), "/E",
                         "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS"]
                    )
            present = [name for name in copy_files if (src_profile / name).exists()]
            if present:
                jobs.append(
                    ["robocopy", str(src_profile), str(dest_profile), *present,
                     "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS"]
                )
            for args in jobs:
                result = subprocess.run(args, capture_output=True, timeout=120)  # noqa: S603
                if result.returncode >= 8:
                    return f"cloning Chrome profile failed (robocopy exit {result.returncode})"
        else:  # pragma: no cover - CI never clones a real profile
            shutil.copy2(user_data / "Local State", dest / "Local State")
            for name in copy_dirs:
                if (src_profile / name).exists():
                    shutil.copytree(src_profile / name, dest_profile / name, dirs_exist_ok=True)
            for name in copy_files:
                if (src_profile / name).exists():
                    shutil.copy2(src_profile / name, dest_profile / name)
        marker.write_text("ok", encoding="utf-8")
        return ""
    except Exception as exc:  # noqa: BLE001 - a failed clone must not crash the tool
        return f"cloning Chrome profile failed: {exc}"


def _digest(payload: dict[str, Any]) -> str:
    return "|".join(
        str(payload.get(key, "")) for key in ("url", "title")
    ) + "|" + str(payload.get("text", ""))[:500]


def _norm_name(value: Any) -> str:
    """Comparable form of an accessible name/label (whitespace- and case-insensitive)."""
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _evidence(pre: dict[str, Any], post: dict[str, Any], new_downloads: int = 0) -> dict[str, Any]:
    """What the action demonstrably did: observed pre/post state, never inferred."""
    return {
        "url_changed": str(pre.get("url", "")) != str(post.get("url", "")),
        "title_changed": str(pre.get("title", "")) != str(post.get("title", "")),
        "text_changed": str(pre.get("text", "")) != str(post.get("text", "")),
        "elements_delta": int(post.get("element_count") or 0) - int(pre.get("element_count") or 0),
        "downloads": int(new_downloads),
    }


class DriveSession:
    """One shared, visible browser window driven over Playwright."""

    name = "playwright-local"

    def __init__(self) -> None:
        self._pw: Any = None
        self._context: Any = None
        self._page: Any = None
        self._elements: list[dict[str, Any]] = []
        self._last_digest: str = ""
        self._downloads: list[dict[str, Any]] = []
        self._attached = False
        self._login_note = ""  # set when the cloned profile starts signed out (cookie purge)
        self.launched_with = ""  # cdp | chrome | msedge | chromium — observable for debugging
        local = os.environ.get("LOCALAPPDATA") or str(Path.home())
        self._profile_root = Path(local) / "Dobot" / "browser-profile"
        self._download_dir = Path.home() / "Downloads"

    # ---------------------------------------------------------------- lifecycle
    async def ensure(self) -> str:
        """Start the browser if needed. Returns "" on success or an error detail."""
        try:
            if self._page is not None and not self._page.is_closed():
                return ""
        except Exception:  # noqa: BLE001 - a dead handle just restarts the browser
            self._page = None
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
            return f"playwright is not installed ({exc})"
        try:
            if self._pw is None:
                self._pw = await async_playwright().start()
            if await self._try_attach():
                return ""
            errors: list[str] = []
            # Chrome first: its cloned profile carries the user's own logins (Gemini etc.).
            for channel in ("chrome", "msedge", None):
                profile = self._profile_root / (channel or "chromium")
                if channel == "chrome":
                    clone_err = await asyncio.to_thread(_clone_chrome_profile, profile)
                    if clone_err:
                        errors.append(clone_err)
                        continue
                else:
                    profile.mkdir(parents=True, exist_ok=True)
                try:
                    self._context = await self._pw.chromium.launch_persistent_context(
                        str(profile),
                        channel=channel,
                        headless=False,
                        accept_downloads=True,
                        args=["--no-first-run", "--no-default-browser-check"],
                        viewport={"width": 1280, "height": 900},
                    )
                    self.launched_with = channel or "chromium"
                    break
                except Exception as exc:  # noqa: BLE001 - try the next browser channel
                    errors.append(f"{channel or 'bundled'}: {str(exc)[:160]}")
            if self._context is None:
                detail = "; ".join(errors) or "no browser channel available"
                await self.close()
                return f"could not launch a browser: {detail}"
            self._page = (
                self._context.pages[0] if self._context.pages else await self._context.new_page()
            )
            self._page.on("download", self._on_download)
            self._hook_download_pages(self._context)
            if self.launched_with == "chrome":
                await self._check_login_carry()
            return ""
        except Exception as exc:  # noqa: BLE001
            await self.close()
            return f"browser launch failed: {exc}"

    async def _try_attach(self) -> bool:
        """Attach to a browser already running with remote debugging, in a fresh tab.

        Their tab is never hijacked, and close() only closes our tab — never their browser.
        """
        endpoints = [
            item.strip()
            for item in os.environ.get(
                "DOBOT_CDP_URLS", "http://127.0.0.1:9222,http://127.0.0.1:9223"
            ).split(",")
            if item.strip()
        ]
        try:
            async with httpx.AsyncClient(timeout=0.6) as client:
                for endpoint in endpoints:
                    try:
                        probe = await client.get(f"{endpoint}/json/version")
                    except Exception:  # noqa: BLE001 - nothing listening there
                        continue
                    if probe.status_code != 200:
                        continue
                    try:
                        browser = await self._pw.chromium.connect_over_cdp(endpoint)
                        context = (
                            browser.contexts[0] if browser.contexts else await browser.new_context()
                        )
                        self._context = context
                        self._page = await context.new_page()
                        self._attached = True
                        self.launched_with = "cdp"
                        self._page.on("download", self._on_download)
                        self._hook_download_pages(context)
                        return True
                    except Exception:  # noqa: BLE001 - endpoint answered but connect failed
                        continue
        except Exception:  # noqa: BLE001 - attach is best-effort; launching still works
            return False
        return False

    def _hook_download_pages(self, context: Any) -> None:
        """Downloads triggered from popups/tabs report on their own page — hook them too."""
        try:
            if getattr(context, "_dobot_download_hook", False):
                return
            context.on("page", lambda page: page.on("download", self._on_download))
            context._dobot_download_hook = True  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - best effort; main-page downloads still work
            pass

    async def _check_login_carry(self) -> None:
        """Detect the Windows Chrome cookie purge and say so instead of starting silently signed out.

        Chrome 151+ (Windows) binds cookie encryption to the original profile path: the first
        launch of a cloned user-data-dir cannot read the copied login cookies and deletes them
        (verified: 3708 cookies -> 6, every auth cookie gone). Only logins made *inside* this
        window survive, so the honest move is to tell the user to sign in once here — that
        login persists across every future run.
        """
        try:
            cookies = await self._context.cookies()
        except Exception:  # noqa: BLE001 - guidance only, never block the launch
            return
        # Read before any navigation: a purged clone holds zero cookies at launch, while a
        # clone with a real login carries dozens (Google alone sets 15+).
        if len(cookies) < 10:
            self._login_note = (
                "this Chrome window starts signed out: Windows Chrome purges copied login "
                "cookies when a cloned profile is first opened, so logins from your own "
                "Chrome cannot carry over. Sign in once inside this window (e.g. "
                "Google/Gemini) — that login persists for every future Dobot run."
            )
        else:
            self._login_note = ""

    async def _on_download(self, download: Any) -> None:
        """Playwright schedules async handlers; save the file and remember where it went."""
        suggested = getattr(download, "suggested_filename", "") or "download.bin"
        record: dict[str, Any] = {"file": suggested, "url": getattr(download, "url", "")}
        try:
            self._download_dir.mkdir(parents=True, exist_ok=True)
            dest = self._download_dir / suggested
            counter = 1
            while dest.exists():
                dest = self._download_dir / f"{counter}-{suggested}"
                counter += 1
            await download.save_as(str(dest))
            record["path"] = str(dest)
        except Exception as exc:  # noqa: BLE001
            record["error"] = str(exc)
        self._downloads.append(record)

    async def close(self) -> None:
        self._login_note = ""
        if self._attached:
            page, self._page = self._page, None
            try:
                if page is not None and not page.is_closed():
                    await page.close()
            except Exception:  # noqa: BLE001 - their browser may already be gone
                pass
            # Never close a browser we do not own: drop our tab and the driver connection only.
            self._attached = False
            self._context = None
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._pw = None
            self._elements = []
            return
        for closer in (
            lambda: self._context and self._context.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                handle = closer()
                if handle is not None:
                    await handle
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        self._context = None
        self._pw = None
        self._page = None
        self._elements = []

    # ------------------------------------------------------------------ actions
    async def goto(self, url: str) -> dict[str, Any]:
        err = await self.ensure()
        if err:
            raise RuntimeError(err)
        await self._page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await self._page.wait_for_timeout(600)
        state = await self.snapshot(fresh=True)
        # Single-page apps (Gemini, dashboards) paint their interactive UI after DOMContentLoaded;
        # keep re-reading until the page has real content or we run out of patience.
        for _ in range(2):
            if int(state.get("element_count") or 0) >= 5 and len(str(state.get("text", ""))) >= 200:
                break
            await self._page.wait_for_timeout(1500)
            state = await self.snapshot(fresh=True)
        return state

    async def snapshot(self, fresh: bool = False, wait_seconds: float = 0) -> dict[str, Any]:
        err = await self.ensure()
        if err:
            raise RuntimeError(err)
        if wait_seconds:
            # Slow content (image generation, AI replies) needs patience the agent can express.
            await self._page.wait_for_timeout(int(min(float(wait_seconds), 30.0) * 1000))
        if fresh:
            self._last_digest = ""
        state = await self._page.evaluate(_SNAPSHOT_JS)
        if self._login_note:
            state["login_note"] = self._login_note
        self._elements = list(state.get("elements") or [])
        digest = _digest(state)
        state["changed"] = digest != self._last_digest
        self._last_digest = digest
        state["element_count"] = len(self._elements)
        if self._downloads:
            state["downloads"] = list(self._downloads)
        return state

    def _entry_for(self, target: str) -> dict[str, Any]:
        """The snapshot record an index points at (digit targets only)."""
        index = int(str(target).strip())
        if index >= len(self._elements):
            raise ValueError(
                f"element #{index} is not in the last snapshot "
                f"({len(self._elements)} elements) — call browser_navigate or browser_snapshot first"
            )
        return self._elements[index]

    def _find_entry(self, wanted: dict[str, Any]) -> dict[str, Any] | None:
        """Locate the *same* element inside the current snapshot.

        Identity stamp first (same node), then role+name (a re-rendered replacement that
        still plays the same role with the same accessible name). None means the element
        is gone from the page as it stands now.
        """
        uid = str(wanted.get("uid") or "")
        if uid:
            for element in self._elements:
                if str(element.get("uid") or "") == uid:
                    return element
        name = _norm_name(wanted.get("name") or wanted.get("text"))
        role = str(wanted.get("role") or "")
        if name and role:
            for element in self._elements:
                if str(element.get("role") or "") == role and _norm_name(
                    element.get("name") or element.get("text")
                ) == name:
                    return element
        return None

    async def _resolve_entry(self, entry: dict[str, Any]) -> Any:
        """Re-find the element an index pointed at, in an order that cannot drift.

        1. the identity stamp — the exact node the snapshot saw;
        2. role + accessible name — survives a re-render that replaced the node;
        3. the recorded CSS path, identity-checked against the live accessible name —
           if it now resolves to a different-looking element, refuse loudly (stale)
           instead of clicking a same-path sibling.
        """
        page = self._page
        uid = str(entry.get("uid") or "")
        if uid:
            try:
                locator = page.locator(f'[data-dobot-uid="{uid}"]').first
                await locator.wait_for(state="visible", timeout=1500)
                return locator
            except Exception:  # noqa: BLE001 - node replaced by a re-render; fall through
                pass
        name = str(entry.get("name") or entry.get("text") or "").strip()
        role = str(entry.get("role") or "")
        if name and role:
            try:
                locator = page.get_by_role(role, name=name).first
                await locator.wait_for(state="visible", timeout=1500)
                return locator
            except Exception:  # noqa: BLE001 - not re-resolvable by name; try the CSS path
                pass
        css = str(entry.get("css") or "")
        if not css:
            raise ValueError(
                f"'{name or 'element'}' has no usable handle — re-read the page with "
                "browser_snapshot and use an index from the new list"
            )
        locator = page.locator(css).first
        try:
            await locator.wait_for(state="visible", timeout=1500)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"element '{name or css}' is no longer on the page (its recorded selector "
                f"'{css}' did not appear) — the page changed; re-read it with browser_snapshot "
                "and use a fresh index"
            ) from exc
        if name:
            try:
                live = await locator.evaluate(
                    "el => ((el.getAttribute('aria-label') || el.innerText || el.value || '') + '')"
                    ".trim().slice(0, 120)"
                )
            except Exception:  # noqa: BLE001 - no evaluate on a lightweight handle
                live = ""
            live_norm, name_norm = _norm_name(live), _norm_name(name)
            if live_norm and name_norm and name_norm not in live_norm and live_norm not in name_norm:
                raise ValueError(
                    f"stale handle: index now resolves to '{str(live)[:60]}' but the snapshot "
                    f"recorded '{name[:60]}' — the page re-rendered; re-read it with "
                    "browser_snapshot before acting again"
                )
        return locator

    async def _locate(self, target: str) -> Any:
        """Resolve an element index from the last snapshot, a CSS selector, or visible text."""
        page = self._page
        target = str(target).strip()
        if _DIGITS.match(target):
            return await self._resolve_entry(self._entry_for(target))
        strategies = [
            ("css selector", lambda: page.locator(target).first),
            ("visible text", lambda: page.get_by_text(target, exact=False).first),
            ("placeholder", lambda: page.get_by_placeholder(target).first),
            ("label", lambda: page.get_by_label(target).first),
            ("button name", lambda: page.get_by_role("button", name=target).first),
        ]
        tried: list[str] = []
        for label, make in strategies:
            try:
                locator = make()
                await locator.wait_for(state="visible", timeout=1200)
                return locator
            except Exception:  # noqa: BLE001 - next strategy
                tried.append(label)
        # Last resort: the caller may have named a tag or fragment ("textarea") that only appears
        # inside a snapshot handle ("... rich-textarea > div") — match against recorded handles.
        needle = target.lower()
        for element in self._elements:
            handle = str(element.get("css", ""))
            if needle and needle in f"{handle} {element.get('text', '')}".lower():
                try:
                    locator = page.locator(handle).first
                    await locator.wait_for(state="visible", timeout=1200)
                    return locator
                except Exception:  # noqa: BLE001 - try the next recorded handle
                    continue
        raise ValueError(
            f"no visible element matched '{target}' "
            f"(tried: {', '.join(tried)}; then snapshot handles) — "
            "use an element index from the latest browser_navigate/browser_snapshot output"
        )

    async def click(self, target: str) -> dict[str, Any]:
        err = await self.ensure()
        if err:
            raise RuntimeError(err)
        # The element may not exist YET (AI reply still generating, lazy result list), and the
        # page may re-render between snapshot and action. For index targets the ORIGINAL record
        # is pinned across retries: a fresh snapshot must never silently rebind the agent's index
        # to a different node — re-resolution goes stamp → role+name → identity-checked CSS, and
        # a disabled target waits for enablement instead of failing on the first click.
        locator: Any = None
        entry: dict[str, Any] | None = None
        last_error: Exception | None = None
        for delay_ms in (0, 2500, 4000):
            if delay_ms:
                await self._page.wait_for_timeout(delay_ms)
                await self.snapshot(fresh=True)
            try:
                if _DIGITS.match(str(target).strip()):
                    if entry is None:
                        entry = self._entry_for(target)
                    else:
                        entry = self._find_entry(entry) or entry
                    locator = await self._resolve_entry(entry)
                    if entry.get("disabled"):
                        raise ValueError(f"element #{target} is disabled — waiting for it to enable")
                else:
                    locator = await self._locate(target)
                last_error = None
                break
            except ValueError as exc:
                last_error = exc
        if last_error is not None or locator is None:
            raise last_error or ValueError(f"could not locate '{target}'")

        # Pre-action baseline: post-action `changed` compares against THIS state, so
        # "nothing happened" is an observation rather than a fresh-digest artefact
        # (fresh snapshots used to make changed=True unconditionally).
        pre = await self.snapshot()
        downloads_before = len(self._downloads)
        try:
            await locator.click(timeout=3000)
        except Exception as exc:
            raise RuntimeError(self._click_diagnosis(target, entry, exc)) from exc
        await self._page.wait_for_timeout(600)
        # Downloads report their saved path asynchronously — wait briefly so the path can be
        # included in this step's output instead of surfacing only in a later snapshot.
        for _ in range(3):
            if len(self._downloads) > downloads_before:
                break
            await self._page.wait_for_timeout(500)
        state = await self.snapshot()
        state["clicked"] = str(target)
        state["evidence"] = _evidence(pre, state, len(self._downloads) - downloads_before)
        return state

    @staticmethod
    def _click_diagnosis(target: str, entry: dict[str, Any] | None, exc: Exception) -> str:
        """Turn a raw Playwright click failure into an actionable, honest sentence."""
        first_line = (str(exc) or exc.__class__.__name__).splitlines()[0][:200]
        message = f"click on '{target}' failed: {first_line}"
        if entry:
            if entry.get("disabled"):
                message += (
                    " — the element is disabled (snapshots mark it `disabled`); "
                    "wait for it to enable, then retry"
                )
            elif entry.get("obscured"):
                message += (
                    " — another element covers it (snapshots mark it `obscured`); "
                    "re-read the page and click the covering element or its parent"
                )
        return message

    async def type_text(self, target: str, text: str, press_enter: bool = False) -> dict[str, Any]:
        err = await self.ensure()
        if err:
            raise RuntimeError(err)
        # Fields appear late too (chat composers render after the surrounding chrome): the same
        # patient, identity-pinned resolution as click, instead of one shot then a hard failure.
        locator: Any = None
        entry: dict[str, Any] | None = None
        last_error: Exception | None = None
        for delay_ms in (0, 2500, 4000):
            if delay_ms:
                await self._page.wait_for_timeout(delay_ms)
                await self.snapshot(fresh=True)
            try:
                if _DIGITS.match(str(target).strip()):
                    if entry is None:
                        entry = self._entry_for(target)
                    else:
                        entry = self._find_entry(entry) or entry
                    locator = await self._resolve_entry(entry)
                    if entry.get("disabled"):
                        raise ValueError(f"element #{target} is disabled — waiting for it to enable")
                else:
                    locator = await self._locate(target)
                last_error = None
                break
            except ValueError as exc:
                last_error = exc
        if last_error is not None or locator is None:
            raise last_error or ValueError(f"could not locate '{target}'")
        pre = await self.snapshot()
        await locator.click(timeout=3000)
        try:
            await locator.fill(text, timeout=2000)
        except Exception:  # noqa: BLE001 - rich editors (contenteditable) reject fill()
            await self._page.keyboard.insert_text(text)
        confirmed = await self._read_back(locator, text)
        if press_enter:
            await self._page.keyboard.press("Enter")
            await self._page.wait_for_timeout(600)
        state = await self.snapshot()
        state["evidence"] = _evidence(pre, state)
        state["typed"] = text[:400]
        state["target"] = str(target)
        state["confirmed"] = confirmed
        return state

    @staticmethod
    async def _read_back(locator: Any, text: str) -> bool:
        """Confirm the text actually landed in the field — never trust the write alone."""
        try:
            value = await locator.input_value(timeout=800)
            if value is not None:
                return text in str(value)
        except Exception:  # noqa: BLE001 - contenteditable has no value attribute
            pass
        try:
            inner = await locator.inner_text(timeout=800)
            return text in str(inner)
        except Exception:  # noqa: BLE001
            return False

    async def submit(
        self, url: str, fields: dict[str, Any], submit_selector: str = ""
    ) -> dict[str, Any]:
        err = await self.ensure()
        if err:
            raise RuntimeError(err)
        page = self._page
        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(500)
        before = await page.url
        for name, value in (fields or {}).items():
            locator = page.locator(f'[name="{name}"]').first
            try:
                await locator.wait_for(state="visible", timeout=1200)
            except Exception:  # noqa: BLE001 - fall back to label/text lookup
                locator = await self._locate(str(name))
            await locator.fill(str(value), timeout=2000)
        submitter = (
            page.locator(submit_selector).first
            if submit_selector
            else page.locator('button[type="submit"], input[type="submit"], button:has-text("Sign in")').first
        )
        await submitter.click(timeout=3000)
        await page.wait_for_timeout(1200)
        state = await self.snapshot(fresh=True)
        return {
            "ok": True,
            "confirmation": (state.get("title") or state.get("text", ""))[:400],
            "page_changed": state.get("url") != before or bool(state.get("changed")),
            "url": state.get("url", ""),
        }


_DRIVE: DriveSession | None = None


def get_drive() -> DriveSession:
    """The shared driver (module-level so every tool call sees the same window)."""
    global _DRIVE
    if _DRIVE is None:
        _DRIVE = DriveSession()
    return _DRIVE


async def close_drive() -> None:
    """Close the shared browser window on shutdown; no-op if it was never started."""
    if _DRIVE is not None:
        await _DRIVE.close()


def _state_output(state: dict[str, Any]) -> dict[str, Any]:
    """Trim a snapshot down to what the planner and answer need.

    Identity stamps (``uid``) are internal plumbing — the agent never acts on them — but
    role, accessible name, and the ``disabled``/``obscured`` marks are decision-relevant
    and are surfaced verbatim.
    """
    elements = [
        {key: value for key, value in element.items() if key != "uid"}
        for element in state.get("elements", [])
    ]
    text_entry_roles = {"textbox", "searchbox", "combobox", "spinbutton"}
    # The planner historically guessed "textarea" or index 0; naming the text-entry elements
    # makes the right target the obvious choice. Role (incl. contenteditable divs) plus the
    # legacy tag/css heuristics for snapshots recorded before roles existed.
    input_indices = [
        int(element.get("i", -1))
        for element in elements
        if element.get("role") in text_entry_roles
        or element.get("tag") in {"input", "textarea", "select"}
        or "textarea" in str(element.get("css", "")).lower()
        or "textbox" in str(element.get("css", "")).lower()
    ]
    return {
        "url": state.get("url", ""),
        "title": state.get("title", ""),
        "text": str(state.get("text", ""))[:2000],
        "elements": elements,
        "element_count": state.get("element_count", 0),
        "input_indices": input_indices,
        "changed": bool(state.get("changed")),
        "evidence": state.get("evidence") or {},
        "downloads": state.get("downloads", []),
        "login_note": str(state.get("login_note", "")),
    }


class _DriveTool(Tool):
    """Shared plumbing: a session hook for tests, plus cancellation checks."""

    session: Any = None

    def _drive(self) -> Any:
        return self.session or get_drive()

    async def _state(self, params: dict[str, Any], ctx: ToolContext) -> tuple[dict[str, Any] | None, str]:
        ctx.check_cancelled()
        err = await self._drive().ensure()
        if err:
            return None, err
        return None, ""


class BrowserNavigateTool(_DriveTool):
    name = "browser_navigate"
    description = (
        "Open a URL in the visible browser window Dobot controls and return the page title, text, "
        "and an indexed list of interactive elements (use an element's index as `target` for "
        "browser_click / browser_type). Use this — not app_open — when the task requires typing, "
        "clicking, or downloading on the page. Indices refresh after every action."
    )
    risk_floor = RiskLevel.LOW
    proactive_ok = True
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "Absolute http(s) URL"}},
        "required": ["url"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        url = self.require(params, "url")
        drive = self._drive()
        try:
            state = await drive.goto(url)
        except Exception as exc:  # noqa: BLE001
            return self.fail(self.name, str(exc), started)
        return self.ok(self.name, _state_output(state), started)

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        checks = [
            CheckResult(name="page_loaded", passed=bool(output.get("title") or output.get("text")),
                        detail=str(output.get("url", ""))[:120]),
            CheckResult(name="interactive_elements_indexed",
                        passed=int(output.get("element_count") or 0) > 0,
                        detail=f"{output.get('element_count', 0)} elements"),
        ]
        notes = "element indices from this snapshot are the handles for browser_click/browser_type"
        if output.get("login_note"):
            notes = f"{notes}. {output['login_note']}"
        return VerificationResult(
            verified=all(check.passed for check in checks),
            checks=checks,
            notes=notes,
        )


class BrowserSnapshotTool(_DriveTool):
    name = "browser_snapshot"
    description = (
        "Re-read the current page in the visible browser: title, text, and a fresh indexed list of "
        "interactive elements with their role and accessible name — plus `disabled`/`obscured` "
        "marks when an element cannot be acted on. Use after clicks, navigation, or when a page "
        "has finished loading something new; element indices from older snapshots may be stale. "
        "Pass wait_seconds when the page is waiting on slow content (image generation, an AI "
        "reply) before you need the result."
    )
    risk_floor = RiskLevel.LOW
    proactive_ok = True
    parameters = {
        "type": "object",
        "properties": {
            "wait_seconds": {
                "type": "integer",
                "minimum": 0,
                "maximum": 30,
                "description": "Seconds to wait before reading (slow content: image generation, AI replies)",
            }
        },
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        wait_seconds = int(params.get("wait_seconds") or 0)
        try:
            state = await self._drive().snapshot(fresh=True, wait_seconds=wait_seconds)
        except Exception as exc:  # noqa: BLE001
            return self.fail(self.name, str(exc), started)
        return self.ok(self.name, _state_output(state), started)


class BrowserClickTool(_DriveTool):
    name = "browser_click"
    description = (
        "Click an element in the visible browser by index (from the latest browser_navigate / "
        "browser_snapshot output), its accessible name, CSS selector, or visible text. An index "
        "keeps pointing at the same element across page re-renders (resolved by identity, then "
        "role+name), and if the target is not on the page yet (content still loading/generating), "
        "the click waits and retries before failing — when it does fail, wait with "
        "browser_snapshot(wait_seconds=...) and retry instead of giving up. Returns the page state "
        "after the click with an `evidence` block (what actually changed); snapshots mark elements "
        "`disabled` or `obscured`, and failures say which. If the click triggers a file download, "
        "its saved path is included."
    )
    risk_floor = RiskLevel.MEDIUM
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Element index, CSS selector, or visible text"},
            "description": {"type": "string", "description": "What clicking this accomplishes"},
        },
        "required": ["target"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        target = self.require(params, "target")
        try:
            state = await self._drive().click(target)
        except Exception as exc:  # noqa: BLE001
            return self.fail(self.name, str(exc), started)
        return self.ok(self.name, _state_output(state), started)

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        evidence = output.get("evidence") or {}
        new_downloads = int(evidence.get("downloads") or 0)
        # A download IS a page response even when url/title/text stayed identical.
        responded = bool(output.get("changed")) or new_downloads > 0
        checks = [
            CheckResult(name="click_delivered", passed=result.ok),
            CheckResult(
                name="page_responded",
                passed=responded,
                detail=(
                    "url/title/text changed"
                    if output.get("changed")
                    else f"{new_downloads} download(s)" if new_downloads
                    else "no visible page change"
                ),
            ),
        ]
        downloads = output.get("downloads") or []
        notes = ""
        if downloads:
            latest = downloads[-1]
            notes = f"download: {latest.get('path') or latest.get('error', 'unknown')}"
        elif not responded:
            notes = (
                "the click landed but nothing changed — the target may be covered (snapshots "
                "mark elements `obscured`) or the action opened a new tab; re-read with "
                "browser_snapshot and retry"
            )
        return VerificationResult(
            verified=all(check.passed for check in checks), checks=checks, notes=notes
        )


class BrowserTypeTool(_DriveTool):
    name = "browser_type"
    description = (
        "Type text into an input, textarea, or contenteditable element in the visible browser, by "
        "index (from the latest snapshot), accessible name, CSS selector, or visible text — with "
        "the same wait-and-retry as browser_click when the field has not rendered yet. Optionally "
        "press Enter afterwards (for chat/search boxes). The output's `confirmed` field means the "
        "text was read back from the field, not just written."
    )
    risk_floor = RiskLevel.MEDIUM
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Element index, CSS selector, or visible text"},
            "text": {"type": "string", "description": "Text to type"},
            "press_enter": {"type": "boolean", "default": False},
            "description": {"type": "string", "description": "What typing this accomplishes"},
        },
        "required": ["target", "text"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        target = self.require(params, "target")
        text = self.require(params, "text")
        press_enter = bool(params.get("press_enter", False))
        try:
            state = await self._drive().type_text(target, text, press_enter=press_enter)
        except Exception as exc:  # noqa: BLE001
            return self.fail(self.name, str(exc), started)
        return self.ok(self.name, _state_output(state) | {"confirmed": state.get("confirmed", False)},
                       started)

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        confirmed = bool(output.get("confirmed"))
        checks = [
            CheckResult(name="typing_delivered", passed=result.ok),
            CheckResult(name="text_read_back", passed=confirmed,
                        detail="field contains the typed text" if confirmed else "could not read the field back"),
        ]
        return VerificationResult(
            verified=all(check.passed for check in checks),
            checks=checks,
            notes=(
                "text verified by reading the field back, not by the write succeeding"
                if confirmed
                else "the typed text could not be read back — it did not land in the field"
            ),
        )


_DRIVE_AVAILABLE_MSG = (
    "interactive browser control needs the local Playwright driver (pip install playwright) "
    "or the Hermes runtime (AGENT_RUNTIME=cli or remote)"
)


async def drive_submit(session: Any, url: str, fields: dict[str, Any], submit_selector: str) -> dict[str, Any]:
    """Shared entry point so BrowserSubmitTool can prefer the local driver."""
    err = await session.ensure()
    if err:
        return {"ok": False, "error": err}
    try:
        return await session.submit(url, fields, submit_selector)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
