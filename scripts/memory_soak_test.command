#!/bin/zsh
# Krab Ear memory soak test — IDLE drift only: captures RSS/VSZ snapshots every 10 cycles
# while sleeping between them and reports growth.
#
# 🔴 ЧЕСТНО: скрипт НЕ отправляет запросов транскрипции (раньше заголовок обещал «N transcribe
# IPC calls», а цикл только спал). Для реального сигнала об утечке запускайте его ВО ВРЕМЯ
# диктовки/нагрузки либо используйте нагрузочный тест с настоящим STT.

set -e

CYCLES="${CYCLES:-100}"
INTERVAL="${INTERVAL:-2}"
OUTPUT="${OUTPUT:-soak-results.csv}"

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Krab Ear memory soak test ==="
echo "Cycles: $CYCLES"
echo "Interval: ${INTERVAL}s"
echo "NOTICE: NO transcription requests are sent — this run measures IDLE memory drift only (sleep between snapshots). For a real leak signal run it DURING dictation."
echo ""

# Pre-snapshot
python3 "$ROOT_DIR/scripts/memory_baseline.py" --once --output "$OUTPUT"

# Run cycles
for i in {1..$CYCLES}; do
    if (( i % 10 == 0 )); then
        echo "Cycle $i/$CYCLES — taking memory snapshot"
        python3 "$ROOT_DIR/scripts/memory_baseline.py" --once --output "$OUTPUT"
    fi
    sleep "$INTERVAL"
done

# Post-snapshot
python3 "$ROOT_DIR/scripts/memory_baseline.py" --once --output "$OUTPUT"

echo ""
echo "=== Soak complete ==="
echo "Results in $OUTPUT"
echo "Open with: column -t -s , $OUTPUT | head -20"
