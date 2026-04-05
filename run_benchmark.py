"""
Orchestrate mini-swe-agent across all tasks and models.

Phase A — Agent runs (local, parallelized via git worktrees)
Phase B — Scoring (Docker, parallelized via containers)

Usage:
  python run_benchmark.py                          # full run
  python run_benchmark.py --max-tasks 10           # pilot
  python run_benchmark.py --models openai/gpt-4o   # single model
"""

import json, os, argparse, subprocess, shutil, traceback, yaml, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()

from minisweagent.agents.default import DefaultAgent
from minisweagent.models.litellm_model import LitellmModel
from minisweagent.environments.local import LocalEnvironment
from score import score_task

# ── Config ──────────────────────────────────────────────────────────────

MODELS = [
    "openai/gpt-4o",
    "openai/gpt-5.4-mini",
    "openai/gpt-5-codex",
]

# Each model gets its own pool of parallel agents
N_AGENTS_PER_MODEL = 5
N_PARALLEL_SCORING = 10
BASE_DIR = Path(__file__).parent.resolve()
REPO_DIR = BASE_DIR / "networkx"

# Load mini-swe-agent default config for agent templates
_MSA_CONFIG_PATH = BASE_DIR / "mini-swe-agent" / "src" / "minisweagent" / "config" / "default.yaml"
_MSA_CONFIG = yaml.safe_load(_MSA_CONFIG_PATH.read_text())

# Thread-safe print
_print_lock = threading.Lock()
def tprint(msg):
    with _print_lock:
        print(msg, flush=True)

# ── Helpers ─────────────────────────────────────────────────────────────

def git(cwd, *args, check=True):
    r = subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd), capture_output=True, text=True, timeout=60,
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr[:200]}")
    return r


def setup_worktree(task, model_tag, workdir):
    """Create an isolated git worktree at the task's base_sha.
    Uses model_tag in the path so different models don't collide."""
    wt = workdir.resolve() / f"{task['task_id']}_{model_tag}"

    if wt.exists():
        git(REPO_DIR, "worktree", "remove", "--force", str(wt), check=False)
        if wt.exists():
            shutil.rmtree(wt)

    git(REPO_DIR, "worktree", "add", "--force", "--detach", str(wt), task["base_sha"])

    # Apply test diff: new tests now exist but fail (SWE-bench setup)
    if task.get("test_diff"):
        patch_file = workdir / f"{task['task_id']}_{model_tag}_test.patch"
        patch_file.write_text(task["test_diff"])
        git(wt, "apply", "--allow-empty", str(patch_file), check=False)

    return wt


def run_agent(task, model_name, workdir):
    """Run mini-swe-agent on one task. Returns dict with patch and status."""
    model_tag = model_name.split("/")[-1]
    try:
        wt = setup_worktree(task, model_tag, workdir)

        model_kwargs = {**_MSA_CONFIG.get("model", {})}
        model = LitellmModel(model_name=model_name, **model_kwargs)
        env = LocalEnvironment(cwd=str(wt), **_MSA_CONFIG.get("environment", {}))
        agent_kwargs = {**_MSA_CONFIG["agent"]}
        agent_kwargs["step_limit"] = 50  # cap agent steps
        agent_kwargs["cost_limit"] = 5.0  # cap cost per task
        agent = DefaultAgent(
            model=model, env=env,
            **agent_kwargs,
        )

        agent.run(task=task["prompt"])

        # Capture all changes: tracked modifications + new untracked files
        git(wt, "add", "-A", check=False)
        r = git(wt, "diff", "--cached", check=False)
        patch = r.stdout

        return {"task_id": task["task_id"], "model": model_name,
                "patch": patch, "status": "success"}

    except Exception as e:
        traceback.print_exc()
        return {"task_id": task["task_id"], "model": model_name,
                "patch": "", "status": f"error: {str(e)[:200]}"}


