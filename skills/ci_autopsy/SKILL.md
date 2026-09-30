---
name: ci_autopsy
description: Find why the tests or the build are failing and write an evidence-backed autopsy report.
trigger: ci failing, tests failing, build failed, why did the pipeline fail, debug the test failures
required_tools: terminal_run, fs_read, fs_write, tavily_search
safety: Runs the project's own test/lint/build commands and read-only git commands. Never edits source, never pushes, never rewrites history.
---

# CI Autopsy

Turn a red pipeline into a report a human can act on: what failed, the exact evidence, the most
likely cause, and a suggested fix — with confidence stated honestly.

Inspired by multi-agent CI investigation workflows (fetch logs → trace evidence → structured audit
report), run by one careful agent instead of a swarm.

Steps:

1. Identify the project and its test command by reading the manifest (`fs_read` `package.json`,
   `pyproject.toml`, `Makefile` — whichever exists). Do not guess a command.
2. `terminal_run` `git log --oneline -5` and `git status --short` to see what changed most recently —
   the regression is usually in the last commits or in uncommitted work.
3. Run the test suite once, capturing the output (`npm test`, `python -m pytest -q`, `cargo test` —
   the manifest's own command).
4. Group failures by root cause, in this order of likelihood: assertion/logic, import or collection
   error, environment or missing dependency, ordering flake. For a failure that smells like a flake,
   re-run *that one test* exactly once and report whether the result changed.
5. For an error that looks external (a library, an API, a known issue), `tavily_search` the error
   text with paths and local versions stripped, and cite what you find.
6. `fs_write` `ci-autopsy-<ISO date>.md` beside the project with: symptom, exact failing commands,
   quoted evidence (the real error lines), most likely cause, suggested fix, confidence
   (high/medium/low), and what would confirm the diagnosis.
7. Report the file path, the largest failure group, and whether anything was *changed* (it was not).

Safety requirements:

- Only the project's own test/lint/build commands and read-only git commands (`git log`,
  `git status`, `git diff`, `git show`). Anything that changes state — moving commits around,
  rewriting history, publishing — is out of scope and must be asked for separately.
- Commands outside Dobot's reviewed allowlist will prompt for approval; that prompt is expected, not
  an error to work around.
- Never claim a fix was applied. Suggest it, cite the evidence, and stop.
- One diagnostic run plus one targeted re-run per suspicion — do not loop on a red suite.
