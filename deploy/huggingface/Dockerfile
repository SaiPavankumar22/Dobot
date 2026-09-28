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

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

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
ENV DOBOT_HOME=/app \
    DOBOT_ENV=production \
    DOBOT_HOST=0.0.0.0 \
    DOBOT_PORT=8756

EXPOSE 8756
WORKDIR /app/backend
CMD ["uv", "run", "--no-sync", "python", "-m", "app.main"]
