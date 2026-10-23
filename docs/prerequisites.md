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
| Model-backed risk judgement (AutoTrust JEV-27B-VL) | + `NOVITA_API_KEY` (below) | optional |
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

## 4. Optional — AutoTrust JEV-27B-VL (the model behind JEV)

**Clearing up a common confusion:** there are two different things here, and only one of them can
cost money.

- **JEV** is Dobot's judgement layer — the code that decides *which questions to ask* about a
  proposed action and how their answers may change its risk. It is part of the codebase, and its
  hard rules (a policy BLOCK outranks everything; JEV can only escalate, never de-escalate) are
  deterministic and enforced in code.
- **AutoTrust JEV-27B-VL** is the hosted model that answers those questions on
  [Novita](https://novita.ai) — a decision-evaluation model, not a chat model: each request submits
  one question and gets back a *probability distribution over options*, so there is no free text to
  parse and nothing to hallucinate. JEV asks it three things: how risky is this action (it thinks
  only when its first answer is unsure), is this untrusted on-screen/web content a prompt-injection
  attempt, and — it is a vision model — is a credential visible in the captured screenshot.

Get a key at [novita.ai/settings/key-management](https://novita.ai/settings/key-management), then
paste it under **Settings → Your API keys → Novita** (applies immediately) or set
`NOVITA_API_KEY` in `.env`. Only input tokens are billed, and a judgement call is a handful of
tokens answering in ~0.2 s.

**Without a key** Dobot still runs: the deterministic policies and risk floors decide on their own,
and the Doctor reports the missing judgement model rather than pretending it is there. The model
can only *escalate* risk, never lower it, and deterministic policy always wins — so wiring it in is
strictly additive safety.

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
| `MULTI_USER` | `false` | `1` = accounts instead of one shared store: per-user memory, tasks, automations and API keys, invite-gated registration (first account is the owner), per-user event streams. Anonymous requests are refused. See [Security](security.md#accounts-and-multi-user-multi_usertrue). |
| `MULTI_USER_OPEN_REGISTRATION` | `false` | Only meaningful with `MULTI_USER=1`: lets anyone register without an owner invite. |
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
