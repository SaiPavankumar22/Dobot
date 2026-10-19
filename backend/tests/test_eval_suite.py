"""The eval suite inside pytest: every offline case is one test.

This is what makes evals a *regression* gate instead of a report somebody has to
remember to run — ``uv run pytest`` now includes task-level behaviour. Cases are
parameterised so a failure names exactly which task broke. A second test enforces the
baseline's coverage: a case that passed in ``evals/baseline.json`` cannot quietly
disappear from the suite.
"""

from __future__ import annotations

import json

import pytest

from evals.cases import OFFLINE_CASES
from evals.runner import BASELINE_PATH, _run_case


@pytest.mark.parametrize("case", OFFLINE_CASES, ids=lambda case: case.id)
def test_eval_case(case) -> None:
    result = _run_case(case)
    assert result.passed, f"{result.id}: " + "; ".join(result.failures)


def test_no_baseline_case_disappeared() -> None:
    """Removing a previously-passing case is a coverage regression, not a refactor."""
    if not BASELINE_PATH.exists():
        pytest.skip("no eval baseline yet")
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    if baseline.get("mode") != "offline":
        pytest.skip("baseline belongs to the live mode")
    known = {case.id for case in OFFLINE_CASES}
    vanished = [
        str(item.get("id"))
        for item in baseline.get("results", [])
        if item.get("passed") and str(item.get("id")) not in known
    ]
    assert not vanished, (
        f"cases passed in the baseline are gone from OFFLINE_CASES: {vanished} — "
        "restore them, or rewrite the baseline deliberately (uv run python -m evals.runner --write-baseline)"
    )
