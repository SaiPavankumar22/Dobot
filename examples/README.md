# Examples

## `demo_flow.py`

Runs the specification's demo scenarios through the real HTTP API (the same one the desktop app
uses), printing the plan, risk verdicts, approvals, sources and the activity timeline at each step.

```bash
# backend running first
cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8756

# then, from the repository root
python examples/demo_flow.py                  # full run
python examples/demo_flow.py --shadow         # plan everything, execute nothing
python examples/demo_flow.py --interactive    # pause between scenarios
```

## Scenario walkthrough

| Step | What it demonstrates |
| --- | --- |
| "Say hello and tell me what tools you have available" | Backend, reasoning model and tool surface |
| "Explain … mixture-of-experts" | Reasoning without a tool call |
| "Find recent research about mixture-of-experts routing" | Tavily search → sources → synthesis |
| "Remember that this project uses Zilliz" | Durable memory written with verification |
| "What database are we using …?" | Memory retrieval (memory injected into context) |
| "Clean my Downloads folder" | Skill reuse → plan → approval gate → verification |
| "Every Friday at 6 PM, research new AI agent releases" | Cron automation created and scheduled |

## Things worth trying by hand

- **Shadow mode**: `POST /chat {"message": "...", "shadow": true}` — returns the plan with real file
  counts and risk verdicts, and executes nothing.
- **Action firewall**: ask it to `delete everything in ~/Downloads`. The plan pauses at an approval
  with a categorised preview (screenshots / documents / installers / unknown).
- **Kill switch**: start a long task in the background, then `POST /security/kill`.
- **Prompt-injection defence**: post a message whose screen context contains instruction-like text
  and let a plan propose an outbound action; JEV flags it and forces approval.
- **Memory audit**: `GET /memory/recall?q=…` shows exactly which memories would be injected, and why
  (similarity, recency, importance, task overlap).
