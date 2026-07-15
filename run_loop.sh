#!/bin/bash
set -euo pipefail
NODE=$1
TIMESTAMP=$(date +%s)

# Step 1: Generator implements node + tests
opencode "Read contracts/${NODE}.md, contracts/conftest.md, \
and CLAUDE.md sections 6,7,11. \
Implement app/pipeline/nodes/${NODE}.py and tests/unit/test_${NODE}.py. \
Import all state factories and mock fixtures from tests/conftest.py. \
Write summary to reviews/${NODE}_summary.md. Do not touch any other file."

# Step 2: Verify expected files exist and were modified
for f in "app/pipeline/nodes/${NODE}.py" "tests/unit/test_${NODE}.py" \
         "reviews/${NODE}_summary.md"; do
    if [ ! -f "$f" ]; then
        echo "ABORT: Generator did not produce $f" >&2
        exit 1
    fi
    file_mtime=$(stat -c %Y "$f")
    if [ "$file_mtime" -lt "$TIMESTAMP" ]; then
        echo "ABORT: $f was not modified during this run" >&2
        exit 1
    fi
done

# Step 3: Run tests in clean state (neutral executor — neither agent)
mkdir -p test_results
pytest "tests/unit/test_${NODE}.py" -v --tb=short \
    2>&1 | tee "test_results/${NODE}.txt"
TEST_EXIT=${PIPESTATUS[0]}

# Step 4: Evaluator reads contract + diff + test results
claude -p "You are Evaluator. Read contracts/${NODE}.md, \
reviews/${NODE}_summary.md, app/pipeline/nodes/${NODE}.py, \
tests/unit/test_${NODE}.py, and test_results/${NODE}.txt. \
The test results file contains actual pytest output — do not assume \
tests passed, read the output. \
Check each contract assertion: PASS or FAIL. \
If FAIL quote the exact failing line or test output. \
Append to log.md: ## [$(date +%Y-%m-%d)] verify | ${NODE} | PASS or FAIL | failed: N,M \
Write nothing else. Do not fix code."

tail -1 log.md
