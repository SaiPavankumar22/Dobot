---
name: weekly-review-planning
description: Weekly reset: commitments, stalled work, next-week plan.
trigger: Weekly reset: commitments, stalled work, next-week plan.
required_tools: [automation_create, task_create]
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
# Weekly Review and Planning

Run a bounded weekly reset across the user's chosen systems. This is a concrete recurring task, not a generic productivity methodology — the `weekly-review` Automation Blueprint schedules it as a cron job.

## When to Use

- "Run my weekly review."
- "What did I commit to and what is slipping?"
- "Plan next week from my calendar, tasks, and notes."
- "Find stale projects and waiting items."
- A cron tick fires for a scheduled weekly review.

Don't use for: daily briefs (see the `google-workspace` daily-brief reference) or single-inbox triage (`email-inbox-triage`).

## Procedure

### 1. Set systems and window

Confirm timezone, review period, planning horizon, authoritative task/project store, calendars, inboxes, and allowed writes. Default to recommendations/drafts, not mutations. Done when source-of-truth conflicts have a declared winner.

### 2. Review calendar evidence

Load `google-workspace` or the relevant calendar connector. Inspect the completed week for meetings and commitments, then the next 1-2 weeks for deadlines, travel, preparation, and capacity. Capture follow-ups implied by past events and conflicts ahead. Done when both retrospective and horizon are covered.

### 3. Clear capture inboxes

Review the task inbox, notes (`obsidian`, `notion`), flagged email (`email-inbox-triage` owns thread-level triage), and other declared capture points. Convert each item to next action, project, waiting, scheduled, someday, reference, archive, or delete proposal. Do not mutate until scope is approved. Done when remaining unprocessed items are counted and stated.

### 4. Reconcile active projects

For each project identify desired outcome, next action, owner, deadline, blocker, last meaningful activity, and source link. Flag projects with no next action, missed dates, duplicate records, or contradictory status. Done when every active project is actionable or explicitly paused.

### 5. Review waiting and commitments

Find promises made by the user and items owed by others. Propose follow-ups with dates and channels. Do not infer that silence means completion. Done when each waiting item has an owner and next review/follow-up date.

### 6. Build a capacity-aware plan

Estimate fixed calendar load and select a small set of weekly outcomes plus near-term next actions. Rank by consequence, deadline, dependency, and effort; do not fill every free hour. Done when the plan fits actual capacity and names deferred work.

### 7. Apply approved updates

Update tasks/projects, create calendar holds, archive processed items, and draft follow-ups only as approved. Read every changed record back from the provider. Done when verified writes match the review summary.

## Output Shape

1. Wins and completed commitments
2. Overdue or at risk
3. Waiting/follow-ups
4. Stalled or ambiguous projects
5. Next week's outcomes and calendar constraints
6. Proposed updates awaiting approval
7. Coverage gaps

## Pitfalls

- Planning from tasks without calendar capacity.
- Carrying every unfinished item forward as high priority.
- Marking projects active with no next action.
- Silently deleting or rescheduling personal commitments.
- Treating silence from others as completion.

## Verification

- [ ] Both the completed week and the planning horizon were covered, or gaps are stated.
- [ ] Every stalled/waiting flag traces to a specific record, event, or thread.
- [ ] No task, event, or note was mutated without approval; approved writes were read back.
- [ ] The plan names what was deferred, not just what was chosen.
