# Take-home Interview (24h)

**Goal:** Turn a large real-world codebase into an unsaturated evaluation benchmark for coding agents and compare models.

You may use **any tools or models** to design the environment, tasks, and evaluators.

## Task

1. **Repository**
   - Choose a public GitHub repository with **≥1,000 merged PRs**.
2. **Evaluation Environment**
   - Convert the repository into one or more **evaluation environments** (e.g. Docker-based).
   - The environment must support automatic evaluation of model-generated code changes.
3. **Tasks**
   - Design **100 evaluation questions/tasks**.
   - Tasks may be based on PRs, issues, tests, code structure, docs, configs, or anything else.
   - Each task defines a starting state and a prompt.
4. **Scoring**
   - Implement an automatic scoring procedure that maps a solution to a score in [0,1].
5. **Benchmark**
   - Use **mini-swe-agent** to evaluate **≥3 models/configurations** on the tasks.
6. **Report**
   - Briefly describe the environment, task design, scoring method, results, shortcomings, and how you would improve or scale the benchmark.

## Submission

You have up to 24h to finish the task. If you are not able to complete the task, submit your incomplete solution.

Submit **only**:
- The evaluation environment(s),
- Scripts/procedures for task generation, evaluation, scoring, and benchmarking,
- A short written report.

Multiple scripts are allowed.

## Evaluation Criteria
- Creativity and taste
- Quality of environment, task, and scorer design
- Meaningfulness of the [0,1] score
- Soundness of model comparison
- Engineering judgment
- Insightfulness of analysis and proposed improvements