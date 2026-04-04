# NetworkX Coding Agent Benchmark — Execution Plan

> **Goal**: Build an unsaturated evaluation benchmark for coding agents using the
> NetworkX graph library. Mine 100 tasks from real PRs, evaluate 3 models via
> mini-swe-agent, produce a report.
>
> **Time budget**: 4 hours
>
> **Repo**: `networkx/networkx` (~5k+ merged PRs, pure Python, NOT in SWE-bench)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Phase A: Agent Runs (local, parallel-safe)                     │
│                                                                 │
│  For each (task, model):                                        │
│    1. git worktree at base_sha  (isolated directory per task)   │
│    2. Apply test patch  (new tests exist but fail)              │
│    3. mini-swe-agent + LocalEnvironment  (agent edits files)    │
│    4. git diff → agent_patch.diff                               │
│                                                                 │
│  Parallel-safe because each worktree is a separate directory.   │
│  No pip install — agent just reads and edits source files.      │
└───────────────────────────┬─────────────────────────────────────┘
                            │  patches
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  Phase B: Scoring (Docker, parallel-safe)                       │
│                                                                 │
│  For each patch:                                                │
│    1. docker run nx-eval with worktree mounted as /repo         │
│    2. git apply agent_patch.diff                                │
│    3. PYTHONPATH=/repo pytest <test_spec> → targeted results    │
│    4. PYTHONPATH=/repo pytest <module_tests> → regression       │
│    5. Output JSON scores to mounted /results volume             │
│                                                                 │
│  Parallel-safe because each container is fully isolated.        │
│  PYTHONPATH avoids any pip install conflicts.                   │
└─────────────────────────────────────────────────────────────────┘
```

**Scoring uses ONLY the repo's own test suite** — no hand-written checks.
The PR's tests are the ground truth. That's the whole SWE-bench insight.

---

## 0. Manual Prerequisites (do before handing to agent)

```bash
mkdir -p ~/nx-bench && cd ~/nx-bench

# Clone repos
git clone https://github.com/networkx/networkx.git
git clone https://github.com/SWE-agent/mini-swe-agent.git
cd mini-swe-agent && pip install -e . && cd ..

# Install other deps (host only needs requests for mining + pytest-json-report for local debug)
pip install requests

# API keys (mini-swe-agent uses LiteLLM which reads these env vars)
export ANTHROPIC_API_KEY="sk-..."
export OPENAI_API_KEY="sk-..."
export GEMINI_API_KEY="..."
export GITHUB_TOKEN="ghp_..."

# Verify
python -c "from minisweagent.agents.default import DefaultAgent; print('mini-swe-agent OK')"
docker info  # Verify Docker running
```

---

## 1. Docker Evaluation Environment (~15 min)

The Docker image is used **only for scoring** — it runs pytest against agent patches
in an isolated, reproducible container. The agent itself runs locally.

### File: `~/nx-bench/Dockerfile.eval`

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Install networkx dependencies (but NOT networkx itself — that comes from the mounted worktree)
RUN pip install --no-cache-dir \
    numpy scipy matplotlib pandas pytest pytest-json-report

WORKDIR /repo

# Entrypoint: apply patch, run tests, output results
COPY score_in_docker.sh /score_in_docker.sh
RUN chmod +x /score_in_docker.sh

ENTRYPOINT ["/score_in_docker.sh"]
```

### File: `~/nx-bench/score_in_docker.sh`

```bash
#!/bin/bash
# Usage: docker run -v <worktree>:/repo -v <results_dir>:/results nx-eval <patch_file> <test_spec> <regression_dir>
#
# This script:
#   1. Applies the agent's patch to the mounted /repo
#   2. Runs targeted tests (the PR's specific tests)
#   3. Runs regression tests (broader module tests)
#   4. Writes JSON results to /results/
#
# PYTHONPATH=/repo ensures we import networkx from the mounted worktree,
# NOT from any installed package. This is the key to parallel isolation.

PATCH_FILE="$1"
TEST_SPEC="$2"
REGRESSION_DIR="$3"

cd /repo

# Apply agent's patch (if any)
PATCH_APPLIED="false"
if [ -n "$PATCH_FILE" ] && [ -f "$PATCH_FILE" ]; then
    git apply --allow-empty "$PATCH_FILE" 2>/dev/null && PATCH_APPLIED="true"
fi

# Run targeted tests (the PR's fail-to-pass tests)
if [ -n "$TEST_SPEC" ] && [ -f "$TEST_SPEC" ]; then
    PYTHONPATH=/repo python -m pytest "$TEST_SPEC" \
        --json-report --json-report-file=/results/targeted.json \
        -x -q --tb=short 2>&1 | tail -20 > /results/targeted_stdout.txt
else
    echo '{"tests":[]}' > /results/targeted.json
fi

# Run regression tests (broader module — are existing tests still passing?)
if [ -n "$REGRESSION_DIR" ] && [ -d "$REGRESSION_DIR" ]; then
    PYTHONPATH=/repo python -m pytest "$REGRESSION_DIR" \
        --json-report --json-report-file=/results/regression.json \
        -q --tb=no 2>&1 | tail -5 > /results/regression_stdout.txt
else
    echo '{"tests":[]}' > /results/regression.json
fi

# Write metadata
echo "{\"patch_applied\": $PATCH_APPLIED}" > /results/meta.json
```

