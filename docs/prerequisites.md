# Prerequisites — what Dobot needs, what it merely likes having

Everything Dobot needs lives in one file: `.env` at the repo root. Copy `.env.example` to `.env`
and fill in what you want. This page explains **every variable you are likely to touch**, what
happens when you leave it empty, and which keys are genuinely required.

The short version:

| You want… | Set these | Everything else |
| --- | --- | --- |
| A chat assistant that answers questions | `NEBIUS_API_KEY` | optional |
| …that can also research the web | + `TAVILY_API_KEY` | optional |
| Memory that survives restarts and is shared | + `MONGODB_URI`, `ZILLIZ_URI` | optional |
| Model-backed risk judgement (Laya) | + one Laya setting (below) | optional |
| Usage monitoring in LangSmith | + `LANGSMITH_API_KEY` | optional |

**With only `NEBIUS_API_KEY` set, Dobot is fully usable.** Every other subsystem degrades to a
local, offline-capable fallback by design — and the in-app **Doctor page** (or `GET /doctor`) always
tells you exactly what is off and the command that fixes it.

---

## 1. Required

### `NEBIUS_API_KEY` — reasoning

The one key Dobot cannot work without. It powers every model call: understanding what you meant,
planning steps, composing answers, looking at your screen.

- Get a key at <https://studio.nebius.com> (Token Factory).
- The endpoint defaults to `https://api.tokenfactory.nebius.com/v1`. Do **not** switch it back to
  the legacy `api.studio.nebius.com` host — it returns 404 for `/chat/completions`.
- Model defaults are already set to the Nemotron 3 models served by Nebius; you only touch
  `NEMOTRON_*_MODEL` if you want different models per tier (ultra / super / light / vision).

### `NEMOTRON_ALWAYS_THINK` — the latency switch (optional but useful to know)

Nemotron 3 is a reasoning model: by default it can emit a long "thinking" trace before answering,
which is often thousands of tokens of pure latency. Dobot disables that trace by default and
re-enables it only for deep planning. Direct questions come back in seconds.

- `false` (default): fast. Planning still thinks when it needs to.
- `1`: always think, everywhere — better plans on hard tasks, slower everything.

---

## 2. Recommended

### `TAVILY_API_KEY` — web research

Without it, requests like "find the latest NVIDIA news" answer from the model's memory (and say so).
With it, Dobot runs real searches and returns cited answers. Free tier is fine to start:
<https://tavily.com>.

---

## 3. Optional — memory that outlives the process

### `MONGODB_URI` (+ `MONGODB_DB`)

Durable structured memory: conversations, tasks, approvals, the cost ledger. Empty → a local
file-backed store under `.dobot/` that works fine on one machine.

### `ZILLIZ_URI` (+ `ZILLIZ_TOKEN`) and `EMBEDDING_MODEL`

Semantic memory recall ("what did I tell you about my thesis last month?"). Empty → a local vector
index. The embedding model (`Qwen/Qwen3-Embedding-8B` by default) is also served by Nebius and uses
the same `NEBIUS_API_KEY` — there is no separate key.

---

## 4. Optional — Laya (the model behind JEV risk judgement)

**Clearing up a common confusion:** there are two different things here, and only one of them can
cost money.

- **JEV** is Dobot's *own* judgement module — deterministic heuristics (blast radius, credential
  patterns, injection markers). It is part of the codebase. It is free, always on, and never talks
  to the network.
