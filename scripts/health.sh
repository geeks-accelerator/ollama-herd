#!/usr/bin/env bash
# Project health check — one command to know if everything is good.
# Usage: ./scripts/health.sh
#
# Checks: tests pass, lint clean, format clean, test count hasn't dropped.

set -euo pipefail

EXPECTED_MIN_TESTS=212
PASS=0
FAIL=0
RESULTS=()

green() { printf "\033[32m✓ %s\033[0m\n" "$1"; }
red()   { printf "\033[31m✗ %s\033[0m\n" "$1"; }
yellow(){ printf "\033[33m⚠ %s\033[0m\n" "$1"; }

record_pass() { green "$1"; RESULTS+=("PASS: $1"); PASS=$((PASS + 1)); }
record_fail() { red "$1";   RESULTS+=("FAIL: $1"); FAIL=$((FAIL + 1)); }

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Ollama Herd — Project Health Check"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 1. Tests pass
echo "Running tests..."
TEST_OUTPUT=$(uv run pytest --tb=short 2>&1) || true
if echo "$TEST_OUTPUT" | grep -q "passed"; then
    # Extract test count
    TEST_COUNT=$(echo "$TEST_OUTPUT" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+')
    if [ -n "$TEST_COUNT" ] && [ "$TEST_COUNT" -ge "$EXPECTED_MIN_TESTS" ]; then
        record_pass "Tests pass ($TEST_COUNT passed, expected ≥$EXPECTED_MIN_TESTS)"
    elif [ -n "$TEST_COUNT" ]; then
        record_fail "Test count dropped ($TEST_COUNT passed, expected ≥$EXPECTED_MIN_TESTS)"
    else
        record_fail "Could not parse test count"
    fi

    # Check for failures
    if echo "$TEST_OUTPUT" | grep -qE '[0-9]+ failed'; then
        FAILED_COUNT=$(echo "$TEST_OUTPUT" | grep -oE '[0-9]+ failed' | grep -oE '[0-9]+')
        record_fail "$FAILED_COUNT test(s) failed"
    fi
else
    record_fail "Tests did not pass"
    echo "$TEST_OUTPUT" | tail -20
fi

# 2. Lint clean
echo "Running linter..."
LINT_OUTPUT=$(uv run ruff check src/ 2>&1) && LINT_EXIT=0 || LINT_EXIT=$?
if [ "$LINT_EXIT" -eq 0 ]; then
    record_pass "Lint clean (ruff check)"
else
    LINT_ERRORS=$(echo "$LINT_OUTPUT" | grep -cE '^\s+-->' || true)
    record_fail "Lint issues ($LINT_ERRORS findings — run: uv run ruff check src/)"
fi

# 3. Format clean
echo "Checking format..."
FORMAT_OUTPUT=$(uv run ruff format --check src/ 2>&1) && FORMAT_EXIT=0 || FORMAT_EXIT=$?
if [ "$FORMAT_EXIT" -eq 0 ]; then
    record_pass "Format clean (ruff format)"
else
    FORMAT_COUNT=$(echo "$FORMAT_OUTPUT" | grep -c "Would reformat" || true)
    record_fail "Format issues ($FORMAT_COUNT file(s) — run: uv run ruff format src/)"
fi

# 4. CLAUDE.md test count matches
echo "Checking CLAUDE.md test count..."
CLAUDE_COUNT=$(grep -oE 'run all [0-9]+ tests' CLAUDE.md | grep -oE '[0-9]+' || true)
if [ -n "$CLAUDE_COUNT" ] && [ -n "$TEST_COUNT" ]; then
    if [ "$CLAUDE_COUNT" = "$TEST_COUNT" ]; then
        record_pass "CLAUDE.md test count matches ($CLAUDE_COUNT)"
    else
        record_fail "CLAUDE.md says $CLAUDE_COUNT tests but $TEST_COUNT actually ran — update CLAUDE.md"
    fi
else
    yellow "Could not verify CLAUDE.md test count"
fi

# 5. README.md test count matches
echo "Checking README.md test count..."
README_COUNT=$(grep -oE 'run all [0-9]+ tests' README.md | grep -oE '[0-9]+' || true)
if [ -n "$README_COUNT" ] && [ -n "$TEST_COUNT" ]; then
    if [ "$README_COUNT" = "$TEST_COUNT" ]; then
        record_pass "README.md test count matches ($README_COUNT)"
    else
        record_fail "README.md says $README_COUNT tests but $TEST_COUNT actually ran — update README.md"
    fi
else
    yellow "Could not verify README.md test count"
fi

# Summary
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [ "$FAIL" -eq 0 ]; then
    green "HEALTHY — $PASS/$((PASS + FAIL)) checks passed"
else
    red "UNHEALTHY — $FAIL check(s) failed ($PASS passed)"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

exit "$FAIL"
