"""LangSmith observability — optional, off unless a key is configured.

Dobot already records what it did locally (the run journal, the cost ledger, the event bus). LangSmith
adds the piece a local journal cannot give you: a hosted, searchable trace of every stage with the
inputs, outputs and token usage of each model call, so you can answer "what did the agent actually do
and what did it cost?" across runs.

Design rules:

* **Off by default.** With no ``LANGSMITH_API_KEY`` the tracer is a null object and tracing costs
  nothing — not a branch, not a network call, not a log line.
* **Never load-bearing.** Tracing wraps work; it never changes outcomes. Every LangSmith call is
  wrapped so a network failure degrades to a debug log, never to a failed task.
* **Flat spans.** Each traced stage is one run (``harness.step``, ``nemotron.chat``) tagged with the
  task, step and tool that produced it. Nesting can come later; correctness of the journal comes first.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.config import get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)


def _safe(value: Any) -> Any:
    """LangSmith rejects non-JSON payloads; stringify instead of failing the trace."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


class _SpanRecorder:
    """What a traced block reports back about its outcome."""

    def __init__(self) -> None:
        self.outputs: dict[str, Any] = {}
        self.error: str | None = None

    def record(self, **fields: Any) -> None:
        self.outputs.update(fields)

    def fail(self, error: Any) -> None:
        name = type(error).__name__ if isinstance(error, BaseException) else ""
        self.error = f"{name}: {error}".strip(": ")[:300]


class _NullSpan:
    def record(self, **fields: Any) -> None: ...
    def fail(self, error: Any) -> None: ...


class _NullTracer:
    """The default: structurally present, operationally invisible."""

    enabled = False

    @contextmanager
    def span(self, name: str, **fields: Any):
        yield _NullSpan()


class _LangsmithTracer:
    """Creates one LangSmith run per span — without ever blocking the request path.

    The client's HTTP calls are pushed to a worker thread and never awaited by the caller: a slow or
    unreachable LangSmith endpoint must cost background noise, not user-visible latency (measured
    cost of the naive sync version was seconds per model call once retries kicked in). A circuit
    breaker retires the tracer after repeated failures — at that point the local journal is the
    record of truth anyway.
    """

    enabled = True

    #: After this many consecutive failed background submissions the tracer disables itself.
    MAX_CONSECUTIVE_FAILURES = 5

    def __init__(self, client: Any, project: str) -> None:
        self._client = client
        self._project = project
        self._failures = 0
        self._retired = False
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="langsmith")

    def _submit(self, work: Any) -> None:
        if self._retired:
            return

        def _run() -> None:
            try:
                work()
                self._failures = 0
            except Exception as exc:  # noqa: BLE001 - network failures are expected eventually
                self._failures += 1
                logger.debug("langsmith submission failed (%s/%s): %s",
                             self._failures, self.MAX_CONSECUTIVE_FAILURES, exc)
                if self._failures >= self.MAX_CONSECUTIVE_FAILURES:
                    self._retired = True
                    logger.info("langsmith tracer retired after repeated failures; the local run "
                                "journal remains the record of truth")

        try:
            # Fire-and-forget: never awaited by request code, so it cannot add latency.
            self._pool.submit(_run)
        except RuntimeError:  # pragma: no cover - pool shut down at interpreter exit
            pass

    @contextmanager
    def span(self, name: str, **fields: Any):
        rec = _SpanRecorder()
        run_id = uuid4()
        self._submit(
            lambda: self._client.create_run(
                id=run_id,
                name=name,
                run_type="tool",
                inputs={"fields": _safe(fields)},
                project_name=self._project,
                start_time=datetime.now(UTC),
            )
        )
        try:
            yield rec
        finally:
            self._submit(
                lambda: self._client.update_run(
                    run_id,
                    outputs=_safe(rec.outputs),
                    error=rec.error,
                    end_time=datetime.now(UTC),
                )
            )

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for pending background submissions (shutdown, tests)."""
        self._pool.submit(lambda: None).result(timeout=timeout)


_TRACER: Any = None


def langsmith_available() -> bool:
    try:
        import langsmith  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def tracing_enabled() -> bool:
    settings = get_settings()
    return bool(settings.langsmith_api_key.strip()) and langsmith_available()


def get_tracer() -> Any:
    """Return the process tracer: a real one when configured, a null object otherwise."""
    global _TRACER
    if _TRACER is not None:
        return _TRACER
    if not tracing_enabled():
        _TRACER = _NullTracer()
        return _TRACER
    try:
        from langsmith import Client

        settings = get_settings()
        client = Client(
            api_key=settings.langsmith_api_key.strip(),
            api_url=(settings.langsmith_api_url or "https://api.smith.langchain.com").strip(),
        )
        project = settings.langsmith_project.strip() or "dobot"
        _TRACER = _LangsmithTracer(client, project)
        logger.info("langsmith tracing enabled (project=%s)", project)
    except Exception as exc:  # noqa: BLE001 - bad key, unreachable API, bad install
        logger.info("langsmith unavailable (%s); tracing disabled", exc)
        _TRACER = _NullTracer()
    return _TRACER


def reset_tracer() -> None:
    """Drop the cached tracer so settings changes (and tests) take effect."""
    global _TRACER
    _TRACER = None
