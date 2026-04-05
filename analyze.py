"""
Analyze benchmark results and print summary tables for the report.
"""

import json, sys
from collections import defaultdict
from pathlib import Path


def main(results_dir="results"):
    data = json.loads((Path(results_dir) / "all_results.json").read_text())
    tasks = {t["task_id"]: t for t in json.loads(Path("tasks.json").read_text())}

    by_model = defaultdict(list)
    for r in data:
        by_model[r["model"]].append(r)

    # Overall
    print("=" * 72)
    print("  OVERALL RESULTS")
    print("=" * 72)
    print(f"\n{'Model':<40} {'Mean':>7} {'Med':>7} {'Pass@1':>7} {'N':>4}")
    print("-" * 72)
    for model in sorted(by_model):
        scores = [r["score"] for r in by_model[model] if "score" in r]
        if not scores:
            continue
        mean = sum(scores) / len(scores)
        med = sorted(scores)[len(scores) // 2]
        p1 = sum(1 for s in scores if s >= 0.9) / len(scores)
        print(f"{model:<40} {mean:>7.3f} {med:>7.3f} {p1:>7.1%} {len(scores):>4}")

    # Per category
    print(f"\n{'='*72}")
    print("  SCORES BY CATEGORY")
    print("=" * 72)
    for model in sorted(by_model):
        print(f"\n  {model}")
        by_cat = defaultdict(list)
        for r in by_model[model]:
            cat = tasks.get(r["task_id"], {}).get("category", "?")
            if "score" in r:
                by_cat[cat].append(r["score"])
        for cat in sorted(by_cat):
            s = by_cat[cat]
            print(f"    {cat:<20} mean={sum(s)/len(s):.3f}  n={len(s)}")

    # Component breakdown
    comps = ["targeted_test_pass_rate", "regression_pass_rate"]
    print(f"\n{'='*72}")
    print("  SCORE COMPONENTS (mean per model)")
    print("=" * 72)
    header = f"{'Model':<30}" + "".join(f" {c[:18]:>18}" for c in comps)
    print(header)
    print("-" * len(header))
    for model in sorted(by_model):
        row = f"{model.split('/')[-1]:<30}"
        for c in comps:
            vals = [r.get(c, 0) for r in by_model[model]]
            row += f" {sum(vals)/len(vals):>18.3f}"
        print(row)

    # Error summary
    print(f"\n{'='*72}")
    print("  ERROR RATES")
    print("=" * 72)
    for model in sorted(by_model):
        errs = sum(1 for r in by_model[model] if "error" in r.get("status", ""))
        total = len(by_model[model])
        print(f"  {model}: {errs}/{total} errors ({errs/total:.0%})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "results")
