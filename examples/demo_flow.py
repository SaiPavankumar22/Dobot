"""Drive Dobot through the specification's demo scenarios against a running backend.

Usage (with the backend already running):

    python examples/demo_flow.py                # full scripted run
    python examples/demo_flow.py --shadow       # plan-only, execute nothing
    python examples/demo_flow.py --interactive  # pause between steps

The script uses only the HTTP API, which is exactly what the desktop app uses — so it is also the
fastest way to check that the whole stack is healthy.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx

BASE_URL = "http://127.0.0.1:8756"


def show(title: str, payload: Any) -> None:
    print(f"\n=== {title} ===")
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, indent=2, ensure_ascii=False)[:1800])
    else:
        print(payload)


def ask(client: httpx.Client, message: str, *, shadow: bool = False, background: bool = False) -> dict:
    response = client.post(
        "/chat",
        json={"message": message, "shadow": shadow, "background": background, "source": "demo"},
        timeout=300,
    )
    response.raise_for_status()
    body = response.json()
    print(f"\n>>> {message}")
    print(f"    status: {body['status']}")
    print(f"    answer: {body['answer'][:600]}")
    plan = body.get("plan") or {}
    for step in plan.get("steps", []):
        risk = step.get("risk") or "-"
        verdict = step.get("verdict") or "-"
        print(f"      · [{risk}/{verdict}] {step['action']['tool']}: {step['action']['description'][:70]}")
    for source in (body.get("sources") or [])[:5]:
        print(f"      source: {source.get('title')} — {source.get('url')}")
    print("    timeline:")
    for record in body.get("timeline", [])[:14]:
        print(f"      [{record['timestamp'][11:19]}] {record['event_type']}: {record['message'][:90]}")
    for approval in body.get("approvals", []):
        print(f"    APPROVAL REQUIRED ({approval['risk']}): {approval['description']}")
        for line in approval["preview"]:
            print(f"      {line}")
    return body


def pause(interactive: bool) -> None:
    if interactive:
        input("\n…press Enter to continue")


def main() -> int:
    parser = argparse.ArgumentParser(description="Dobot demo flow")
    parser.add_argument("--shadow", action="store_true", help="plan without executing anything")
    parser.add_argument("--interactive", action="store_true", help="pause between scenarios")
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=300) as client:
        try:
            health = client.get("/health").json()
        except httpx.ConnectError:
            print(f"Dobot's backend is not reachable at {args.base_url}. Start it first:", file=sys.stderr)
            print("  cd backend && uv run uvicorn app.main:app --port 8756", file=sys.stderr)
            return 1

        show("health", health)
        show("dashboard", client.get("/dashboard").json())

        ask(client, "Say hello and tell me what tools you have available.", shadow=args.shadow)
        pause(args.interactive)

        ask(client, "Explain in simple terms what a mixture-of-experts architecture is.", shadow=args.shadow)
        pause(args.interactive)

        ask(client, "Find recent research about mixture-of-experts routing.", shadow=args.shadow)
        pause(args.interactive)

        ask(client, "Remember that this project uses Zilliz for long-term memory.", shadow=args.shadow)
        pause(args.interactive)

        ask(client, "What database are we using for Dobot's long-term memory?", shadow=args.shadow)
        pause(args.interactive)

        body = ask(client, "Clean my Downloads folder.", shadow=args.shadow)
        if body["status"] == "WAITING_APPROVAL" and not args.shadow:
            approval = body["approvals"][0]
            print("\nApproving the plan…")
            resumed = client.post(f"/approvals/{approval['id']}", json={"decision": "approve"}).json()
            print(f"    → {resumed['status']}: {resumed['answer'][:400]}")
        pause(args.interactive)

        ask(
            client,
            "Every Friday at 6 PM, research new AI agent releases and send me a summary.",
            shadow=args.shadow,
        )
        pause(args.interactive)

        show("automations", client.get("/automations").json())
        show("memory (stats)", client.get("/memory/stats").json())
        show("activity", client.get("/activity?limit=15").json())

        if not args.shadow:
            print("\nApprovals still pending:")
            pending = client.get("/approvals").json()
            for approval in pending:
                print(f"  {approval['id']} {approval['risk']} {approval['description']}")

        time.sleep(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
