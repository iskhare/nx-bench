"""
Compute a [0,1] composite score for a single task evaluation.

Formula:
  score = 0.70 * targeted_test_pass_rate
        + 0.30 * regression_pass_rate

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


def score_task(task, patch_text, targeted_json, regression_json):
    """Return a dict with the composite score and all components."""
    targeted = _test_pass_rate(targeted_json)
    regression = _test_pass_rate(regression_json)

    composite = 0.70 * targeted + 0.30 * regression

    return {
        "score": round(composite, 4),
        "targeted_test_pass_rate": round(targeted, 4),
        "regression_pass_rate": round(regression, 4),
    }
