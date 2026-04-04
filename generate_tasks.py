"""
Convert mined PR candidates into 100 evaluation tasks.

SWE-bench methodology:
  - Starting state = repo at base_sha (before the PR was merged)
  - Test patch from PR is APPLIED to starting state (so new tests exist but fail)
  - Agent must modify source code to make the failing tests pass
  - Ground truth = the source-only diff from the PR

Output: tasks.json (100 tasks)
"""

import json, subprocess, os, random
from pathlib import Path
from collections import Counter

REPO = str(Path(__file__).parent / "networkx")


def git(*args, check=True):
    r = subprocess.run(
        ["git"] + list(args),
        cwd=REPO, capture_output=True, text=True, timeout=30,
    )
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r


def get_diff(sha_a, sha_b, paths):
    r = git("diff", sha_a, sha_b, "--", *paths, check=False)
    return r.stdout if r.returncode == 0 else None


def sha_exists(sha):
    r = git("cat-file", "-t", sha, check=False)
    return r.stdout.strip() == "commit"


def make_prompt(t):
    return f"""You are working on the NetworkX graph library codebase (Python).

## Problem

{t['title']}

{t['body']}

## Instructions

Fix or implement the change described above. The relevant source files are
likely in: {', '.join(t['source_files'])}

Existing tests validate the expected behavior. Make the failing tests pass
without breaking any existing tests.

Do NOT modify test files. Only change source code."""


def main():
    with open("tasks_raw.json") as f:
        raw = json.load(f)

    # Make sure we have all commits
    print("Fetching all refs...")
    git("fetch", "--all", "--quiet", check=False)

    tasks = []
    for c in raw:
        if not sha_exists(c["base_sha"]):
            continue
        if not c.get("merge_commit_sha") or not sha_exists(c["merge_commit_sha"]):
            continue

        src_diff = get_diff(c["base_sha"], c["merge_commit_sha"], c["source_files"])
        tst_diff = get_diff(c["base_sha"], c["merge_commit_sha"], c["test_files"]) if c["test_files"] else None

        if not src_diff:
            continue

        # Skip trivially small diffs (< 5 changed lines)
        changed = [l for l in src_diff.splitlines()
                    if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
        if len(changed) < 5:
            continue

        # Determine test spec and regression directory for scoring
        test_spec = None
        regression_dir = None
        if c["test_files"]:
            for tf in c["test_files"]:
                if "algorithms" in tf or "classes" in tf or "generators" in tf:
                    test_spec = tf
                    break
            if not test_spec:
                test_spec = c["test_files"][0]
            regression_dir = str(os.path.dirname(test_spec))

        tasks.append({
            "task_id": f"nx-{c['pr_number']}",
            "pr_number": c["pr_number"],
            "pr_url": c["html_url"],
            "category": c["category"],
            "title": c["title"],
            "base_sha": c["base_sha"],
            "merge_commit_sha": c["merge_commit_sha"],
            "source_files": c["source_files"],
            "test_files": c["test_files"],
            "test_spec": test_spec,
            "regression_dir": regression_dir,
            "source_diff": src_diff,
            "test_diff": tst_diff,
            "prompt": make_prompt(c),
        })

    print(f"Valid tasks: {len(tasks)}")

    # Stratified sample to 100
    TARGET = {"bugfix": 35, "feature": 30, "refactor": 15, "performance": 10, "docs": 10}
    by_cat = {}
    for t in tasks:
        by_cat.setdefault(t["category"], []).append(t)

    final = []
    for cat, n in TARGET.items():
        pool = by_cat.get(cat, [])
        take = min(n, len(pool))
        final.extend(random.sample(pool, take))
        print(f"  {cat}: {take}/{n} (pool {len(pool)})")

    # Fill to 100
    used = {t["task_id"] for t in final}
    extra = [t for t in tasks if t["task_id"] not in used]
    random.shuffle(extra)
    final.extend(extra[: max(0, 100 - len(final))])

    for i, t in enumerate(final):
        t["seq_id"] = i

    with open("tasks.json", "w") as f:
        json.dump(final, f, indent=2)

    print(f"\nFinal: {len(final)} tasks saved to tasks.json")
    print(f"Distribution: {dict(Counter(t['category'] for t in final))}")


if __name__ == "__main__":
    main()
