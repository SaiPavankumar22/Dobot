"""Browser tools.

Read paths fetch over HTTP through the sandbox's host allowlist. Interactive paths (click, type,
submit, signed-in sessions) are delegated to Hermes' `browser_exec` toolset, because that is where a
real browser profile lives — and because the decision engine gates submits behind approval.
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
    description = "Open a web page and return its title and readable text."
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
    description = "Read the readable text of a web page (same as browser_open, read-only intent)."
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    }


class BrowserSubmitTool(Tool):
    name = "browser_submit"
    description = (
        "Fill in and submit a form on a web page using the Hermes browser session. "
        "Always requires user approval."
    )
    risk_floor = RiskLevel.HIGH
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
        if ctx.hermes is None or not getattr(ctx.hermes, "available", False):
            return self.fail(
                self.name,
                "submitting forms needs the Hermes browser runtime (AGENT_RUNTIME=cli or remote); "
                "Dobot will not fake a submission",
                started,
            )
        result = await ctx.hermes.browser_submit(
            url=url,
            fields=params.get("fields") or {},
            submit_selector=str(params.get("submit_selector", "")),
        )
        if not result.get("ok"):
            return self.fail(self.name, str(result.get("error", "browser submit failed")), started)
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
