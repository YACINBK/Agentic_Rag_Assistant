#!/bin/bash
set -euo pipefail
MODULE=$1

# ─────────────────────────────────────────────────────────────────────────────
# run_loop_auth.sh — evaluator + test runner for Phase 3a (Auth modules)
#
# Same methodology as run_loop.sh but with Phase 3 file paths.
# Module names map to implementation + test paths:
#
#   auth_dependencies    → app/api/dependencies.py + tests/unit/test_auth_dependencies.py
#   auth_error_handling  → app/api/error_handlers.py + tests/unit/test_auth_error_handling.py
#   auth_service_tests   → (no impl, test only) + tests/unit/test_auth_service.py
#   auth_integration     → (no impl) + tests/integration/test_auth_flow.py
#
# Usage:
#   1. In your OpenCode session: implement the module from the contract
#   2. Run: ./run_loop_auth.sh auth_dependencies
#   3. Check: tail -1 log.md
# ─────────────────────────────────────────────────────────────────────────────

# Map module → file paths
case "$MODULE" in
    auth_dependencies)
        IMPL="app/api/dependencies.py"
        TEST="tests/unit/test_auth_dependencies.py"
        ;;
    auth_error_handling)
        IMPL="app/api/error_handlers.py"
        TEST="tests/unit/test_auth_error_handling.py"
        ;;
    auth_service_tests)
        IMPL=""  # test-only contract — no new implementation
        TEST="tests/unit/test_auth_service.py"
        ;;
    auth_integration)
        IMPL=""  # integration test — no new implementation
        TEST="tests/integration/test_auth_flow.py"
        ;;
    *)
        echo "ERROR: Unknown module '$MODULE'" >&2
        echo "Valid modules: auth_dependencies, auth_error_handling, auth_service_tests, auth_integration" >&2
        exit 1
        ;;
esac

# Step 1: Verify expected files exist
if [ -n "$IMPL" ] && [ ! -f "$IMPL" ]; then
    echo "ABORT: Expected implementation file not found: $IMPL" >&2
    echo "Did OpenCode finish implementing? Check your session." >&2
    exit 1
fi

if [ ! -f "$TEST" ]; then
    echo "ABORT: Expected test file not found: $TEST" >&2
    echo "Did OpenCode finish implementing? Check your session." >&2
    exit 1
fi

if [ ! -f "reviews/${MODULE}_summary.md" ]; then
    echo "ABORT: Expected summary file not found: reviews/${MODULE}_summary.md" >&2
    exit 1
fi

# Step 2: Run tests in clean state (neutral executor)
mkdir -p test_results
pytest "$TEST" -v --tb=short \
    2>&1 | tee "test_results/${MODULE}.txt"
TEST_EXIT=${PIPESTATUS[0]}

if [ "$TEST_EXIT" -ne 0 ]; then
    echo ""
    echo "──── TESTS FAILED (exit $TEST_EXIT) ────"
    echo "Share test_results/${MODULE}.txt with OpenCode for fixes."
    echo ""
fi

# Step 3: Evaluator reads contract + test results (adversarial, automated)
EVAL_FILES="contracts/${MODULE}.md, reviews/${MODULE}_summary.md, $TEST, test_results/${MODULE}.txt"
if [ -n "$IMPL" ]; then
    EVAL_FILES="$EVAL_FILES, $IMPL"
fi

claude -p "You are Evaluator. Read ${EVAL_FILES}. \
The test results file contains actual pytest output — do not assume \
tests passed, read the output. \
Check each contract assertion: PASS or FAIL. \
If FAIL quote the exact failing line or test output. \
Append to log.md: ## [$(date +%Y-%m-%d)] verify | ${MODULE} | PASS or FAIL | failed: N,M \
Write nothing else. Do not fix code."

echo ""
echo "──── EVALUATOR RESULT ────"
tail -1 log.md
