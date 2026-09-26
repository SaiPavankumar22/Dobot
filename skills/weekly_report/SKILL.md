---
name: weekly_report
description: Build the user's weekly project report from repository activity.
trigger: weekly report, prepare my report, friday report
required_tools: terminal_run, fs_read, fs_write, tavily_search
safety: Writes one report file inside the project. Never pushes, never commits, never sends anything.
---

# Weekly Report

Produce a weekly summary document for the active project.

Steps:

1. `terminal_run` `git log --since="7 days ago" --pretty=format:"%h %s"` in the project directory.
2. `terminal_run` `git status --short` to include uncommitted work.
3. Optionally `tavily_search` for external context the user asked about (releases, incidents).
4. `fs_write` the report to `weekly-report-<ISO date>.md` inside the project.
5. Report the path and the number of commits summarised.

Safety requirements:

- Never `git push`, `git commit`, or modify the working tree.
- The report is a new file; do not overwrite an existing report without confirmation.
- If a step fails because the directory is not a git repository, say so instead of guessing.