### Build & smoke test:

```bash
cd ~/nx-bench
docker build -f Dockerfile.eval -t nx-eval .

# Smoke test: run pytest on the real repo
docker run --rm -v $(pwd)/networkx:/repo nx-eval "" \
    "networkx/algorithms/tests/test_shortest_paths.py" \
    "networkx/algorithms/tests"
```

---

## 2. Task Mining (~30 min)

### File: `~/nx-bench/mine_tasks.py`

```python
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
```

### Run:

```bash
cd ~/nx-bench && python mine_tasks.py
```

---

## 3. Task Generation (~20 min)

### File: `~/nx-bench/generate_tasks.py`

```python
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
from collections import Counter

REPO = os.path.expanduser("~/nx-bench/networkx")


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
            # Prefer a test file in the same submodule as the source change
            for tf in c["test_files"]:
                if "algorithms" in tf or "classes" in tf or "generators" in tf:
                    test_spec = tf
                    break
            if not test_spec:
                test_spec = c["test_files"][0]
            # Regression dir = parent of the test file (run all tests in that directory)
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
```

### Run:

```bash
cd ~/nx-bench && python generate_tasks.py
```

**Checkpoint**: `tasks.json` should have ~100 tasks. If fewer, relax filters in
`mine_tasks.py` (increase `max_pages` or allow up to 12 files per PR).

---

## 4. Scoring Function

### File: `~/nx-bench/score.py`

```python
"""
Compute a [0,1] composite score for a single task evaluation.

Formula:
  score = 0.50 * targeted_test_pass_rate
        + 0.30 * regression_pass_rate
        + 0.10 * patch_parseable
        + 0.10 * patch_size_penalty

We use ONLY the repo's own test suite for scoring. No hand-written checks.
The PR's tests are the ground truth — that's the whole SWE-bench insight.

Targeted tests  = the specific test file(s) from the PR (fail-to-pass)
Regression tests = all tests in the parent module directory (pass-to-pass)
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
```

---

## 5. Benchmark Runner (~90 min wall clock)

### File: `~/nx-bench/run_benchmark.py`

