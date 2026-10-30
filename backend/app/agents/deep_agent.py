"""Deep research subagent, powered by deepagents (LangChain's agent harness).

`deepagents` provides the batteries-included harness on top of LangGraph: a tool-calling loop with a
virtual filesystem scratchpad, planning state and subagent delegation. Dobot uses it for exactly one
job so far — *deep research*: given a question, the subagent searches the web, drafts in its
scratchpad, and returns a cited report.

The integration is deliberately narrow and honest:

* **One model, one direction.** The subagent runs on Nebius Nemotron (super tier) through the
  OpenAI-compatible adapter. Thinking traces are disabled — this loop is iterative search, not
  deliberation, and latency dominates.
* **Read-only tools.** The subagent gets `web_search` and deepagents' in-memory filesystem. It
  cannot touch your disk, your shell, or anything Dobot's own tools would gate.
* **Still inside Dobot's safety stack.** The subagent runs *inside* a Dobot step: the planner has to
  choose the `deep_research` tool, the decision engine has to authorise it, and the harness wraps
  it — deepagents never bypasses the layers that make Dobot safe.
* **Optional.** With `DEEPAGENTS_ENABLED=false` (default) nothing here loads; the Doctor reports it
  as declined with the exact switch, and Dobot's native research pipeline handles research as before.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.config import get_settings
from app.events import EventType
from app.logging_setup import get_logger

logger = get_logger(__name__)

_SYSTEM_PROMPT = """You are Dobot's deep-research specialist. Given a question, you:
1. Identify every company, product, person, or other entity explicitly requested. Do not silently
   skip an entity; if reliable information is unavailable, list it as unavailable.
2. Break the request into targeted searches and run them with the web_search tool. Search official
   filings, investor-relations pages, exchanges, and reputable financial reporting where relevant.
3. Compare independent sources, distinguish reported facts from estimates and forward-looking
   expectations, and record working notes in your scratchpad with write_file.
4. Produce a useful report with: an executive summary, an entity-by-entity comparison, explicit
   unavailable items, risks/uncertainties, and a numbered Sources list. Every non-obvious claim
   must have an inline citation like [1]. Never invent estimates, prices, or sources.
5. For investment questions, do not issue a guaranteed buy recommendation. Explain the evidence,
   valuation/earnings caveats, and that the user should verify current market data.
