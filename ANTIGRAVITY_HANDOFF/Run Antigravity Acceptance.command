#!/bin/zsh
# Проверка, что Antigravity-срез принят по базовым критериям.

set -euo pipefail

ROOT_DIR="/Users/pablito/Antigravity_AGENTS/Krab Ear"
GATEWAY_DIR="/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway"

echo "[1/5] Boundary-check (antigravity)..."
"$ROOT_DIR/Run Agent Boundary Check.command" antigravity

echo "[2/5] Swift release build (Krab Ear native)..."
swift build -c release --package-path "$ROOT_DIR/native/KrabEarAgent" >/dev/null

echo "[3/5] Krab Ear release checklist..."
"$ROOT_DIR/Run Release Checklist.command" >/dev/null

echo "[4/5] Gateway tests..."
source "$GATEWAY_DIR/.venv_krab_voice_gateway/bin/activate"
pytest -q "$GATEWAY_DIR/tests" >/dev/null

echo "[5/5] Gateway smoke..."
"$GATEWAY_DIR/scripts/smoke_gateway_api.command" >/dev/null

echo "✅ Antigravity acceptance passed"
