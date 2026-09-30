---
name: action_items
description: Turn meeting notes or a notes file into tracked tasks with honest owners and dates.
trigger: extract action items, turn my notes into tasks, follow ups from this meeting, tasks from these notes
required_tools: fs_read, task_create, reminder_create, fs_write
safety: Creates tasks and, when dates are explicit, reminders. Never sends anything, never edits the source notes.
---

# Action Items

Convert a wall of notes into a task list the user can actually work from.

Inspired by ticket-import and reporting workflows: read the raw material, extract structured work
items, and leave an auditable echo of what was created.

Steps:

1. Get the notes: `fs_read` the file the user named, or use the text/attachment they pasted. Never
   modify the source.
2. Separate actions from discussion. An action needs a verb and an owner (explicit, or clearly the
   user). Decisions, statuses and opinions are not tasks — list them under "decided/context" in the
   echo instead.
3. For each action, `task_create` with a title that starts with the verb, a description containing
   the source line it came from, and a deadline **only** when the notes state one ("by Friday" →
   the coming Friday). Never invent a due date.
4. `reminder_create` only when the user asked for reminders or the notes give an unambiguous time.
5. `fs_write` `action-items-<ISO date>.md`: what was created (title · owner · due · source line),
   what was skipped and why, and the open questions (items with no owner or no date).
6. Report the counts: created, skipped, needs-a-decision — and ask about the open questions rather
   than guessing.

Safety requirements:

- No messages, emails or posts on the user's behalf; tasks and reminders only.
- Ambiguous ownership or dates become questions, not guesses.
- Re-running the skill must not duplicate work: check the task list for matching titles first and
  say what already existed.
