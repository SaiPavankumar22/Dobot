---
name: second_brain
description: Save, recall, review and prune Dobot's long-term memory of you and your work.
trigger: remember this, what do you know about, update your memory, forget that, my second brain, what have you learned about me
required_tools: remember, memory_write, fs_read, fs_list
safety: Writes only what the user explicitly stated. Never invents facts to store. Forgetting anything requires an explicit user confirmation in the conversation, never a silent delete.
---

# Second Brain

Dobot already remembers; this skill is the discipline for when the user wants to *manage* what it
remembers. Inspired by second-brain agent skills: capture, recall, review, prune.

Capture — "remember this":

1. Restate what you are about to store in one clear sentence before storing it.
2. Store with `remember`, choosing the type the user's statement implies: a stable attribute of the
   user is a FACT, a preference about how Dobot should behave is a PREFERENCE, everything else is an
   EPISODE.
3. Never store secrets (passwords, API keys, wallet phrases) as memories. Refuse and explain why.

Recall — "what do you know about…":

1. Search the user's words against memories and the canonical ledger; answer with what is actually
   stored, cited to when it was learned where possible.
2. If nothing is stored, say "I have nothing saved about that yet" and offer to save it now.

Review — "what have you learned about me":

1. Summarise the canonical ledger first (it is the distilled truth), then notable FACT and
   PREFERENCE memories.
2. Flag anything that looks stale or contradictory and ask whether to update it.

Prune — "forget that":

1. Never delete on the first mention. Confirm exactly what will be forgotten.
2. Prefer updating a wrong fact over deleting it; deletion is for things the user wants gone.
