---
name: release_checklist
description: Assemble release evidence — clean tree, green tests, version and changelog — and write the checklist.
trigger: release checklist, are we ready to release, prepare a release, pre release checks, ship this version
required_tools: terminal_run, fs_read, fs_write
safety: Evidence only. Never tags, commits, pushes or publishes; it writes one checklist file.
---

# Release Checklist

Decide whether the current tree is shippable, and prove it. Every gate gets the command that checks
it and the output that passed — evidence, not a feeling.

Borrowed from release-engineering practice where artifacts ship with checksums and smoke results:
the checklist is the same idea applied to a version tag.

Steps:

1. `terminal_run` `git status --short` — a dirty working tree is gate zero, and it is reported
   exactly as it stands (untracked files count).
2. `terminal_run` `git log --oneline -15` to summarise what is actually going out, and
   `git tag --sort=-v:refname` to identify the previous version.
3. `fs_read` the version file that exists (`package.json`, `pyproject.toml`, `Cargo.toml`,
   `pyproject`) and the changelog: does the new version have an entry, and does it match?
4. Run the project's own test command and its build (`npm run build`, `python -m pytest -q`,
   `cargo build`). Keep the real output — counts, durations, failures — as the evidence line.
5. Check the boring gates too: no secrets staged (`git diff --stat` over the release range), no
   TODO that the changelog implies is done, dependencies not left mid-upgrade.
6. `fs_write` `release-checklist-<version>.md` with: what changed since the last tag, one row per
   gate (gate · command · result · evidence), open risks, and — listed but **not executed** — the
   commands a human would run to tag and publish.
7. Report the path, and say plainly whether the tree is shippable.

Safety requirements:

- Evidence only: never tag, commit, push, publish, or edit a tracked file.
- If a gate fails, write it as failed. Do not re-run a suite repeatedly until it turns green — a
  flaky green is a failed gate with worse evidence.
- The tag/publish commands in the report are for the user to run after reading it.
