"""Browser tools.

Read paths fetch over HTTP through the sandbox's host allowlist. Interactive work (click, type,
download) runs on the local Playwright driver in `app.tools.browser_drive` — a real, visible
window — with Hermes' `browser_exec` toolset as a fallback backend for browser_submit.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

import httpx

from app.agents.sandbox import host_of
from app.schemas import CheckResult, ExecutionResult, RiskLevel, VerificationResult
from app.tools.base import Tool, ToolContext

_SCRIPT_STYLE = re.compile(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>")
_TAGS = re.compile(r"(?s)<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_TITLE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")


def html_to_text(html: str, limit: int = 20000) -> str:
    text = _SCRIPT_STYLE.sub(" ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", text)
    text = _TAGS.sub(" ", text)
    text = _WS.sub(" ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()[:limit]


def _title_of(html: str) -> str:
    match = _TITLE.search(html)
    return match.group(1).strip()[:200] if match else ""


class BrowserSession:
    """A tiny shared session so page-change verification has something to compare against."""

    def __init__(self) -> None:
        self.client: httpx.AsyncClient | None = None
        self.last_url: str = ""
        self.last_digest: str = ""

    def _http(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
                headers={"User-Agent": "Dobot/0.1 (+personal-ai-operating-layer)"},
            )
        return self.client

    async def fetch(self, url: str, ctx: ToolContext) -> dict[str, Any]:
        host = host_of(url)
        assert ctx.sandbox is not None
        await ctx.sandbox.check_host(host)
        response = await self._http().get(url)
        html = response.text
        digest = hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest()
        changed = digest != self.last_digest
        self.last_url = str(response.url)
        self.last_digest = digest
        return {
            "url": str(response.url),
            "status": response.status_code,
            "title": _title_of(html),
            "text": html_to_text(html),
            "digest": digest,
            "page_changed": changed,
            "bytes": len(html.encode("utf-8", errors="replace")),
        }

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()


class BrowserOpenTool(Tool):
    name = "browser_open"
    description = (
        "Fetch a web page over HTTP and return its title and readable text. This does NOT open a "
        "visible browser window and cannot interact with the page; to show a page in the user's "
        "browser, use app_open(app='browser', target='<url>'), and to work inside a page (type, "
        "click, download), use browser_navigate. Reads only — never submits."
    )
    risk_floor = RiskLevel.LOW
    proactive_ok = True
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string"}, "wait_seconds": {"type": "number", "default": 0}},
        "required": ["url"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        url = self.require(params, "url")
        session: BrowserSession = ctx.browser or BrowserSession()
        try:
            page = await session.fetch(url, ctx)
        except Exception as exc:  # noqa: BLE001
            return self.fail(self.name, f"could not open {url}: {exc}", started)
        return self.ok(self.name, page, started)

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        status = int(output.get("status") or 0)
        checks = [
            CheckResult(name="http_ok", passed=200 <= status < 400, detail=f"status={status}"),
            CheckResult(name="content_present", passed=bool(output.get("text")), detail=f"{output.get('bytes', 0)} bytes"),
        ]
        return VerificationResult(verified=all(check.passed for check in checks), checks=checks)


class BrowserReadTool(BrowserOpenTool):
    name = "browser_read"
    description = (
        "Read the readable text of a web page over HTTP (same as browser_open, read-only intent). "
        "Does not open a visible window and cannot click or type."
    )
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    }


class BrowserSubmitTool(Tool):
    name = "browser_submit"
    description = (
        "Fill in and submit a whole form on a web page: navigate to the URL, fill every named "
        "field, and click submit. Runs on the local Playwright driver when available, else the "
        "Hermes runtime. For single clicks or typing into a live page, prefer browser_click / "
        "browser_type. Always requires user approval."
    )
    risk_floor = RiskLevel.HIGH
    session: Any = None
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "fields": {"type": "object", "description": "Field name → value"},
            "submit_selector": {"type": "string"},
            "description": {"type": "string"},
        },
        "required": ["url"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        url = self.require(params, "url")
        fields = params.get("fields") or {}
        selector = str(params.get("submit_selector", ""))
        # Prefer the local Playwright driver; fall back to Hermes when it is configured.
        from app.tools.browser_drive import drive_submit, get_drive

        result = await drive_submit(self.session or get_drive(), url, fields, selector)
        if not result.get("ok") and ctx.hermes is not None and getattr(ctx.hermes, "available", False):
            result = await ctx.hermes.browser_submit(
                url=url, fields=fields, submit_selector=selector
            )
        if not result.get("ok"):
            return self.fail(
                self.name,
                str(result.get("error") or "browser submit failed; Dobot will not fake a submission"),
                started,
            )
        return self.ok(self.name, result, started)

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        checks = [
            CheckResult(name="runtime_reported_success", passed=bool(output.get("ok"))),
            CheckResult(
                name="page_confirmed",
                passed=bool(output.get("confirmation") or output.get("page_changed")),
                detail=str(output.get("confirmation", ""))[:120],
            ),
        ]
        return VerificationResult(
            verified=all(check.passed for check in checks),
            checks=checks,
            notes="submission is confirmed by page state, never by the runtime's own claim alone",
        )