```python
"""
Orchestrate mini-swe-agent across all tasks and models.

Phase A — Agent runs (local, parallelized):
  Each task gets its own git worktree. mini-swe-agent runs with LocalEnvironment.
  The agent edits files in the worktree; we capture the diff.
  No pip install needed — the agent just reads/writes source files.
  Parallel-safe because worktrees are separate directories.

Phase B — Scoring (Docker, parallelized):
  Each patch is scored in an isolated Docker container.
  The worktree is mounted read-only at /repo inside the container.
  The container applies the patch, runs pytest with PYTHONPATH=/repo, outputs JSON.
  Parallel-safe because each container is fully isolated.

Usage:
  python run_benchmark.py                          # full run
  python run_benchmark.py --max-tasks 10           # pilot (DO THIS FIRST)
  python run_benchmark.py --models anthropic/claude-sonnet-4-20250514  # single model
"""

import json, os, argparse, subprocess, shutil, traceback, tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from minisweagent.agents.default import DefaultAgent
from minisweagent.models.litellm_model import LitellmModel
from minisweagent.environments.local import LocalEnvironment

from score import score_task

# ── Config ──────────────────────────────────────────────────────────────

MODELS = [
    "anthropic/claude-sonnet-4-20250514",
    "openai/gpt-4o",
    "gemini/gemini-2.5-flash",
]

N_PARALLEL_AGENTS = 3     # concurrent agent runs (bounded by API rate limits)
N_PARALLEL_SCORING = 6    # concurrent Docker scoring containers
REPO_DIR = Path.home() / "nx-bench" / "networkx"

# ── Helpers ─────────────────────────────────────────────────────────────

def git(cwd, *args, check=True):
    r = subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd), capture_output=True, text=True, timeout=60,
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr[:200]}")
    return r


def setup_worktree(task, workdir):
    """Create an isolated git worktree at the task's base_sha."""
    wt = workdir / task["task_id"]

    # Clean up existing worktree if present
    if wt.exists():
        git(REPO_DIR, "worktree", "remove", "--force", str(wt), check=False)
        if wt.exists():
            shutil.rmtree(wt)

    git(REPO_DIR, "worktree", "add", "--detach", str(wt), task["base_sha"])

    # Apply test diff: new tests now exist but fail (SWE-bench setup)
    if task.get("test_diff"):
        patch_file = workdir / f"{task['task_id']}_test.patch"
        patch_file.write_text(task["test_diff"])
        git(wt, "apply", "--allow-empty", str(patch_file), check=False)

    return wt


def run_agent(task, model_name, workdir):
    """Run mini-swe-agent on one task. Returns dict with patch and status."""
    try:
        wt = setup_worktree(task, workdir)

        agent = DefaultAgent(
            LitellmModel(model_name=model_name),
            LocalEnvironment(),
        )

        # Prompt tells agent to cd into the worktree
        full_prompt = f"cd {wt}\n\n{task['prompt']}"
        agent.run(full_prompt)

        # Capture whatever diff the agent produced
        r = git(wt, "diff", check=False)
        patch = r.stdout

        return {"task_id": task["task_id"], "model": model_name,
                "patch": patch, "status": "success"}

    except Exception as e:
        traceback.print_exc()
        return {"task_id": task["task_id"], "model": model_name,
                "patch": "", "status": f"error: {str(e)[:200]}"}


def score_in_docker(result, task, workdir):
    """
    Score one agent patch using a Docker container.

    1. Copy worktree to a temp directory (so Docker mount is clean)
    2. Write agent patch to a file
    3. docker run nx-eval with the worktree + patch mounted
    4. Read JSON results from mounted results volume
    """
    task_id = task["task_id"]
    model_tag = result["model"].split("/")[-1]
    wt = workdir / task_id

    # Create a temp dir for this scoring run's results
    results_dir = workdir / f"{task_id}_results_{model_tag}"
    results_dir.mkdir(exist_ok=True)

    # Copy worktree so we don't modify the agent's work
    score_repo = workdir / f"{task_id}_score_{model_tag}"
    if score_repo.exists():
        shutil.rmtree(score_repo)
    shutil.copytree(wt, score_repo)

    try:
        # Write agent patch
        patch_path = score_repo / "agent.patch"
        if result["patch"]:
            patch_path.write_text(result["patch"])
        else:
            patch_path.write_text("")

        # Determine test spec and regression dir (paths relative to /repo in container)
        test_spec = task.get("test_spec", "")
        regression_dir = task.get("regression_dir", "")

        # Run scoring in Docker
        docker_cmd = [
            "docker", "run", "--rm",
            "-v", f"{score_repo}:/repo",
            "-v", f"{results_dir}:/results",
            "nx-eval",
            "/repo/agent.patch",
            test_spec,
            regression_dir,
        ]

        subprocess.run(docker_cmd, capture_output=True, timeout=300)

        # Read results
        targeted_json = str(results_dir / "targeted.json")
        regression_json = str(results_dir / "regression.json")

        scores = score_task(task, result["patch"], targeted_json, regression_json)
        result.update(scores)

    except Exception as e:
        result["score"] = 0.0
        result["score_error"] = str(e)[:200]

    # Cleanup score repo (keep results)
    try:
        shutil.rmtree(score_repo)
    except Exception:
        pass

    return result


# ── Main ────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tasks", default="tasks.json")
    p.add_argument("--output", default="results")
    p.add_argument("--max-tasks", type=int, default=100)
    p.add_argument("--models", nargs="+", default=MODELS)
    p.add_argument("--parallel-agents", type=int, default=N_PARALLEL_AGENTS)
    p.add_argument("--parallel-scoring", type=int, default=N_PARALLEL_SCORING)
    args = p.parse_args()

    with open(args.tasks) as f:
        tasks = json.load(f)[: args.max_tasks]

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    workdir = out / "workdirs"
    workdir.mkdir(exist_ok=True)

    all_results = []

    for model in args.models:
        tag = model.split("/")[-1]
        print(f"\n{'='*60}\n  Model: {model}\n{'='*60}")

        # ── Phase A: run agents in parallel ──
        model_patches = []
        with ThreadPoolExecutor(max_workers=args.parallel_agents) as pool:
            futs = {pool.submit(run_agent, t, model, workdir): t for t in tasks}
            for fut in as_completed(futs):
                t = futs[fut]
                r = fut.result()
                patch_lines = len(r["patch"].splitlines()) if r["patch"] else 0
                print(f"  [{r['status'][:7]:7s}] {r['task_id']}  ({patch_lines} diff lines)")
                model_patches.append((r, t))

        # ── Phase B: score in Docker containers in parallel ──
        print(f"\n  Scoring {len(model_patches)} patches in Docker...")
        with ThreadPoolExecutor(max_workers=args.parallel_scoring) as pool:
            futs = {
                pool.submit(score_in_docker, r, t, workdir): r
                for r, t in model_patches
            }
            for fut in as_completed(futs):
                scored = fut.result()
                all_results.append(scored)
                s = scored.get("score", "ERR")
                print(f"    {scored['task_id']}: {s}")

        # Save per-model results
        model_res = [r for r in all_results if r["model"] == model]
        (out / f"{tag}_results.json").write_text(json.dumps(model_res, indent=2))

    # Save combined results
    (out / "all_results.json").write_text(json.dumps(all_results, indent=2))
    print(f"\nDone. All results in {out}/")


if __name__ == "__main__":
    main()
```

