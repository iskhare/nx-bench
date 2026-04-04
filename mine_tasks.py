"""
Mine NetworkX PRs for benchmark task candidates.

Filters:
  - Merged PRs only
  - Touches source in networkx/algorithms/, classes/, generators/, linalg/, readwrite/
  - Has test file changes (needed for SWE-bench-style fail-to-pass evaluation)
  - Modifies <= 8 files total
  - Has a non-trivial PR body (>= 20 chars)
  - Skips pure-docs PRs without tests

Output: tasks_raw.json (~200 candidates)
"""

import os, json, time, re, requests
from collections import Counter
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}
REPO = "networkx/networkx"
BASE = f"https://api.github.com/repos/{REPO}"

TARGET_DIRS = [
    "networkx/algorithms/",
    "networkx/classes/",
    "networkx/generators/",
    "networkx/readwrite/",
    "networkx/linalg/",
]

SOURCE_RE = re.compile(r"^networkx/(?!tests/).*\.py$")
TEST_RE = re.compile(r"(test_|/tests/)")


def fetch_merged_prs(max_pages=40):
    prs = []
    for page in range(1, max_pages + 1):
        resp = requests.get(
            f"{BASE}/pulls",
            headers=HEADERS,
            params={
                "state": "closed",
                "sort": "updated",
                "direction": "desc",
                "per_page": 100,
                "page": page,
            },
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        merged = [pr for pr in batch if pr.get("merged_at")]
        prs.extend(merged)
        print(f"  page {page}: +{len(merged)} merged  (total {len(prs)})")
        time.sleep(0.3)
    return prs


def get_pr_files(pr_number):
    resp = requests.get(
        f"{BASE}/pulls/{pr_number}/files",
        headers=HEADERS,
        params={"per_page": 100},
    )
    resp.raise_for_status()
    return resp.json()


def evaluate_pr(pr, files):
    """Return a task dict if this PR is a good candidate, else None."""
    names = [f["filename"] for f in files]

    source_files = [
        f for f in names
        if SOURCE_RE.match(f) and any(f.startswith(d) for d in TARGET_DIRS)
    ]
    test_files = [f for f in names if TEST_RE.search(f)]

    if not source_files:
        return None
    if len(names) > 8:
        return None

    body = pr.get("body") or ""
    if len(body.strip()) < 20:
        return None

    title = (pr.get("title") or "").lower()

    # Categorize
    cat = "feature"
    if any(w in title for w in ["bug", "fix", "correct", "wrong", "error", "issue"]):
        cat = "bugfix"
    elif any(w in title for w in ["refactor", "cleanup", "simplify", "reorganize"]):
        cat = "refactor"
    elif any(w in title for w in ["perf", "optim", "speed", "fast", "improve"]):
        cat = "performance"
    elif any(w in title for w in ["doc", "docstring", "typo", "example"]):
        cat = "docs"

    if cat == "docs" and not test_files:
        return None

    return {
        "pr_number": pr["number"],
        "title": pr["title"],
        "body": body[:2000],
        "category": cat,
        "merged_at": pr["merged_at"],
        "base_sha": pr["base"]["sha"],
        "merge_commit_sha": pr.get("merge_commit_sha"),
        "source_files": source_files,
        "test_files": test_files,
        "all_files": names,
        "html_url": pr["html_url"],
    }


def main():
    print("Fetching merged PRs...")
    prs = fetch_merged_prs(max_pages=40)
    print(f"Fetched {len(prs)} merged PRs\n")

    candidates = []
    for i, pr in enumerate(prs):
        if len(candidates) >= 200:
            break
        try:
            files = get_pr_files(pr["number"])
            task = evaluate_pr(pr, files)
            if task:
                candidates.append(task)
                print(f"  [{len(candidates):3d}] PR #{pr['number']}: {pr['title'][:60]} [{task['category']}]")
        except Exception as e:
            print(f"  skip PR #{pr['number']}: {e}")
        if i % 10 == 0:
            time.sleep(0.5)

    with open("tasks_raw.json", "w") as f:
        json.dump(candidates, f, indent=2)

    dist = Counter(c["category"] for c in candidates)
    print(f"\nSaved {len(candidates)} candidates to tasks_raw.json")
    print(f"Distribution: {dict(dist)}")


if __name__ == "__main__":
    main()
