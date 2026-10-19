# syntax=docker/dockerfile:1
# Dobot backend — Hugging Face Docker Space image.
#
# The build context is the Space repo this file lands in (assembled by scripts/deploy-hf-space.sh):
# Dockerfile, README.md, backend/, skills/. Secrets are never baked into the image — the Space
# injects them as environment variables (Settings -> Variables and secrets), which the backend's
# pydantic Settings already reads.
FROM python:3.11-slim-bookworm

# uv installs exactly what backend/uv.lock resolves — no lock drift between laptop and Space.
RUN pip install --no-cache-dir uv

# Server 1 of 2: the Hermes agent gateway (NousResearch/hermes-agent), started as a sidecar by
# entrypoint.sh next to the backend. Pinned — Dobot's adapter speaks the /v1/runs + SSE contract
# verified against 0.19.0, and aiohttp (imported by the API server, but only shipped behind
# hermes-agent's messaging extras) is installed explicitly so /v1/runs is not a lazy ImportError.
ARG HERMES_VERSION=0.19.0
RUN pip install --no-cache-dir "hermes-agent==${HERMES_VERSION}" "aiohttp==3.14.1"

WORKDIR /app

# Dependencies first: code-only changes then reuse this layer.
COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN cd backend && uv sync --frozen --no-install-project \
      --extra mongo --extra vectors --extra deepresearch

# The app (run from source — cwd is on sys.path) and the shipped skills the Skills page manages.
COPY backend/app ./backend/app
COPY skills ./skills

# Runtime config. DOBOT_HOME anchors skills/ and .dobot/ at /app; secrets arrive as env vars.
# The Space is reached through HF's proxy on every interface, on the port the README card declares.
# AGENT_RUNTIME=auto (not `local`) is what makes the two servers coexist: the adapter probes the
# sidecar and serves anyway when it is down — Dobot's own tools cover files, terminal, browser
# and web research, and only desktop gestures / hermes_run report the gateway as unavailable.
ENV DOBOT_HOME=/app \
    DOBOT_ENV=production \
    DOBOT_HOST=0.0.0.0 \
    DOBOT_PORT=8756 \
    AGENT_RUNTIME=auto \
    HERMES_ENDPOINT=http://127.0.0.1:8642 \
    HERMES_SIDECAR=1

EXPOSE 8756
# The build context root holds entrypoint.sh next to this file (deploy-hf-space.sh copies it there).
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
WORKDIR /app/backend
CMD ["uv", "run", "--no-sync", "python", "-m", "app.main"]