If the search tools fail or return nothing useful, say so plainly. Never invent sources.
"""


def deepagents_ready(settings: Any = None) -> tuple[bool, str]:
    """Whether the deep research path can run, and why not when it cannot."""
    settings = settings or get_settings()
    if not settings.deepagents_enabled:
        return False, "DEEPAGENTS_ENABLED is off"
    if not settings.has_nebius:
        return False, "NEBIUS_API_KEY is not set"
    if not settings.has_tavily:
        return False, "TAVILY_API_KEY is not set"
    try:
        import deepagents  # noqa: F401
    except Exception:  # noqa: BLE001
        return False, "the deepagents package is not installed"
    try:
        from langchain_openai import ChatOpenAI  # noqa: F401
    except Exception:  # noqa: BLE001
        return False, "the langchain-openai package is not installed"
    return True, ""


def build_deep_agent(services: Any) -> Any | None:
    """Compile the deep-research subagent, or None when unavailable."""
    settings = services.settings
    ok, _why = deepagents_ready(settings)
    if not ok:
        return None

    from deepagents import create_deep_agent
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        model=settings.nemotron_super_model,
        api_key=settings.nebius_api_key,
        base_url=settings.nebius_base_url,
        temperature=0.2,
        max_retries=1,
        # Iterative search loop: disable the thinking trace for latency, same as the main planner's
        # light tier. extra_body passes through the OpenAI-compatible payload untouched.
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )

    searched: list[str] = []
    collected: list[dict[str, Any]] = []

    async def web_search(query: str) -> str:
        """Search the web and return the top results as JSON: title, url, snippet."""
        searched.append(query)
        if hasattr(services, "bus"):
            await services.bus.emit(
                EventType.RESEARCH_PROGRESS,
                message=f"Deep research search {len(searched)}: {query[:100]}",
                stage="searching",
                queries=list(searched),
            )
        tavily = services.tavily
        if tavily is None or not tavily.available:
            return json.dumps({"error": "Tavily is not configured"})
        payload = await tavily.search(query, max_results=5)
        results = [
            {
                "title": item.get("title", "")[:200],
                "url": item.get("url", ""),
                "snippet": item.get("content", "")[:500],
            }
            for item in payload.get("results", []) or []
        ]
        collected.extend(results)
        return json.dumps({"results": results})

    return create_deep_agent(
        model=model,
        tools=[web_search],
        system_prompt=_SYSTEM_PROMPT,
        interrupt_on=None,  # read-only research: fully autonomous
    )


async def run_deep_research(services: Any, question: str, *, recursion_limit: int = 25) -> dict[str, Any]:
    """Run the deep-research subagent with bounded execution and source recovery."""
    ok, why = deepagents_ready(services.settings)
    if not ok:
        return {"ok": False, "answer": "", "engine": "deepagents", "error": why}
    try:
        agent = build_deep_agent(services)
        if agent is None:  # pragma: no cover - covered by deepagents_ready
            return {"ok": False, "answer": "", "engine": "deepagents", "error": "agent unavailable"}
        if hasattr(services, "bus"):
            await services.bus.emit(
                EventType.RESEARCH_PROGRESS,
                message="Deep research agent is analysing and cross-checking results",
                stage="synthesising",
            )
        result = await asyncio.wait_for(
            agent.ainvoke(
                {"messages": [{"role": "user", "content": question}]},
                config={"recursion_limit": recursion_limit},
            ),
            timeout=180,
        )
        answer = _extract_answer(result)
        if not answer:
            return {"ok": False, "answer": "", "engine": "deepagents", "error": "empty response"}
        sources = _extract_sources(result)
        return {
            "ok": True,
            "answer": answer,
            "engine": "deepagents",
            "sources": sources,
            "sub_queries": [],
            "error": "",
        }
    except TimeoutError:
        return {
            "ok": False,
            "answer": "",
            "engine": "deepagents",
            "error": "deep research exceeded the 3-minute time limit",
        }
    except Exception as exc:  # noqa: BLE001 - degraded report, never a crash
        logger.warning("deep research failed: %s", exc)
        return {"ok": False, "answer": "", "engine": "deepagents", "error": str(exc)[:300]}


def _extract_answer(result: Any) -> str:
    """Last AI message text from a deepagents state dict."""
    try:
        messages = result.get("messages") or []
        for message in reversed(messages):
            role = getattr(message, "type", None) or (
                message.get("role") if isinstance(message, dict) else None
            )
            if role in ("ai", "assistant"):
                content = getattr(message, "content", None)
                if content is None and isinstance(message, dict):
                    content = message.get("content")
                if isinstance(content, list):  # content parts
                    content = "".join(
                        part.get("text", "") for part in content if isinstance(part, dict)
                    )
                if content and str(content).strip():
                    return str(content).strip()
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _extract_sources(result: Any) -> list[dict[str, Any]]:
    """Recover source records from tool messages so deep reports are linkable in the UI."""
    sources: list[dict[str, Any]] = []
    messages = result.get("messages") or [] if isinstance(result, dict) else []
    for message in messages:
        role = getattr(message, "type", None) or (
            message.get("type") if isinstance(message, dict) else None
        )
        if role not in ("tool", "function"):
            continue
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            continue
        for item in payload.get("results", []) if isinstance(payload, dict) else []:
            if isinstance(item, dict) and item.get("url"):
                sources.append(
                    {
                        "title": str(item.get("title", ""))[:200],
                        "url": str(item.get("url", "")),
                        "snippet": str(item.get("snippet", ""))[:600],
                        "score": float(item.get("score", 0) or 0),
                    }
                )
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        url = source["url"].rstrip("/").lower()
        if url not in seen:
            seen.add(url)
            unique.append(source)
    return unique[:20]