def score_in_docker(result, task, workdir):
    """Score one agent patch in a Docker container."""
    task_id = task["task_id"]
    model_tag = result["model"].split("/")[-1]
    workdir = workdir.resolve()
    wt = workdir / f"{task_id}_{model_tag}"

    results_dir = workdir / f"{task_id}_results_{model_tag}"
    results_dir.mkdir(exist_ok=True)

    # Copy worktree to a plain directory (no .git worktree pointer issues)
    score_repo = workdir / f"{task_id}_score_{model_tag}"
    if score_repo.exists():
        shutil.rmtree(score_repo)

    if not wt.exists():
        result["score"] = 0.0
        result["score_error"] = "worktree not found (agent setup failed)"
        return result

    shutil.copytree(wt, score_repo)

    try:
        # Write agent patch
        patch_path = score_repo / "agent.patch"
        patch_path.write_text(result["patch"] if result["patch"] else "")

        test_spec = task.get("test_spec") or ""
        regression_dir = task.get("regression_dir") or ""

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

        targeted_json = str(results_dir / "targeted.json")
        regression_json = str(results_dir / "regression.json")

        scores = score_task(task, result["patch"], targeted_json, regression_json)
        result.update(scores)

    except Exception as e:
        result["score"] = 0.0
        result["score_error"] = str(e)[:200]

    # Clean up score copy and worktree to save disk space
    for d in [score_repo, wt]:
        try:
            shutil.rmtree(d)
        except Exception:
            pass

    return result


# ── Per-model runner ───────────────────────────────────────────────────

def run_model(model, tasks, workdir, out, n_agents, n_scoring):
    """Run all tasks for one model (agent phase + scoring phase).
    Designed to be called from a thread — one thread per model."""
    tag = model.split("/")[-1]
    tprint(f"\n{'='*60}\n  [{tag}] Starting {len(tasks)} tasks (parallelism={n_agents})\n{'='*60}")

    # Phase A: run agents
    model_patches = []
    with ThreadPoolExecutor(max_workers=n_agents) as pool:
        futs = {pool.submit(run_agent, t, model, workdir): t for t in tasks}
        for fut in as_completed(futs):
            t = futs[fut]
            r = fut.result()
            patch_lines = len(r["patch"].splitlines()) if r["patch"] else 0
            tprint(f"  [{tag}] [{r['status'][:7]:7s}] {r['task_id']}  ({patch_lines} diff lines)")
            model_patches.append((r, t))

    tprint(f"\n  [{tag}] Scoring {len(model_patches)} patches in Docker...")

    # Phase B: score in Docker
    scored_results = []
    with ThreadPoolExecutor(max_workers=n_scoring) as pool:
        futs = {
            pool.submit(score_in_docker, r, t, workdir): r
            for r, t in model_patches
        }
        for fut in as_completed(futs):
            scored = fut.result()
            scored_results.append(scored)
            s = scored.get("score", "ERR")
            tprint(f"    [{tag}] {scored['task_id']}: {s}")

    # Save per-model results
    (out / f"{tag}_results.json").write_text(json.dumps(scored_results, indent=2))
    tprint(f"\n  [{tag}] Done. {len(scored_results)} results saved.")
    return scored_results


# ── Main ────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tasks", default="tasks.json")
    p.add_argument("--output", default="results")
    p.add_argument("--max-tasks", type=int, default=100)
    p.add_argument("--models", nargs="+", default=MODELS)
    p.add_argument("--agents-per-model", type=int, default=N_AGENTS_PER_MODEL)
    p.add_argument("--parallel-scoring", type=int, default=N_PARALLEL_SCORING)
    args = p.parse_args()

    with open(args.tasks) as f:
        tasks = json.load(f)[: args.max_tasks]

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    workdir = out / "workdirs"
    workdir.mkdir(exist_ok=True)

    # Prune stale worktrees once at startup (not per-task to avoid race conditions)
    git(REPO_DIR, "worktree", "prune", check=False)

    tprint(f"Running {len(tasks)} tasks x {len(args.models)} models IN PARALLEL")
    tprint(f"Agents per model: {args.agents_per_model}, Scoring workers: {args.parallel_scoring}")

    # Run ALL models in parallel — each model gets its own thread + agent pool
    all_results = []
    with ThreadPoolExecutor(max_workers=len(args.models)) as model_pool:
        futs = {
            model_pool.submit(
                run_model, model, tasks, workdir, out,
                args.agents_per_model, args.parallel_scoring,
            ): model
            for model in args.models
        }
        for fut in as_completed(futs):
            model = futs[fut]
            try:
                results = fut.result()
                all_results.extend(results)
            except Exception as e:
                tprint(f"ERROR: model {model} failed: {e}")
                traceback.print_exc()

    # Save combined results
    (out / "all_results.json").write_text(json.dumps(all_results, indent=2))
    tprint(f"\nDone. {len(all_results)} total results in {out}/")


if __name__ == "__main__":
    main()
