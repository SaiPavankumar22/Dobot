"""End-to-end self test: ``python -m app.selftest``.

Runs the real loop — context, plan, decision, execution, verification, memory — against whatever
providers are configured, and prints the event trace. Useful on a judge's laptop and in CI.
"""

from __future__ import annotations

import asyncio
import sys

from app.config import get_settings
from app.logging_setup import configure_logging
from app.schemas import ChatRequest
from app.services import build_services


def _configure_stdout() -> None:
    """The Windows console default (cp1252) cannot print ✓/→; force UTF-8 with replacement."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - non-reconfigurable stream
                pass

async def run_scenario(message: str, *, shadow: bool = False) -> None:
    _configure_stdout()
    settings = get_settings()
    configure_logging()
    services = await build_services(settings)
    await services.startup()
    try:
        print(f"\n=== providers ===\n{await services.provider_status()}")
        print(f"\n=== request ===\n{message}")
        request = ChatRequest(message=message, shadow=shadow, source="selftest")
        response = await services.orchestrator.submit(request)
        print(f"\n=== status === {response.status.value}")
        print(f"\n=== answer ===\n{response.answer}")
        if response.plan and response.plan.steps:
            print("\n=== plan ===")
            for step in response.plan.steps:
                print(
                    f"  {step.index}. {step.action.tool} "
                    f"[{step.risk.value if step.risk else '-'} / {step.verdict.value if step.verdict else '-'}] "
                    f"{step.action.description[:70]} → {step.status.value}"
                )
        if response.sources:
            print(f"\n=== sources === {len(response.sources)}")
            for index, source in enumerate(response.sources[:5], start=1):
                print(f"  [{index}] {source.get('title', '')} — {source.get('url', '')}")
        print("\n=== timeline ===")
        for record in response.timeline:
            print(f"  [{record.timestamp.strftime('%H:%M:%S')}] {record.event_type}: {record.message[:110]}")
        if response.approvals:
            print(f"\n=== approvals pending === {[item.id for item in response.approvals]}")
    finally:
        await services.shutdown()


def new_token() -> int:
    """Print a fresh API token. Print only - it never writes your .env.

    A tool that silently edits the file holding your credentials is a tool you have to trust more than
    it deserves; pasting one line is a fair price for that.
    """
    import secrets

    _configure_stdout()
    token = secrets.token_urlsafe(32)
    print("Add this line to your .env, then restart the backend:\n")
    print(f"DOBOT_API_TOKEN={token}\n")
    print("The desktop app asks for it once under Settings, and the Doctor page reports whether")
    print("the gate is on.")
    return 0


async def main() -> int:
    if "--new-token" in sys.argv:
        return new_token()
    await run_scenario("Say hello and tell me what tools you have available.")
    await run_scenario("Clean my Downloads folder.", shadow=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(asyncio.run(main()))
