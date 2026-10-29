---
name: github
description: GitHub via gh CLI: PRs, issues, reviews, repos, auth.
trigger: GitHub via gh CLI: PRs, issues, reviews, repos, auth.
required_tools: [terminal_run, fs_read]
safety: Imported from NousResearch/hermes-agent (Ben Barclay (benbarclay), Hermes Agent, MIT). Instructions only — Dobot's tools, approvals, protected paths and skill scanner still apply.
---

> **Imported from [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT).**
> Dobot adaptation: this document was written for Hermes, and only the names are different —
> `terminal` → `terminal_run` · `file` → `fs_read` / `fs_write` / `fs_list` ·
> `browser` / `browser_exec` → `browser_open` / `browser_navigate` / `browser_snapshot` ·
> `web` → `tavily_search` · `memory` → `remember` · `skills` → `skill_view` / `skill_run` ·
> `delegation` → `hermes_run` · `computer_use` → `screen_capture`.
> Hermes slash commands (`/skill-name`) do not exist here: call `skill_view` to read a skill's full
> instructions and `skill_run` to execute one. Scheduled repetition uses `automation_create`, which
> accepts `0 9 * * *`, `every monday 9am`, `weekdays at 9am` or `every 30m`. Bundled helper scripts
> sit in this skill's `scripts/` folder and run only through `terminal_run`, under Dobot's usual
> approvals, protected-path rules and skill scanner.
# GitHub

Work GitHub end to end with the `gh` CLI (REST fallback where noted): auth,
issues, the PR lifecycle, issue-to-PR delivery, code review, and repo
management. This skill consolidates six former skills; each workflow lives
complete in its reference file — ALWAYS read the matching reference before
starting that workflow, the body below only routes.

## Routing

| Task | Read first |
|---|---|
| Auth broken / new machine / token or SSH setup / gh login | `references/auth.md` |
| Create, triage, label, assign, close issues | `references/issues.md` |
| Branch, commit, open PR, watch CI, merge | `references/pr-workflow.md` |
| Carry an ISSUE to a verified PR (full delivery loop) | `references/issue-to-pr.md` |
| Review someone's PR: diffs, inline comments, verdict | `references/code-review.md` |
| Clone/create/fork repos, remotes, releases | `references/repo-management.md` |

Supporting assets: `scripts/gh-env.sh` + `scripts/git-credential-token.py`
(auth helpers), `templates/` (PR bodies, bug report, feature request),
`references/ci-troubleshooting.md`, `references/conventional-commits.md`,
`references/github-api-cheatsheet.md`, `references/review-output-template.md`.

## Core discipline (applies to every workflow)

- Preflight once per session: `gh auth status` — if it fails, go to
  `references/auth.md` before anything else.
- Prefer `gh` over raw REST; drop to `gh api` only for endpoints the
  porcelain lacks (the cheatsheet lists them).
- Never report CI green without checking `gh pr checks` yourself; never
  claim merged without verifying `state,mergedAt`.
- Read full context before writing: `gh issue view --comments` /
  `gh pr view --comments` — decisions live in threads, not titles.
- Sweep for duplicates before creating anything:
  `gh pr list --search` / `gh issue list --search`.

## Verification

- The workflow's own reference file defines done for that task.
- Cross-cutting: every claim about remote state (CI, merge, release,
  issue state) is backed by a fresh `gh` read, never memory.

> **Not imported (scanner refused):** `references\auth.md`, `references\ci-troubleshooting.md`, `references\code-review.md`, `references\github-api-cheatsheet.md`, `references\pr-workflow.md`, `references\repo-management.md`, `scripts\gh-env.sh`. Any step that needs one of these cannot be followed here — work around it or ask the user first.
