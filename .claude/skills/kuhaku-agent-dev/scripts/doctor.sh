#!/usr/bin/env bash
# Quick health check for the kuhaku-agent-cli repo.
# Runs syntax checks and the CLI's own `doctor` command.
#
# Usage (from repo root):
#   bash claude/skills/kuhaku-agent-dev/scripts/doctor.sh
set -euo pipefail

cd "$(dirname "$0")/../../../.."

echo "=== syntax ============================================================"
find src/kuhaku_agent -name '*.py' -print0 \
  | xargs -0 -n1 python3 -m py_compile
echo "all modules compile."

echo
echo "=== uv project state =================================================="
if command -v uv >/dev/null 2>&1; then
  uv lock --check 2>&1 || echo "  (uv.lock out of sync; run 'uv lock' to refresh)"
else
  echo "  uv not installed; skipping lockfile check"
fi

echo
echo "=== runtime check ====================================================="
if [ -f .env ]; then
  uv run kuhaku-agent doctor || true
else
  echo "  .env missing; skipping runtime check (copy env.example to .env first)"
fi
