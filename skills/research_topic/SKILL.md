---
name: research_topic
description: Research a topic on the web and answer with cited sources.
trigger: research this, find recent research, what is the latest on, look this up
required_tools: tavily_research, tavily_search, remember
safety: Read-only. Never submits forms, never signs in, never sends anything.
---

# Research Topic

Turn a question — typed or selected on screen — into a sourced answer.

Steps:

1. If a screen region was selected, use its text as the topic; otherwise use the user's words.
2. Call `tavily_research` with the topic. Tavily runs several queries and returns sources.
3. Cross-check: prefer findings that appear in more than one independent source. If sources disagree,
   say so rather than picking one.
4. Answer in 3-6 sentences with inline citations `[1]`, `[2]` mapped to the source list.
5. If the user asks you to keep the topic, add a `remember` step with one sentence summarising it.

Safety requirements:

- If Tavily is not configured, say so plainly. Never invent sources or citations.
- Stay read-only. Fetching a page is allowed; interacting with an account is not.
