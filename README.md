# nx-bench

An unsaturated evaluation benchmark for coding agents, built from real merged PRs in [networkx/networkx](https://github.com/networkx/networkx).

Uses the SWE-bench methodology: roll back source changes from a PR, keep the new tests (which now fail), and ask a coding agent to make them pass again.

## Models evaluated

| Model | Provider |
|-------|----------|
| GPT-4o | OpenAI |
| GPT-5.4-mini | OpenAI |
| GPT-5-codex | OpenAI |

## Setup

```bash
# 1. Create conda environment
conda create -n nx-bench python=3.11 -y
conda activate nx-bench

# 2. Clone this repo
git clone git@github.com:iskhare/nx-bench.git
cd nx-bench

# 3. Clone dependencies
git clone https://github.com/networkx/networkx.git
git clone https://github.com/SWE-agent/mini-swe-agent.git
cd mini-swe-agent && pip install -e . && cd ..

# 4. Install Python deps
pip install -r requirements.txt

# 5. Set API keys in a .env file
echo "OPENAI_API_KEY=sk-..." > .env
echo "GITHUB_TOKEN=ghp_..." >> .env

# 6. Build Docker scoring image
docker build -f Dockerfile.eval -t nx-eval .
```

## Usage

Run steps in order:

```bash
# Mine PR candidates from GitHub API (~15 min)
python mine_tasks.py

# Generate 100 evaluation tasks from mined PRs (~5 min)
python generate_tasks.py

# Pilot run — 10 tasks, verify everything works (~20 min)
python run_benchmark.py --max-tasks 10 --output results_pilot/

# Full run — 100 tasks x 3 models in parallel (~1.5-2 hrs)
python run_benchmark.py --output results/

# Analyze results
python analyze.py results/
```

## How it works

### Task design
- 100 tasks mined from real merged PRs via GitHub API
- Categories: bugfix, feature, refactor, performance, docs
- Each task = repo at pre-PR state + failing tests from the PR

### Scoring
Composite score in [0, 1] using only the repo's own test suite:

| Component | Weight | What it measures |
|-----------|--------|------------------|
| Targeted test pass rate | 0.70 | Do the PR's fail-to-pass tests pass? |
| Regression pass rate | 0.30 | Do existing module tests still pass? |

A model that does nothing scores ~0.30 (existing tests still pass). Solving the task cleanly scores 1.0. Solving but breaking existing tests lands in between.

### Architecture
- **Agent runs**: Local, parallelized via git worktrees + mini-swe-agent
- **Scoring**: Docker containers running pytest in isolation (PYTHONPATH, no pip install)

## File overview

| File | Purpose |
|------|---------|
| `mine_tasks.py` | Mine PR candidates from GitHub API |
| `generate_tasks.py` | Convert PRs to 100 evaluation tasks |
| `run_benchmark.py` | Orchestrate mini-swe-agent across models |
| `score.py` | Composite [0,1] scoring function |
| `analyze.py` | Results analysis and summary tables |
| `Dockerfile.eval` | Docker image for scoring |
| `score_in_docker.sh` | Entrypoint script for Docker scoring |
