---
title: Dobot
emoji: 🤖
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 8756
pinned: false
license: apache-2.0
---

# Dobot backend

The FastAPI brain of **Dobot** — an always-on personal AI operating layer (NVIDIA × Nebius
hackathon). This Space runs the backend as a Docker image, so the Windows desktop app needs no
local Python: install the app, point it at this Space, paste one token.

## Connecting the desktop app

The Space is **private**: Hugging Face's proxy rejects every request that arrives without
`Authorization: Bearer <HF_READ_TOKEN>`, and so does the backend itself.

1. Create a **read** token at <https://huggingface.co/settings/tokens>.
2. In the app: **Settings → Backend** → URL `https://sai-pavankumar22-dobot.hf.space` (already the
   default in release builds) → paste the token in the API-token field → **Save and reconnect**.

Every HTTP call carries that token. A browser WebSocket cannot attach headers, and a private
Space's proxy refuses the upgrade without one — so when the WebSocket cannot connect, the app
falls back to polling `GET /events` over HTTP (same events, every 2.5 s) and stays fully live.

## Secrets (Settings → Variables and secrets)

Secrets are environment variables; nothing secret lives in the image or the repo.

These are the ones the Space's **operator** sets. The four credentials an end user brings arrive in
the app (**Settings → Your API keys**) and are written to the container's `.env` at runtime.

| Secret | Needed? | What it does |
| --- | --- | --- |
| `DOBOT_API_TOKEN` | **recommended** | Backend-side gate. Set it to the **same value as your HF read token** — the app holds one token, so both gates share it: a second, independent check behind HF's proxy on every call, including the `/events` fallback poll. |
| `MONGODB_URI` | recommended | Durable memory (`MONGODB_DB` defaults to `dobot`) |
| `LANGSMITH_API_KEY` | optional | Usage tracing (`LANGSMITH_API_URL`, `LANGSMITH_PROJECT` defaults to `dobot`) |
| `DEEPAGENTS_ENABLED` | optional | `1` enables the deep-research subagent |
| `TRANSCRIBER_ENABLED` | optional | `false` when nothing can reach a microphone from inside the container |
| `NEBIUS_API_KEY`, `TAVILY_API_KEY`, `ZILLIZ_URI`, `ZILLIZ_TOKEN` | optional | Fallbacks for a single-person Space. Normally each user supplies their own in the app; the API refuses `PUT /settings/keys/{mongodb_uri,langsmith*,dobot_api_token,laya*}` with **403 `OPERATOR_MANAGED`**, so user keys and operator config cannot collide. |
| `EXECUTION_MODE` / `SHADOW_MODE` / `REQUIRE_WRITE_APPROVAL` | optional | Behaviour tuning — see `.env.example` in the repo |

Without `MONGODB_URI`, memory lives in the container's filesystem and is lost when the Space
rebuilds — set it if you care about memories surviving. The same applies to anything a user saves
through **Settings → Your API keys**: it persists in the container's `.env` until a rebuild, which is
the honest reason to give each person their own Space when their credentials must stay separate.

## Local development (same image)

```bash
docker build -t dobot-backend .
docker run --rm -p 8756:8756 --env-file .env dobot-backend
curl http://127.0.0.1:8756/health
```

## Notes

- Free CPU hardware; inference runs on Nebius, so no GPU is needed.
- A sleeping Space wakes on the first request (the first call can take ~30 s).
- Voice transcription and local OCR are not in this image — the app degrades those features
  honestly and explains why on the Doctor page.
- Chat attachments ride on the vision model in the same Nebius account as everything else: up to 4
  images (5 MB each) and 4 text files (256 KB each) per message, ~6 000 characters of file text and
  a 24 MB request in total. `GET /chat/attachments` publishes those numbers so the composer and the
  server can never disagree.
