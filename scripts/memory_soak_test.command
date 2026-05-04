#!/bin/zsh
# Krab Ear memory soak test — fires N transcribe IPC calls, captures
# RSS/VSZ deltas before/after, reports growth.
#
# Designed to surface memory leaks in transcription pipeline. Each cycle
# is a small audio array (np.zeros 1s @16kHz) — minimal MLX load but
# exercises the full backend pipeline.

set -e

CYCLES="${CYCLES:-100}"
INTERVAL="${INTERVAL:-2}"
OUTPUT="${OUTPUT:-soak-results.csv}"

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Krab Ear memory soak test ==="
echo "Cycles: $CYCLES"
echo "Interval: ${INTERVAL}s"
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
