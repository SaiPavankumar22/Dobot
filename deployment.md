# Deployment

Dobot ships to two places, from one repo:

| Target | What it is | How it updates |
| --- | --- | --- |
| **GitHub** (`<your-user>/Dobot`) | The source of truth: backend, desktop app, skills, docs. | `git push origin main` |
| **Hugging Face Space** (`$HF_SPACE` → `https://YOUR_SPACE.hf.space`) | The hosted backend the desktop app can talk to. A Docker image built by HF from `backend/` + `skills/` + `deploy/huggingface/`. | `bash scripts/deploy-hf-space.sh` (a git push that triggers an image build) |

The Space is **private**: Hugging Face's proxy rejects every request that arrives without your
token, which is half of why a hosted Dobot is not an open endpoint. Never delete and recreate a
Space casually — HF no longer creates free Docker Spaces, and a replacement would land on paid
hardware.

The Space's name is deliberately **not** written in this repo. Pass it once as `HF_SPACE=owner/name`,
or keep it in a git-ignored `.hf-space` file at the repo root and the command stays short.

---

## Every deployment, in order

Run from the repo root. The HF token needs **write** scope; keep it in the environment, never in
files or commits.

```bash
# 1. Commit and push the source of truth.
git add <changed files>
git commit -m "<what and why>"
git push origin main

# 2. Push the backend to the Hugging Face Space (triggers the Docker build).
HF_TOKEN=hf_xxx HF_SPACE=YOUR_SPACE bash scripts/deploy-hf-space.sh
```

If `git status` shows nothing to commit but backend code changed earlier, step 2 alone is enough.
If only `desktop/` or `scripts/` changed, step 2 is unnecessary — the Space image contains only
`backend/`, `skills/`, `deploy/huggingface/`, the Space README card, and the docs the script
substitutes (`install.md`, `deployment.md`).

## What the deploy script does

`scripts/deploy-hf-space.sh` is the only supported path to the Space. It:

1. Clones the Space repo into a temp dir (deleted on exit; token only ever lives in the clone URL).
2. **Replaces, never overlays**, `backend/app` and `skills` from the checkout (`rm -rf` then
   `cp -r src/. dest/`). Overlaying is how the Space once silently served old code from
   `backend/app` while the new code sat in a nested `backend/app/app`.
3. Guards the result: refuses to push unless `backend/app/main.py` exists and no nested
   `backend/app/app` directory is present.
4. Commits and pushes — HF then builds the image (`app_port 8756`, `python -m app.main`).

Secrets are **never** pushed. They live in Space → Settings → Variables and secrets:

| Secret | Purpose |
| --- | --- |
| `MONGODB_URI` | Durable memory that survives Space rebuilds |
| `LANGSMITH_API_KEY` | Usage tracing |
| `DOBOT_API_TOKEN` | Optional second gate behind HF's proxy (set it to the same value as your HF read token) |

The four end-user keys (Nebius, Tavily, Zilliz token + URL) are **not** Space secrets and **not**
repo config — each user pastes them in the app under **Settings → Your API keys**, and the backend
writes them to the container's `.env` at runtime.

## Verify after deploying

```bash
# The Space tree is flat (no backend/app/app) — needs a read token for a private Space.
python - <<'EOF'
import json, os, urllib.request
req = urllib.request.Request(
    "https://huggingface.co/api/spaces/YOUR_SPACE/tree/main?recursive=true",
    headers={"Authorization": "Bearer " + os.environ["HF_TOKEN"]})
paths = [f["path"] for f in json.load(urllib.request.urlopen(req))]
print("nested:", sum(1 for p in paths if "/app/app/" in p))
print("main.py:", "backend/app/main.py" in paths)
EOF
```

Then check the Space went green (`https://huggingface.co/spaces/YOUR_SPACE`, stage
`RUNNING`) and that `GET /health` answers `200` with the read token. Without a token the private
proxy answers **404** — that is expected, not an outage.

## Rollback

`git revert` (or check out the last good commit into the working tree) and run the deploy script
again — the Space always builds exactly what the script pushes, so a redeploy of old code is a
real rollback, not a cache trick.

## Rules for coding agents (Cursor and friends)

When a change touches `backend/` or `skills/`, deployment means **both pushes, always** — finishing
at GitHub leaves the live backend stale. Never edit the Space repo on huggingface.co directly; it
is overwritten on the next deploy. Never commit tokens, keys, or `.env`. If `scripts/deploy-hf-space.sh`
is modified, keep its guards intact (the `if [ ! -f ]` blocks, not `A && fail` — the script runs
under `set -e`, which would kill the short-circuit form before the message prints) and re-verify a
full deploy end to end. HF git auth only works with credentials **in the URL**; the
`http.extraheader=AUTHORIZATION: bearer` form clones but is rejected on push.