- **Laya** ([convaiinnovations/laya](https://huggingface.co/convaiinnovations/laya)) is an *open*
  model (open weights, free to run) that gives JEV model-calibrated probabilities instead of pure
  regex signals. Because it is open, running it costs compute, not licence fees.

Laya runs in three modes — set **exactly one**:

| Mode | Setup | Env |
| --- | --- | --- |
| **Server** (recommended) | `pip install "laya[serve]"` then `laya-serve` | `LAYA_SERVER_URL=http://127.0.0.1:8000` |
| **In-process** | `pip install laya` (checkpoint downloads on first use) | `LAYA_INPROCESS=1` |
| **Heuristics** (default) | nothing | nothing — JEV judges alone |

**About `LAYA_API_KEY`:** you were right to question it. Laya itself is an open model — no account,
no key, no fees. The key exists for exactly one situation: if you run `laya-serve` on a *shared*
machine or an exposed port, you can start the server itself with `LAYA_API_KEY=…` so only callers
bearing that bearer token may query it. It is an access lock **you** choose to put on **your** own
server. Running Laya locally on your laptop? Leave `LAYA_API_KEY` empty and forget it exists.

Laya can only *escalate* risk, never lower it, and deterministic policy always wins — so wiring it
in is strictly additive safety.

---

## 5. Optional — LangGraph agent harness

`LANGGRAPH_ENABLED` (default `true`)

Every tool step Dobot executes runs through a structured **validate → execute → finalise** loop.
When the `langgraph` package is installed the loop runs as a real state graph (inspectable,
replayable); when it is not installed, the identical stages run inline. The harness *wraps* the
existing guards (decision engine, approvals, interceptors) — it never replaces them, and outcomes
are the same in both modes.

- Install it: `cd backend && uv pip install langgraph` (already in the lockfile as of this release).
- Turn it off: `LANGGRAPH_ENABLED=false`.

---

## 6. Optional — LangSmith usage monitoring

`LANGSMITH_API_KEY`, `LANGSMITH_API_URL`, `LANGSMITH_PROJECT`

Wire Dobot to [LangSmith](https://smith.langchain.com) and every model call and every harness stage
becomes a searchable run with inputs, outputs, token counts, latency and errors — the answer to
"what did Dobot actually do all day and what did it cost?".

- **Completely off unless the key is set.** No key → no network calls, no data leaves your machine,
  and the local journal still records everything.
- Get a key at <https://smith.langchain.com> (free developer tier), create a project, and set
  `LANGSMITH_PROJECT` if you rename it from the default `dobot`.
- Tracing is failure-tolerant by design: if LangSmith is unreachable, your task runs anyway and the
  failure is a debug log line.

---

## 7. Optional — safety and access controls (worth reading once)

| Variable | Default | What it does when you set it |
| --- | --- | --- |
| `EXECUTION_MODE` | `agent` | `ask` never executes anything; `assist` confirms anything beyond a read; `agent` runs safe reversible work automatically |
| `REQUIRE_WRITE_APPROVAL` | `false` | `1` = confirm before anything that changes your files, even MEDIUM risk |
| `PROTECTED_PATHS` | ssh/aws/gpg/kube… | Locations no tool may ever read or write, even with approval. **Add your own** (comma-separated) |
| `SKILL_SCAN_MODE` | `block` | Skills are scanned for injection/secrets/exfiltration before the planner sees them |
| `DOBOT_API_TOKEN` | *(empty)* | Set it and every API call except `/health` needs `Authorization: Bearer <token>`. Generate one: `cd backend && uv run python -m app.selftest --new-token` |
| `SHADOW_MODE` | `false` | `1` = run everything in preview: plans are judged and previewed but nothing executes |

## 8. Optional — everything else

- `AGENT_RUNTIME=local` — the built-in guarded tools. `cli`/`remote` shell out to a Hermes runtime.
- `SANDBOX_PROVIDER=local` — the action firewall. `nemoclaw` routes execution through the NemoClaw CLI.
- `TOKENJUICE_*` — context compression before tool output reaches the model. On by default; savings
  are measured in `/system/usage`.
- `PRICE_PER_MTOK_INPUT/OUTPUT` — set them and the cost ledger reports real USD estimates instead of `UNKNOWN`.
- `MEMORY_*`, `CONSOLIDATION_*`, `CANONICAL_*` — memory decay/consolidation tuning. Defaults are sane.
- `VOICE_ENABLED` — text-to-speech via the OS engine. Off by default.
- `DOBOT_HOST`/`DOBOT_PORT` — loopback `127.0.0.1:8756` by default; keep it loopback on a laptop.
- `DOBOT_SKILLS_DIR` — empty = the repo's `skills/` folder.

---

## Verifying your setup

```bash
cd backend && uv run uvicorn app.main:app --port 8756
curl http://127.0.0.1:8756/health   # "degraded": [] → everything you configured answered
curl http://127.0.0.1:8756/doctor   # per-capability state + the exact fix for anything off
```

The Doctor page in the desktop app shows the same thing with one-click copyable fixes.