### Run (pilot first!):

```bash
cd ~/nx-bench

# PILOT — 10 tasks, catch bugs early
python run_benchmark.py --max-tasks 10 --output results_pilot/

# FULL RUN — only after pilot succeeds
python run_benchmark.py --output results/
```

---

## 6. Analysis & Report (~30 min)

### File: `~/nx-bench/analyze.py`

```python
"""
Analyze benchmark results and print summary tables for the report.
"""

import json
from collections import defaultdict
from pathlib import Path


def main(results_dir="results"):
    data = json.loads((Path(results_dir) / "all_results.json").read_text())
    tasks = {t["task_id"]: t for t in json.loads(Path("tasks.json").read_text())}

    by_model = defaultdict(list)
    for r in data:
        by_model[r["model"]].append(r)

    # ── Overall ──
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

    # ── Per category ──
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

    # ── Component breakdown ──
    comps = ["targeted_test_pass_rate", "regression_pass_rate",
             "patch_parseable", "patch_size_score"]
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

    # ── Error summary ──
    print(f"\n{'='*72}")
    print("  ERROR RATES")
    print("=" * 72)
    for model in sorted(by_model):
        errs = sum(1 for r in by_model[model] if "error" in r.get("status", ""))
        total = len(by_model[model])
        print(f"  {model}: {errs}/{total} errors ({errs/total:.0%})")


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else "results")
```

### Run:

```bash
cd ~/nx-bench && python analyze.py results/
```

### File: `~/nx-bench/report.md`

Write this after `analyze.py`. Use this template:

```markdown
# NetworkX Coding Agent Benchmark

## 1. Environment

- **Repository**: networkx/networkx — pure Python graph algorithms library (~150k LOC)
- **Not in SWE-bench** or any known coding agent benchmark dataset
- **Evaluation**: Docker containers (Python 3.11 + numpy/scipy/pytest)
- **Isolation**: git worktrees for agent runs; Docker containers for scoring;
  PYTHONPATH used instead of pip install to prevent cross-task contamination

## 2. Task Design (N=100)

- Mined from real merged PRs via GitHub API
- **SWE-bench methodology**: roll back source changes, keep new tests from the PR,
  agent must make failing tests pass by modifying source code only
- **Categories**: [INSERT distribution from analyze.py output]
- Algorithmic diversity: shortest paths, centrality, traversal, connected components,
  graph generators, I/O parsers, linear algebra, bipartite, tree algorithms
- Difficulty range: 5-line bugfixes to multi-file feature implementations

## 3. Scoring

Composite score in [0, 1] using **only the repo's own test suite** — no hand-written
checks. The PR's tests are the ground truth.

| Component               | Weight | What it measures                                     |
|-------------------------|--------|------------------------------------------------------|
| Targeted test pass rate | 0.50   | Do the PR's specific fail-to-pass tests pass?        |
| Regression pass rate    | 0.30   | Do existing module tests still pass? (pass-to-pass)  |
| Patch parseable         | 0.10   | Did the model produce a valid unified diff?          |
| Patch size penalty      | 0.10   | Penalizes absurdly large patches (> 500 change-lines)|

Partial credit by design: a model that doesn't solve the task but also breaks nothing
still gets ~0.30 for regression. A model that solves the task but causes regressions
gets penalized. This avoids the binary pass/fail saturation problem of many benchmarks.

## 4. Results

[INSERT overall table from analyze.py]

### Per-Category Breakdown

[INSERT category table from analyze.py]

### Key Observations

[WRITE 3-5 bullet points about: which model is best overall, which model does
best on bugfixes vs features, whether performance-optimization tasks are hardest,
any surprising failure modes observed in agent patches]

## 5. Shortcomings

- Tasks biased toward recent PRs; older, deeper algorithmic changes underrepresented
- 100 tasks yields moderate statistical power; 500+ would tighten confidence intervals
- Single-run; no pass@k analysis to measure variance
- Results reflect model + mini-swe-agent scaffold jointly, not pure model capability
- No code quality or readability dimension in scoring
- Some PRs may have under-specified descriptions, making the task unfairly hard

## 6. Improvements & Scaling

- **500+ tasks**: mine full PR history + issues (not just PRs with test changes)
- **LLM-as-judge**: add code quality/readability scoring via a separate model call
- **pass@k**: run each task k times with temperature > 0, report pass@1 and pass@5
- **Difficulty tiers**: auto-classify by diff size, file count, cyclomatic complexity
- **Efficiency metrics**: track tokens-to-solution and wall-clock time per task
- **Cross-repo**: extend methodology to Biopython, scikit-image, librosa
- **Task-specific verification**: for algorithm tasks, generate targeted inputs where
  ground-truth output is known and test beyond the PR's test coverage
```

