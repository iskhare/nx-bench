"""
Orchestrate mini-swe-agent across all tasks and models.

Phase A — Agent runs (local, parallelized via git worktrees)
Phase B — Scoring (Docker, parallelized via containers)

Usage:
  python run_benchmark.py                          # full run
  python run_benchmark.py --max-tasks 10           # pilot
  python run_benchmark.py --models openai/gpt-4o   # single model
"""

import json, os, argparse, subprocess, shutil, traceback, yaml
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
    "together_ai/Qwen/Qwen3-Coder-Next-FP8",
    "together_ai/openai/gpt-oss-120b",
]

N_PARALLEL_AGENTS = 3
N_PARALLEL_SCORING = 6
BASE_DIR = Path(__file__).parent.resolve()
REPO_DIR = BASE_DIR / "networkx"

# Load mini-swe-agent default config for agent templates
_MSA_CONFIG_PATH = BASE_DIR / "mini-swe-agent" / "src" / "minisweagent" / "config" / "default.yaml"
_MSA_CONFIG = yaml.safe_load(_MSA_CONFIG_PATH.read_text())

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
    wt = workdir.resolve() / task["task_id"]

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

        model = LitellmModel(model_name=model_name, **_MSA_CONFIG.get("model", {}))
        env = LocalEnvironment(cwd=str(wt), **_MSA_CONFIG.get("environment", {}))
        agent = DefaultAgent(
            model=model, env=env,
            **_MSA_CONFIG["agent"],
        )

        agent.run(task=task["prompt"])

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
    """Score one agent patch in a Docker container."""
    task_id = task["task_id"]
    model_tag = result["model"].split("/")[-1]
    workdir = workdir.resolve()
    wt = workdir / task_id

    results_dir = workdir / f"{task_id}_results_{model_tag}"
    results_dir.mkdir(exist_ok=True)

    # Copy worktree to a plain directory (no .git worktree pointer issues)
    score_repo = workdir / f"{task_id}_score_{model_tag}"
    if score_repo.exists():
        shutil.rmtree(score_repo)
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

        # Phase A: run agents in parallel
        model_patches = []
        with ThreadPoolExecutor(max_workers=args.parallel_agents) as pool:
            futs = {pool.submit(run_agent, t, model, workdir): t for t in tasks}
            for fut in as_completed(futs):
                t = futs[fut]
                r = fut.result()
                patch_lines = len(r["patch"].splitlines()) if r["patch"] else 0
                print(f"  [{r['status'][:7]:7s}] {r['task_id']}  ({patch_lines} diff lines)")
                model_patches.append((r, t))

        # Phase B: score in Docker
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
