#!/bin/zsh
# Приёмка именно Batch B задач для Antigravity.

set -euo pipefail

ROOT_DIR="/Users/pablito/Antigravity_AGENTS/Krab Ear"
OPENCLAW_DIR="/Users/pablito/Antigravity_AGENTS/Краб"

echo "[1/4] Boundary-check (antigravity)..."
"$ROOT_DIR/Run Agent Boundary Check.command" antigravity

echo "[2/4] Krab Ear release checklist..."
"$ROOT_DIR/Run Release Checklist.command" >/dev/null

echo "[3/4] OpenClaw command registration sanity..."
python3 - <<'PY'
from pathlib import Path
import re

paths = [
    Path("/Users/pablito/Antigravity_AGENTS/Краб/src/handlers/tools.py"),
    Path("/Users/pablito/Antigravity_AGENTS/Краб/src/handlers/commands.py"),
]
cmds = ["callstart", "callstop", "callstatus", "notify", "calllang"]
pattern_map = {c: re.compile(rf"filters\.command\(\"{c}\"", re.MULTILINE) for c in cmds}

hits = {c: 0 for c in cmds}
for path in paths:
    text = path.read_text(encoding="utf-8")
    for c, p in pattern_map.items():
        hits[c] += len(p.findall(text))

bad = {k: v for k, v in hits.items() if v != 1}
if bad:
    raise SystemExit(f"Voice command handlers must be unique, got: {bad}")
print("OK handlers:", hits)
PY

echo "[4/4] Batch B report exists?..."
LATEST_REPORT=$(ls -t "$ROOT_DIR"/docs/reports/antigravity_batch_b_report_*.md 2>/dev/null | head -n 1 || true)
if [[ -z "$LATEST_REPORT" ]]; then
  echo "❌ Не найден antigravity_batch_b_report_*.md"
  exit 1
fi
echo "✅ Found report: $LATEST_REPORT"

echo "✅ Antigravity Batch B acceptance passed"