---

## Complete File Listing

```
~/nx-bench/
├── networkx/                 # git clone (manual prereq)
├── mini-swe-agent/           # git clone + pip install -e . (manual prereq)
├── Dockerfile.eval           # Docker image for scoring
├── score_in_docker.sh        # Entrypoint script run inside Docker
├── mine_tasks.py             # Step 2: mine PRs from GitHub
├── generate_tasks.py         # Step 3: convert PRs to 100 tasks
├── score.py                  # Step 4: scoring function (called by run_benchmark)
├── run_benchmark.py          # Step 5: orchestrator
├── analyze.py                # Step 6: analysis
├── report.md                 # Step 6: written report
├── tasks_raw.json            # output of mine_tasks.py
├── tasks.json                # output of generate_tasks.py (THE 100 TASKS)
├── results_pilot/            # pilot run output
│   ├── all_results.json
│   └── workdirs/
└── results/                  # full run output
    ├── all_results.json
    ├── claude-sonnet-4-20250514_results.json
    ├── gpt-4o_results.json
    ├── gemini-2.5-flash_results.json
    └── workdirs/
```

---

## Execution Cheat Sheet

```bash
cd ~/nx-bench

# Step 1  (~5 min)   Build Docker scoring image
docker build -f Dockerfile.eval -t nx-eval .

# Step 2  (~15 min)  Mine PRs from GitHub API
python mine_tasks.py

# Step 3  (~5 min)   Generate 100 tasks from mined PRs
python generate_tasks.py

# Step 4  (~20 min)  Pilot run — 10 tasks, verify everything works
python run_benchmark.py --max-tasks 10 --output results_pilot/

# Step 5  (~90 min)  Full run — 100 tasks × 3 models
python run_benchmark.py --output results/

# Step 6  (~10 min)  Analyze and write report
python analyze.py results/
```

---

## Debugging Checklist

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `mine_tasks.py` < 100 candidates | Not enough pages | Increase `max_pages=60` or allow 12 files/PR |
| `generate_tasks.py` skips most | Missing git history | `cd networkx && git fetch --all --tags` |
| `ImportError: minisweagent` | Not installed | `cd mini-swe-agent && pip install -e .` |
| Agent auth failures | Missing API keys | Check env vars for all 3 providers |
| Docker scoring returns empty JSON | score_in_docker.sh not executable | `chmod +x score_in_docker.sh` |
| All scores are 0.0 | pytest-json-report missing in Docker | Rebuild: check Dockerfile has `pytest-json-report` |
| `git worktree` errors | Stale worktrees | `cd networkx && git worktree prune` |
| Pilot > 30 min | Agent doing too many steps | Add `--max-steps` to agent config |
| Rate limits | Too many parallel agents | `--parallel-agents 2` |
| Gemini model string wrong | LiteLLM naming | Try `gemini/gemini-2.5-flash-preview-04-17` |
| Docker can't mount worktree | Path not absolute | Ensure `workdir` paths are absolute |
