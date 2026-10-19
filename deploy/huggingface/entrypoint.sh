#!/usr/bin/env bash
# Two servers, one container: the Hermes gateway (agent runtime) beside the Dobot backend.
#
# Contract — the backend NEVER waits on Hermes:
#   * the gateway starts in the background and the backend execs immediately;
#   * HERMES_SIDECAR=0 skips the gateway entirely;
#   * if the gateway crashes or is still booting, Dobot keeps serving: AGENT_RUNTIME=auto
#     probes it on a TTL and degrades to Dobot's own guarded tools (ResilientRuntime), which
#     is what docs/hermes.md documents.
#
# The gateway binds 127.0.0.1 inside the container — Hugging Face only routes the declared
# app port, so /v1/runs is reachable by this container alone, never from the internet.
set -u

log() { printf '[entrypoint] %s\n' "$*"; }

if [ "${HERMES_SIDECAR:-1}" = "1" ] && command -v hermes >/dev/null 2>&1; then
  export HERMES_HOME="${HERMES_HOME:-/app/.hermes}"
  mkdir -p "$HERMES_HOME"

  # Both processes must present the same bearer key. The gateway loads $HERMES_HOME/.env
  # with override=True, so when that file already exists ITS key wins and the backend client
  # adopts it; a fresh container generates one key and writes it once.
  if [ -f "$HERMES_HOME/.env" ]; then
    existing_key="$(grep -E '^API_SERVER_KEY=' "$HERMES_HOME/.env" 2>/dev/null | tail -1 | cut -d= -f2-)"
    if [ -n "${existing_key:-}" ]; then
      API_SERVER_KEY="$existing_key"
    fi
  fi
  if [ -z "${API_SERVER_KEY:-}" ]; then
    API_SERVER_KEY="dobot-$(od -An -N16 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')"
    [ -n "$API_SERVER_KEY" ] || API_SERVER_KEY="dobot-$$-$(date +%s)"
    {
      echo "API_SERVER_ENABLED=true"
      echo "API_SERVER_KEY=$API_SERVER_KEY"
      echo "API_SERVER_PORT=8642"
      echo "API_SERVER_HOST=127.0.0.1"
      if [ -n "${NEBIUS_API_KEY:-}" ]; then
        echo "NEBIUS_API_KEY=$NEBIUS_API_KEY"
      fi
    } > "$HERMES_HOME/.env"
  fi
  # The gateway authenticates with API_SERVER_KEY; the backend client reads HERMES_API_KEY.
  # A Space secret set to the same value passes through untouched.
  export API_SERVER_KEY
  export HERMES_API_KEY="${HERMES_API_KEY:-$API_SERVER_KEY}"
  export API_SERVER_ENABLED=true
  export API_SERVER_HOST="${API_SERVER_HOST:-127.0.0.1}"
  export API_SERVER_PORT="${API_SERVER_PORT:-8642}"

  # First boot only: the gateway's own model config. Hermes runs its own model loop, so it
  # needs the provider key in ITS environment — the app's pasted keys never reach it.
  if [ ! -f "$HERMES_HOME/config.yaml" ] && [ -n "${NEBIUS_API_KEY:-}" ]; then
    cat > "$HERMES_HOME/config.yaml" <<'YAML'
model:
  default: nvidia/nemotron-3-super-120b-a12b
  provider: custom
  base_url: https://api.tokenfactory.nebius.com/v1
  key_env: NEBIUS_API_KEY
YAML
  fi
  # A pre-existing .env may predate the Nebius key now in the container environment.
  if [ -n "${NEBIUS_API_KEY:-}" ] && [ -f "$HERMES_HOME/.env" ] && \
     ! grep -q '^NEBIUS_API_KEY=..*' "$HERMES_HOME/.env"; then
    echo "NEBIUS_API_KEY=$NEBIUS_API_KEY" >> "$HERMES_HOME/.env"
  fi

  log "starting hermes gateway sidecar on ${API_SERVER_HOST}:${API_SERVER_PORT}"
  hermes gateway run &
else
  log "hermes sidecar off (HERMES_SIDECAR=${HERMES_SIDECAR:-1}, hermes on PATH: $(command -v hermes || echo no)) — Dobot runs on its own tools"
fi

# Immediate either way; the backend re-probes the gateway on a TTL and reports
# available / unreachable honestly at GET /settings/providers.
exec "$@"
