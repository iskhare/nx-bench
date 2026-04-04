"""
Compute a [0,1] composite score for a single task evaluation.

Formula:
  score = 0.50 * targeted_test_pass_rate
        + 0.30 * regression_pass_rate
        + 0.10 * patch_parseable
        + 0.10 * patch_size_penalty

Uses ONLY the repo's own test suite for scoring. No hand-written checks.
"""

import json


def _test_pass_rate(json_report_path):
    """Parse pytest-json-report output, return fraction of tests passed."""
    try:
        with open(json_report_path) as f:
            data = json.load(f)
        tests = data.get("tests", [])
        if not tests:
            return 0.0
        return sum(1 for t in tests if t["outcome"] == "passed") / len(tests)
    except Exception:
        return 0.0


def _patch_quality(patch_text):
    """Return (parseable_score, size_score) each in [0,1]."""
    if not patch_text or not patch_text.strip():
        return 0.0, 1.0

    lines = patch_text.strip().splitlines()
    has_header = any(
        l.startswith("diff ") or l.startswith("--- ") for l in lines[:10]
    )
    parseable = 1.0 if has_header else 0.5

    change_lines = [
        l for l in lines
        if (l.startswith("+") or l.startswith("-"))
        and not l.startswith(("+++", "---"))
    ]
    n = len(change_lines)
    size_score = max(0.0, 1.0 - max(0, n - 500) / 1000) if n > 0 else 0.0

    return parseable, size_score


def score_task(task, patch_text, targeted_json, regression_json):
    """Return a dict with the composite score and all components."""
    targeted = _test_pass_rate(targeted_json)
    regression = _test_pass_rate(regression_json)
    parseable, size = _patch_quality(patch_text)

    composite = (
        0.50 * targeted
        + 0.30 * regression
        + 0.10 * parseable
        + 0.10 * size
    )

    return {
        "score": round(composite, 4),
        "targeted_test_pass_rate": round(targeted, 4),
        "regression_pass_rate": round(regression, 4),
        "patch_parseable": round(parseable, 4),
        "patch_size_score": round(size, 4),
    }
