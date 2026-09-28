#!/usr/bin/env bash
# Deploy the Dobot backend to a Hugging Face Docker Space.
#
#   HF_TOKEN=hf_xxx bash scripts/deploy-hf-space.sh        # or: bash scripts/deploy-hf-space.sh hf_xxx
#
# One command from the repo root: creates the Space if it does not exist (private, Docker SDK),
# assembles the Space repo in a temp dir (Dockerfile + Space README + backend/ + skills/ + the
# docs) and pushes it — a push is what triggers the image build. Secrets are NEVER pushed: set them
# in the Space's Settings -> Variables and secrets afterwards.
set -euo pipefail

fail() { echo "error: $*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOKEN="${HF_TOKEN:-${1:-}}"
# The Space's name is personal to whoever runs the deploy, so the repo never carries it: pass it as
# HF_SPACE=owner/name, or keep it once in a git-ignored `.hf-space` file at the repo root.
SPACE="${HF_SPACE:-}"
if [ -z "$SPACE" ] && [ -f "$REPO_ROOT/.hf-space" ]; then
  SPACE="$(tr -d '[:space:]' < "$REPO_ROOT/.hf-space")"
fi
API="https://huggingface.co/api"
REMOTE="https://huggingface.co/spaces/${SPACE}"
# owner/name -> owner-name.hf.space (lowercase, separators become dashes)
DIRECT="https://$(printf '%s' "$SPACE" | tr '[:upper:]' '[:lower:]' | tr '/_' '--').hf.space"
# Git over HTTPS needs the credentials *in the URL*: Hugging Face's git endpoint accepts
# `http.extraheader=AUTHORIZATION: bearer …` for a clone but rejects the push, which is the kind of
# thing you only discover after waiting for an upload. The temp directory this lands in is deleted on
# exit, so the token never reaches a file we keep.
REMOTE_AUTH="https://${SPACE%%/*}:${TOKEN}@huggingface.co/spaces/${SPACE}"

[ -n "$TOKEN" ] || fail "pass an HF token (write scope): https://huggingface.co/settings/tokens"
[ -n "$SPACE" ] || fail "pass the Space as HF_SPACE=owner/name (or keep it in a git-ignored .hf-space file at the repo root)"
command -v git >/dev/null 2>&1 || fail "git is required"
command -v curl >/dev/null 2>&1 || fail "curl is required"

space_exists() {
  curl -fsS "$API/spaces/$SPACE" -H "Authorization: Bearer $TOKEN" >/dev/null 2>&1
}

# 1. The Space must exist — private, Docker SDK. Best effort via the API; if that is refused, the
#    script tells the user exactly what to click and stops.
if ! space_exists; then
  echo "Space $SPACE not found — creating it (private, Docker SDK)…"
  curl -sS -X POST "$API/spaces/$SPACE" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"sdk":"docker","private":true}' >/dev/null || true
  space_exists ||
    fail "could not create the Space automatically. Create it at https://huggingface.co/new-space (owner: ${SPACE%%/*}, name: ${SPACE##*/}, SDK: Docker, Private) and re-run."
fi

# 2. Clone it (possibly empty) into a temp dir.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
echo "cloning spaces/$SPACE…"
git clone --depth 1 "$REMOTE_AUTH" "$WORK/space" ||
  fail "clone failed — check the token's write scope."

SPACE_DIR="$WORK/space"

# 3. Assemble the Space repo: Dockerfile + Space card + backend sources + skills + docs.
#    The card and the docs are written against a placeholder (`https://YOUR_SPACE.hf.space`), so
#    the checked-in copies never name a personal deployment while the Space's copies do.
substitute() {
  sed -e "s|https://YOUR_SPACE.hf.space|$DIRECT|g" -e "s|YOUR_SPACE|$SPACE|g" "$1" > "$2"
}
cp "$REPO_ROOT/deploy/huggingface/Dockerfile" "$SPACE_DIR/Dockerfile"
cp "$REPO_ROOT/deploy/huggingface/.dockerignore" "$SPACE_DIR/.dockerignore"
substitute "$REPO_ROOT/deploy/huggingface/README.md" "$SPACE_DIR/README.md"
substitute "$REPO_ROOT/docs/install.md" "$SPACE_DIR/install.md"
substitute "$REPO_ROOT/deployment.md" "$SPACE_DIR/deployment.md"
# Replace the source trees wholesale, never overlay them. `cp -r src dest` copies INTO dest when dest
# already exists, so a second deploy would leave the previous code at backend/app and put the new code
# at backend/app/app — where the Dockerfile never looks. The Space would quietly keep serving the old
# build forever, which is exactly what happened once. Wiping first also drops files deleted locally.
rm -rf "$SPACE_DIR/backend/app" "$SPACE_DIR/skills"
mkdir -p "$SPACE_DIR/backend/app" "$SPACE_DIR/skills"
cp "$REPO_ROOT/backend/pyproject.toml" "$REPO_ROOT/backend/uv.lock" "$SPACE_DIR/backend/"
cp -r "$REPO_ROOT/backend/app/." "$SPACE_DIR/backend/app/"
cp -r "$REPO_ROOT/skills/." "$SPACE_DIR/skills/"
# Drop the junk a working copy carries around.
find "$SPACE_DIR" \( -name '__pycache__' -o -name '.pytest_cache' -o -name '.ruff_cache' \) \
  -type d -prune -exec rm -rf {} +
find "$SPACE_DIR" -name '*.pyc' -delete

# Guard: the Dockerfile runs `python -m app.main` from /app/backend, so anything other than
# backend/app/main.py means the image would build against code nobody wrote. `if` rather than
# `A && B` on purpose — with `set -e` a bare short-circuit that evaluates false ends the script.
if [ ! -f "$SPACE_DIR/backend/app/main.py" ]; then
  fail "backend/app/main.py is missing from the assembled Space"
fi
if [ -d "$SPACE_DIR/backend/app/app" ]; then
  fail "assembled tree nested to backend/app/app — refusing to deploy"
fi

# 4. Push — this triggers the Docker image build on HF.
cd "$SPACE_DIR"
git add -A
if git diff --cached --quiet; then
  echo "Space already up to date — nothing to deploy."
  exit 0
fi
git -c user.name="Dobot deploy" -c user.email="deploy@localhost" \
  commit -m "Deploy Dobot backend"
git push "$REMOTE_AUTH" HEAD:main

# The Space's public URL (computed near the top, after the name is resolved).
cat <<EOF

✓ Pushed. The Space builds the image now — watch it at:
    https://huggingface.co/spaces/$SPACE/activity

Then, once the build is green:
  1. Space → Settings → Variables and secrets — the operator side:
     DOBOT_API_TOKEN (same value as your HF read token), MONGODB_URI, LANGSMITH_API_KEY.
     User keys (nebius, tavily, zilliz) are NOT set here: each person pastes their own in the app.
  2. Create a READ token: https://huggingface.co/settings/tokens
  3. In the Dobot app: Settings → Backend → $DIRECT
     and paste that token in the API-token field.
  4. In the app: Settings → Your API keys → paste your own nebius key (required).
EOF
