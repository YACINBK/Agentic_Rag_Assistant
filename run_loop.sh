#!/bin/bash
set -euo pipefail
NODE=$1

# ─────────────────────────────────────────────────────────────────────────────
# run_loop.sh — evaluator + test runner for the loop engineering cycle
#
# The GENERATION step happens in your persistent OpenCode session (interactive).
# This script handles ONLY the neutral test execution and adversarial evaluation.
#
# Usage:
#   1. In your OpenCode session: implement the node from the contract
#   2. Run: ./run_loop.sh node_01_classifier
#   3. Check: tail -1 log.md
#
# If PASS → squash-merge. If FAIL → tell OpenCode which assertions failed.
# ─────────────────────────────────────────────────────────────────────────────

# Step 1: Verify expected files exist
for f in "app/pipeline/nodes/${NODE}.py" "tests/unit/test_${NODE}.py" \
         "reviews/${NODE}_summary.md"; do
    if [ ! -f "$f" ]; then
        echo "ABORT: Expected file not found: $f" >&2
        echo "Did OpenCode finish implementing? Check your session." >&2
        exit 1
    fi
done

# Step 2: Run tests in clean state (neutral executor)
mkdir -p test_results
pytest "tests/unit/test_${NODE}.py" -v --tb=short \
    2>&1 | tee "test_results/${NODE}.txt"
TEST_EXIT=${PIPESTATUS[0]}

if [ "$TEST_EXIT" -ne 0 ]; then
    echo ""
    echo "──── TESTS FAILED (exit $TEST_EXIT) ────"
    echo "Share test_results/${NODE}.txt with OpenCode for fixes."
    echo ""
fi

# Step 3: Evaluator reads contract + test results (adversarial, automated)
claude -p "You are Evaluator. Read contracts/${NODE}.md, \
reviews/${NODE}_summary.md, app/pipeline/nodes/${NODE}.py, \
tests/unit/test_${NODE}.py, and test_results/${NODE}.txt. \
The test results file contains actual pytest output — do not assume \
tests passed, read the output. \
Check each contract assertion: PASS or FAIL. \
If FAIL quote the exact failing line or test output. \
Append to log.md: ## [$(date +%Y-%m-%d)] verify | ${NODE} | PASS or FAIL | failed: N,M \
Write nothing else. Do not fix code."

echo ""
echo "──── EVALUATOR RESULT ────"
tail -1 log.md
