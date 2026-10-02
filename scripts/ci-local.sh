#!/usr/bin/env bash
# Run the CI jobs the way GitHub will, against a pristine checkout.
#
# The point of the pristine copy: CI gets only what git tracks. Running the jobs in
# your working tree passes even when a file CI needs is gitignored or never added --
# which is the most common way a green local run turns into a red first build.
#
#   scripts/ci-local.sh           lint + unit + harness-replay  (no secrets needed)
#   scripts/ci-local.sh --live    also the live-smoke job       (needs .env)
#
# Not covered here, because they only exist on GitHub: astral-sh/setup-uv, the
# dependency cache, repository secrets, the nightly schedule, and artifact upload.
# Push a branch to exercise those.

set -uo pipefail
cd "$(dirname "$0")/.."
SRC="$PWD"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FAILED=0
run() {
  local name="$1"; shift
  printf '\n\033[1m== %s ==\033[0m\n' "$name"
  if "$@"; then
    printf '\033[32mPASS\033[0m %s\n' "$name"
  else
    printf '\033[31mFAIL\033[0m %s (exit %d)\n' "$name" "$?"
    FAILED=1
  fi
}

# Exactly what a fresh clone would contain: tracked files, plus untracked ones that
# are not ignored (those become tracked the moment you commit).
echo "Staging a pristine checkout in $WORK"
git -C "$SRC" ls-files --cached --others --exclude-standard -z \
  | while IFS= read -r -d '' f; do
      mkdir -p "$WORK/$(dirname "$f")"
      cp "$SRC/$f" "$WORK/$f"
    done

cd "$WORK"
echo "$(find . -type f | wc -l) files"

# Mirrors every job's first step. --locked asserts the lockfile still matches
# pyproject.toml; --frozen would install happily from a stale one.
run "uv sync --locked"            uv sync --locked

run "lint: ruff"                  uv run ruff check .
run "lint: no hardcoded nouns"    uv run pytest tests/test_no_hardcoded_nouns.py -q
run "unit"                        uv run pytest tests/unit -q
run "harness: the task set"       uv run python -m harness.runner
run "harness: replay recorded run" uv run pytest tests/test_prompt_behaviour.py -q

# The replay test skips itself when no cassette is committed. A skip is not a pass,
# and a silently-skipped regression net is worse than none.
if uv run pytest tests/test_prompt_behaviour.py -q 2>&1 | grep -qi "skipped"; then
  printf '\033[31mFAIL\033[0m cassettes are not committed, so the replay test skipped in CI\n'
  FAILED=1
fi

if [ "${1:-}" = "--live" ]; then
  cp "$SRC/.env" "$WORK/.env" 2>/dev/null || true
  run "live-smoke: drift check"   uv run python scripts/discover.py --check-drift
fi

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf '\033[32mAll jobs passed on a pristine checkout.\033[0m\n'
else
  printf '\033[31mSomething failed. CI would be red.\033[0m\n'
fi
exit "$FAILED"
