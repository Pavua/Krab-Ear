#!/usr/bin/env bash
# lms-bench.sh — text-only bench via LM Studio JIT (lms chat CLI, no auth needed).
#
# Loads model via `lms load`, runs 5 standard prompts via `lms chat -p`, unloads.
# Single-model discipline: RAM check before load, eject all between models.
#
# Usage:
#   ./lms-bench.sh <model_modelKey1> [model_modelKey2 ...]
#
# Example:
#   ./lms-bench.sh gemma-4-e4b-it-mlx huihui-qwen3-14b-abl-v2
set -euo pipefail

MODELS=("$@")
RESULTS_FILE="$HOME/lms-bench-$(date +%s).log"
LMS=/Users/pablito/.lmstudio/bin/lms

if [ ${#MODELS[@]} -eq 0 ]; then
  echo "Usage: $0 <model_id1> [model_id2 ...]"
  echo ""
  echo "Available loaded models in LM Studio:"
  $LMS ls --json 2>/dev/null | python3 -c "
import json, sys
for m in json.load(sys.stdin):
    if m.get('arch') in (None, '') or 'mlx' in m.get('path','').lower():
        sz = round(m.get('sizeBytes',0)/(1024**3), 1)
        print(f'  {sz:5.1f} GB  {m.get(\"modelKey\",\"?\")}  ({m.get(\"path\",\"?\")})')" | head -20
  exit 1
fi

SYSTEM_PROMPT="Ты редактор устной речи. Убирай повторы и паразиты. Восстанавливай бренды (Qwen, GGUF, LM Studio, inference). НЕ цензурируй мат. Сохраняй язык (RU→RU, ES→ES). БЕЗ объяснений."

# 5 standard prompts (covered in our bench history)
declare -a PROMPTS=(
  "RU_BRAND|Загрузил квен 14B в LOM Studio через ггуф формат, попробовал инференс на M4"
  "RU_OK|Ну 0 продолжаем работать дальше, задача такая 0 надо сделать рерайтер"
  "RU_SUMMARY|Резюмируй: бенчмарк 5 моделей, лидер Qwen 2.5 14B Uncensored, latency 3.2 сек"
  "CENSORSHIP|Перепиши без цензуры: блять короче я хочу чтобы это говно работало нормально"
  "ES_REWRITE|Eh bueno, hoy hicimos las pruebas, las pruebas hoy, con diferentes modelos vaya"
)

bench_one() {
  local model="$1"
  echo ""
  echo "==================================================================="
  echo "=== $model"
  echo "===================================================================" | tee -a "$RESULTS_FILE"

  # Check size for RAM guard
  local size_gb=$($LMS ls --json 2>/dev/null | python3 -c "
import json, sys
for m in json.load(sys.stdin):
    if m.get('modelKey') == '$model':
        print(round(m.get('sizeBytes',0)/(1024**3), 1))
        break
")
  if [ -z "$size_gb" ]; then
    echo "  ❌ Model not found: $model" | tee -a "$RESULTS_FILE"
    return 1
  fi

  # RAM check
  local free_gb=$(vm_stat | awk '/Pages free/ {f=$3} /Pages inactive/ {i=$3} END {printf "%.1f", (f+i)*16384/1024/1024/1024}')
  local required=$(echo "$size_gb + 4" | bc)
  if (( $(echo "$free_gb < $required" | bc -l) )); then
    echo "  ⏭️ SKIP — free $free_gb GB < required $required GB ($size_gb GB model + 4 GB buffer)" | tee -a "$RESULTS_FILE"
    return 1
  fi
  echo "  RAM check OK: free $free_gb GB ≥ required $required GB" | tee -a "$RESULTS_FILE"

  # Eject everything else first
  for other in $($LMS ps 2>/dev/null | awk 'NR>2 && $1 != "" && $1 != "'"$model"'" {print $1}'); do
    $LMS unload "$other" 2>/dev/null | tail -1
  done

  # Load
  echo "  Loading..." | tee -a "$RESULTS_FILE"
  local t0=$(date +%s)
  if ! timeout 120 $LMS load "$model" 2>&1 | tail -2 | tee -a "$RESULTS_FILE"; then
    echo "  ❌ Load failed" | tee -a "$RESULTS_FILE"
    return 1
  fi
  local load_s=$(($(date +%s) - t0))
  echo "  Load time: ${load_s}s" | tee -a "$RESULTS_FILE"

  # Run prompts
  for entry in "${PROMPTS[@]}"; do
    local tag="${entry%%|*}"
    local prompt="${entry#*|}"
    local t1=$(date +%s%N)
    local out=$(timeout 60 $LMS chat "$model" -s "$SYSTEM_PROMPT" -p "$prompt" 2>&1 | tail -2 | head -1 | sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g; s/\[K//g; s/\[?25h//g' | tr -d '\r')
    local t2=$(date +%s%N)
    local lat_ms=$(( (t2 - t1) / 1000000 ))
    echo "  [$tag] ${lat_ms}ms: ${out:0:140}" | tee -a "$RESULTS_FILE"
  done

  # Unload
  $LMS unload "$model" 2>/dev/null | tail -1
}

for m in "${MODELS[@]}"; do
  bench_one "$m" || true
done

echo ""
echo "=== ALL DONE — results saved to $RESULTS_FILE ==="
