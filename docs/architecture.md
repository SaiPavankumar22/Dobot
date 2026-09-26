# Dobot Architecture

## Request lifecycle

```
Desktop (Tauri)                 Backend (FastAPI)                     External
---------------                 -----------------                     --------
chat composer / dot   ──────►   POST /chat  ──► Orchestrator
region select         ──────►   POST /screen/analyze  ──► Context Engine
                                                          │
                                                          ├─ screen crop + OCR/vision
                                                          ├─ memory retrieval
                                                          ├─ open tasks / automations
                                                          └─ environment (apps, files, sandbox)
                                                                   │
                                                                   v
                                                          Model Router → Nemotron (Nebius)
                                                                   │
                                                            Plan (steps = Actions)
                                                                   │
                                                                   v
                                                          Decision Engine
                                                          deterministic policy + risk + JEV
                                                                   │
                                              ALLOW ───────────────┼─────────────── BLOCK
                                                 │                 │                    │
                                                 v          APPROVAL                    v
                                        Hermes runtime │   (user decides)      structured refusal
                                                 │      │
                                                 v      v
                                        NemoClaw / OpenShell boundary
                                                 │
                                                 v
                                        Tool executes (browser / fs / terminal / computer)
                                                 │
                                                 v
                                          Verifier ──► Memory ──► Activity log
                                                 │
   WebSocket events ◄────────────────────────────┘
```

The desktop side is one React bundle rendering five Tauri windows — `chat` (the default surface),
`dot` (the opt-in always-on companion), `panel`, `dashboard`, and a temporary `overlay` for region
selection. See [`development.md`](development.md) for how the windows and the always-on toggle are
wired.

Every stage emits an event onto the WebSocket bus, which is what feeds the dot state, the chat
thread, the activity timeline, and progress bars in the dashboard.

## Backend modules

| Path | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI app, lifespan wiring, health, providers status |
| `app/config.py` | Typed settings from environment / `.env` |
| `app/events.py` | Event model + WebSocket hub (broadcast, replay buffer) |
| `app/schemas.py` | Shared domain models: Action, Plan, Task, Memory, Approval, ScreenContext |
| `app/core/orchestrator.py` | The loop: context → plan → decide → act → verify → remember |
| `app/core/harness.py` | Per-step agent loop (validate → execute → finalise) via optional LangGraph |
| `app/observability/langsmith.py` | Optional hosted tracing of model calls and harness stages (null when unset) |
| `app/core/context_engine.py` | Builds the context bundle handed to the model |
| `app/core/router.py` | Model tier selection (light / super / ultra) |
| `app/core/planner.py` | Turns a model response into a validated Plan |
| `app/core/decision_engine.py` | Policy + risk + JEV → ALLOW / APPROVAL / BLOCK |
| `app/core/verifier.py` | Post-execution verification per action type |
| `app/core/scheduler.py` | Reminders and cron automations (APScheduler) |
| `app/core/killswitch.py` | Global cancellation of active executions |
| `app/agents/nemotron.py` | Nebius client (OpenAI-compatible) + offline reasoning fallback |
| `app/agents/hermes.py` | Execution runtime adapter (local tools / CLI / remote) |
| `app/agents/sandbox.py` | NemoClaw / OpenShell boundary adapter |
| `app/agents/research_agent.py` | Tavily research pipeline with parallel sub-queries |
| `app/tools/*` | Tool implementations with declared risk levels |
| `app/memory/*` | Structured store, vector store, embeddings, retriever |
| `app/security/*` | Risk classifier, policies, JEV (optionally Laya-backed), approvals |
| `app/api/*` | HTTP + WebSocket surface |

## Degradation model

Each external dependency has a local implementation behind the same interface:

| Dependency | Live | Fallback |
| --- | --- | --- |
| Nebius / Nemotron | OpenAI-compatible chat completions | Deterministic rule-based planner/responder |
| Tavily | REST search + extract | Offline "no sources" research result with an explicit notice |
| MongoDB | `motor` async driver | JSON file store under `DOBOT_STATE_DIR` |
| Zilliz | `pymilvus` client | In-process cosine index |
| Hermes | CLI / remote endpoint | Dobot's guarded local tool implementations |
| NemoClaw | NemoClaw CLI / OpenShell gateway | Dobot action firewall + path/network allowlists |
| OCR | `pytesseract` | Vision model transcription, then empty-text fallback |

`GET /health` reports which of these are actually live, and the UI surfaces it, so a demo never
silently depends on something that is down.

## Screen context pipeline

Screen data is only ever produced on demand:

```
user selects region (native overlay)
      → capture_region(x, y, w, h) in Rust (xcap) → PNG
      → POST /screen/analyze { image, question }
      → OCR (tesseract) ⟂ vision transcription (Nemotron vision, if configured)
      → ScreenContext { application, window_title, region, ocr_text, image_available }
      → orchestrator
```

No frame is persisted unless the user saves it to memory.

## Event contract

```json
{
  "type": "tool_started",
  "task_id": "task_8f13",
  "timestamp": "2026-09-25T23:10:10.221Z",
  "message": "Searching the web...",
  "data": { "tool": "tavily" }
}
```

Event types: `task_received`, `context_built`, `planning`, `plan_created`, `decision`, `approval_required`,
`execution_started`, `tool_started`, `tool_completed`, `verification`, `memory_written`, `completed`,
`failed`, `killed`, `dot_status`.

## Data model

```
tasks(id, title, description, status, priority, source, created_at, updated_at, deadline, steps[], result, error)
task_steps(id, task_id, sequence, description, status, tool, action, result, verification)
memories(id, user_id, type, content, importance, tags[], embedding_id, created_at, updated_at)
automations(id, user_id, name, schedule, prompt, status, created_at, last_run_at, next_run_at)
approvals(id, task_id, action, risk, description, payload, status, created_at, resolved_at)
activity_logs(id, task_id, event_type, message, metadata, timestamp)
```

Structured records live in MongoDB when `MONGODB_URI` is set, otherwise in the local store. Embeddings
live in Zilliz when configured, otherwise in the local index. Both paths share one interface, so the
retriever is storage-agnostic.

> Note: the specification's later sections mention PostgreSQL + pgvector. The definitive stack for
> this build is MongoDB + Zilliz (stated in the product header); the store interfaces are narrow
> enough that a Postgres implementation is a drop-in third backend.
