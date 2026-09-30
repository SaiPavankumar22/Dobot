---
name: disk_report
description: Report what is eating a folder — biggest files, stale directories, duplicate-looking copies — without touching anything.
trigger: disk space, what is taking up space, big files, storage report, why is my disk full, clean up space
required_tools: fs_list, terminal_run, fs_write
safety: Read-only. Never deletes, never moves; the report names candidates for the user to review.
---

# Disk Report

Answer "where did my space go?" with numbers, then stop — the user decides what leaves the disk.

Inspired by the organise-desktop-files workflows, with the same rule they use: an assistant that
reports candidates is useful, one that decides what to destroy is not.

Steps:

1. Confirm the target folder (default: the user's home) unless they already named one.
2. `fs_list` the top level and sum sizes per entry to find the heavy directories. For depth, use one
   read-only shell command appropriate to the platform — `du -sh <dir>/*` on macOS/Linux, or
   `powershell -Command "Get-ChildItem -Recurse -File | Sort-Object Length -Descending | Select-Object -First 30 FullName,Length"`
   on Windows. This command is outside the reviewed allowlist, so expect a one-time approval prompt.
3. Identify, with counts:
   - the 20 largest files;
   - directories not modified in 90+ days;
   - duplicate-looking sets (same stem with `(1)`/` - Copy` suffixes, or same name in sibling folders);
   - installers/archives older than a year (`.iso`, `.msi`, `.exe`, `.zip`, `.tar.gz`).
4. `fs_write` `storage-report-<ISO date>.md` beside the target folder: totals, the four lists above,
   and a "candidates for review" section — each item a *suggestion*, never an action taken.
5. Report the path and the three biggest findings.

Safety requirements:

- Read-only commands only: listing, sizing, sorting. No deletion, no moving, no renaming — a wrong
  guess about what is safe to remove is the expensive direction to be wrong in.
- Every recommendation lands in the report for the user to act on; if they then ask to delete
  something, that is a separate request which goes through approval.
