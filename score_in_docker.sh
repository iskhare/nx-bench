#!/bin/bash
# Usage: docker run -v <repo_copy>:/repo -v <results_dir>:/results nx-eval <patch_file> <test_spec> <regression_dir>
#
# Uses patch(1) instead of git apply so it works on plain directory copies
# (git worktree .git files point to the host and are invalid inside Docker).

PATCH_FILE="$1"
TEST_SPEC="$2"
REGRESSION_DIR="$3"

cd /repo

# Apply agent's patch (if any)
PATCH_APPLIED="false"
if [ -n "$PATCH_FILE" ] && [ -f "$PATCH_FILE" ]; then
    patch -p1 < "$PATCH_FILE" 2>/dev/null && PATCH_APPLIED="true"
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
