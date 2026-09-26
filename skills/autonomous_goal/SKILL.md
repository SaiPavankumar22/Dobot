---
name: autonomous_goal
description: Turn a goal into scheduled, self-managing work that continues across days.
trigger: work on this goal, keep working until, set up a goal, chip away at, every day until
required_tools: task_create, task_update, automation_create, remember, tavily_search
safety: Creates its own follow-up tasks and schedules, but every schedule must be visible to the user and cancelable from the Automations page. Never schedules anything faster than hourly. Never works unattended on anything irreversible.
---

# Autonomous Goal

A goal ("find me a remote job in Berlin", "get my thesis chapters outlined") is not a single task —
it is a loop of progress, review and next steps. Inspired by goal-runner agents: decompose, schedule,
report, adapt.

Setup:

1. Restate the goal, the finish condition ("done when…"), and the cadence you propose. Ask before
   creating anything.
2. Decompose into concrete tasks with `task_create`. Each task must produce an observable artifact —
   a list, a draft, a shortlist — never just "think about X".
3. Create the loop with `automation_create` at the agreed cadence (never faster than hourly). The
   automation prompt must tell future-you to: check task status, do the next concrete chunk, update
   tasks, and report progress in 2-4 sentences.

Guardrails:

- Every run is read-only by default; anything that writes outside `.dobot/` or your notes folder
  needs the normal approval flow, exactly like a manual request.
- If a run cannot make progress twice in a row, it reports the blocker and waits, instead of
  repeating the same failed approach.
- The user can cancel the loop from the Automations page or by saying so; confirm you understand
  which automation is being cancelled.
