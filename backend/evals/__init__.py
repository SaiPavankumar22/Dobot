"""Dobot eval harness.

Task-level regression suite: real messages through the real HTTP surface, scored by
deterministic assertions (plan shape, world state, verification checks). No LLM judge,
no paid API in the default (offline) mode — the same reason the app's own tests are
deterministic. ``evals.runner`` for humans and CI, ``tests/test_eval_suite.py`` inside
pytest.
"""
