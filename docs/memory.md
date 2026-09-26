# Dobot Memory

Dobot remembers across sessions: preferences, projects, people, facts, workflows, tasks, episodes and
past actions.

## Categories

| Type | Example |
| --- | --- |
| `preference` | "User prefers Python for backend development." |
| `project` | "User is building Dobot for an NVIDIA × Nebius hackathon." |
| `person` | "John is the reviewer for the Dobot report." |
| `fact` | "Dobot's long-term vector memory uses Zilliz." |
| `workflow` | "Weekly report: check GitHub → check issues → summarize → draft." |
| `episode` | "On Friday Dobot researched AI agent releases and summarized them." |
| `task` | Task titles and outcomes, so follow-ups have continuity. |

## Storage

```
Memory Service
     │
     ├── structured memory   MongoDB collection `memories`   (fallback: local JSON store)
     ├── semantic memory     Zilliz / Milvus collection       (fallback: in-process cosine index)
     └── task & automation   MongoDB collections `tasks`, `automations`, `activity_logs`
```

`app/memory/store.py` and `app/memory/vectors.py` define the two interfaces; `manager.py` writes to
both, `retriever.py` reads from both. Nothing else in the codebase talks to a database directly, so
swapping in Postgres + pgvector means adding one store implementation.

## Retrieval

Memories are never dumped wholesale into a prompt. `Retriever.search()` fuses four signals:

```
score = w_sim · cosine(query, memory)        # semantic similarity
      + w_recency · decay(age)               # newer memories rank higher
      + w_importance · importance            # explicit user importance
      + w_task · tag/task overlap            # relevance to the current task
```

Defaults: `w_sim=0.6`, `w_recency=0.15`, `w_importance=0.15`, `w_task=0.1`, with a half-life of 30
days. The top-K (default 6) plus everything tagged with the active project are injected into the
context bundle, and the *sources* are shown in the UI so the user can audit what Dobot used.

## Embeddings

With `NEBIUS_API_KEY` set, embeddings come from the configured Nebius embedding model. Without it,
Dobot uses a deterministic hashed bag-of-words embedding (dimension 512) — good enough for retrieval
tests, deterministic across runs, and clearly labelled as `local-hash` in `/health`.

## Writing memories

Two paths:

1. **Explicit** — the user says "remember this". The planner emits a `memory_write` action, which is
   `LOW` risk and executes immediately.
2. **Implicit** — after a task completes, the orchestrator stores an `episode` memory (what was asked,
   what happened, whether it verified) at low importance. Implicit writes are visible on the Memory
   page and deletable.

## Forgetting

`DELETE /memory/{id}` removes the record and its vector. `DELETE /memory?type=episode&older_than_days=30`
prunes episodes in bulk. The Memory page exposes both, because a memory the user cannot audit is a
liability rather than a feature.
