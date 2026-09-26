---
name: decision_journal
description: Record decisions with their reasoning, and surface them when the same choice comes back.
trigger: log this decision, decision journal, what did I decide about, record that I chose, why did we choose
required_tools: remember, memory_write, fs_read
safety: Records decisions as memories with their reasoning, never rewrites history — a later decision is appended alongside the old one, not substituted for it.
---

# Decision Journal

Good decisions get lost when their *reasoning* is lost. Inspired by decision-log practice: when the
user makes a choice, capture what was chosen, why, and what was rejected — so future-you can surface
it when the same choice comes back.

Recording a decision — "log this decision":

1. Capture four things in one memory: **the decision** (what was chosen), **the reasoning** (why),
   **the alternatives considered** (what else was on the table, and why it lost), and **the date**.
2. Store it as a FACT via `remember` (it is a stable attribute of the user's project, not an
   episode). Prefix the text with "Decision:" so it is greppable.
3. Confirm in one line what you stored. If the user's message contains no reasoning at all, ask for
   one sentence of it — a decision log without a why is a list, not a journal.

Surfacing — "what did I decide about…":

1. Search memories for the topic; report each decision with its date and reasoning.
2. If an older decision conflicts with what the user is now saying, surface it explicitly:
   "You decided X on <date> because <reason> — do you want to revisit that, or is this different?"

Revisiting:

- A new decision on the same topic is stored as a **new** memory alongside the old one, never an
  edit of the old one. The history of a decision is part of the decision.
