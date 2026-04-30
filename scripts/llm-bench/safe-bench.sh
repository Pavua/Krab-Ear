#!/usr/bin/env bash
# Safe LLM/VL bench wrapper — strict single-model discipline + RAM guard.
#
# Prevents M4 Max OOM reboots when benching large models (>15 GB) by:
# 1. Ejecting ALL LM Studio models BEFORE each bench load
# 2. Killing any orphan mlx_lm/mlx_vlm Python processes
# 3. Checking free RAM ≥ (model_size + 4 GB buffer) before loading
# 4. Restoring production rewriter (qwen3.5-9b@6bit) at end via JIT
#
# Usage:
#   ./safe-bench.sh <bench_type> <model_filter>
#   bench_type: text | vl
#   model_filter: substring of model name (passed to bench script)
#
# Example:
#   ./safe-bench.sh vl Qwen3.6-27B
#   ./safe-bench.sh text Hermes-3
set -euo pipefail

BENCH_TYPE="${1:?Usage: safe-bench.sh text|vl <filter>}"
MODEL_FILTER="${2:-}"
KRAB_EAR="/Users/pablito/Antigravity_AGENTS/Krab Ear"
SCRIPTS="$KRAB_EAR/scripts/llm-bench"
RESULTS_LOG="$HOME/krab-bench-${BENCH_TYPE}.log"

echo "=== safe-bench: ${BENCH_TYPE} / filter='${MODEL_FILTER}' ==="

# Step 1 — eject everything in LM Studio
echo "[1/5] Ejecting all LM Studio models..."
for model in $(/Users/pablito/.lmstudio/bin/lms ps 2>/dev/null | awk 'NR>2 && $1 != "" {print $1}'); do
  /Users/pablito/.lmstudio/bin/lms unload "$model" 2>&1 | tail -1
done

# Step 2 — kill orphan bench processes
echo "[2/5] Killing orphan mlx/bench processes..."
pkill -f "mlx_lm.server" 2>/dev/null || true
pkill -f "mlx_lm.generate" 2>/dev/null || true
pkill -f "vl-bench.py" 2>/dev/null || true
pkill -f "text-bench.py" 2>/dev/null || true
sleep 2

# Step 3 — check free RAM (minimum baseline; per-model checks happen inside bench scripts)
FREE_GB=$(vm_stat | awk '/Pages free/ {f=$3} /Pages inactive/ {i=$3} END {printf "%.1f", (f+i)*16384/1024/1024/1024}')
echo "[3/5] Free RAM: ${FREE_GB} GB"
# Lowered baseline to 8 GB — accommodates small (4-7 GB) models. The bench scripts
# themselves should skip individual models if size+4GB exceeds free RAM at load time.
if (( $(echo "$FREE_GB < 8.0" | bc -l) )); then
  echo "  ⚠️ Free RAM < 8 GB — even smallest models unsafe. Aborting."
  exit 1
fi
if (( $(echo "$FREE_GB < 12.0" | bc -l) )); then
  echo "  ℹ️ Free RAM ${FREE_GB} GB — only small models (≤8 GB) are safe. Larger ones may be skipped."
fi

# Step 4 — run bench
echo "[4/5] Running ${BENCH_TYPE} bench..."
case "$BENCH_TYPE" in
  vl)
    source ~/.venv_vl/bin/activate
    python "$SCRIPTS/vl-bench.py" "$MODEL_FILTER" 2>&1 | tee "$RESULTS_LOG"
    ;;
  text)
    source "$KRAB_EAR/.venv_krab_ear/bin/activate" 2>/dev/null || true
    /opt/homebrew/anaconda3/bin/python3 "$SCRIPTS/text-bench.py" "$MODEL_FILTER" 2>&1 | tee "$RESULTS_LOG"
    ;;
  *)
    echo "  ❌ unknown bench_type: $BENCH_TYPE (use: text | vl)"
    exit 1
    ;;
esac

# Step 5 — production rewriter will JIT-load on first request via Krab Ear backend
echo "[5/5] Bench done. Production rewriter (qwen3.5-9b@6bit) will JIT-load on first transcribe."
echo "      Results: $RESULTS_LOG"
