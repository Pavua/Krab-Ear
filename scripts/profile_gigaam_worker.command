#!/bin/zsh
# Profile gigaam_worker memory growth.
# Sets KRAB_EAR_TRACE_GIGAAM_MEM=1 + restarts backend, runs N transcribe IPC calls,
# captures memory growth deltas via memory_baseline.py snapshots.
#
# Usage:
#   chmod +x scripts/profile_gigaam_worker.command
#   CYCLES=50 OUTPUT=gigaam-mem-profile.csv scripts/profile_gigaam_worker.command
#
# After profiling, disable tracing:
#   launchctl unsetenv KRAB_EAR_TRACE_GIGAAM_MEM
#
# Results appear as rows in OUTPUT csv. View with:
#   column -t -s , "$OUTPUT"
#
# See docs/audit/gigaam-worker-memory-2026-05-05.md for hypothesis and analysis guide.

set -e
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CYCLES="${CYCLES:-50}"
OUTPUT="${OUTPUT:-gigaam-mem-profile.csv}"

echo "=== GigaAM worker memory profiling ==="
echo "Root dir : $ROOT_DIR"
echo "Cycles   : $CYCLES"
echo "Output   : $OUTPUT"
echo ""

# Check that psutil is available for memory_baseline.py
if ! "$ROOT_DIR/.venv_krab_ear/bin/python" -c "import psutil" 2>/dev/null; then
    echo "ERROR: psutil not found in main venv."
    echo "Install: source .venv_krab_ear/bin/activate && pip install psutil"
    exit 1
fi

PYTHON="$ROOT_DIR/.venv_krab_ear/bin/python"
BASELINE="$ROOT_DIR/scripts/memory_baseline.py"

if [[ ! -f "$BASELINE" ]]; then
    echo "ERROR: memory_baseline.py not found at $BASELINE"
    exit 1
fi

echo "1. Setting KRAB_EAR_TRACE_GIGAAM_MEM=1 in launchctl env..."
launchctl setenv KRAB_EAR_TRACE_GIGAAM_MEM 1
echo "   Done. Workers spawned after this point will enable tracemalloc."

echo ""
echo "2. Restart backend to pick up env (requires launchd-managed backend)..."
if launchctl kickstart -k gui/501/ai.krab.ear.backend 2>/dev/null; then
    echo "   Backend restarted via launchctl."
    echo "   Waiting 5s for gigaam_worker to load model..."
    sleep 5
else
    echo "   WARNING: launchctl kickstart failed (backend may not be launchd-managed)."
    echo "   If running backend manually, restart it with KRAB_EAR_TRACE_GIGAAM_MEM=1 set."
    echo "   Continuing with current process state..."
fi

echo ""
echo "3. Baseline snapshot (before cycles)..."
"$PYTHON" "$BASELINE" --once --output "$OUTPUT"

echo ""
echo "4. Running $CYCLES cycles (snapshot every 10)..."
for i in $(seq 1 "$CYCLES"); do
    if (( i % 10 == 0 )); then
        echo "   Cycle $i — taking snapshot..."
        "$PYTHON" "$BASELINE" --once --output "$OUTPUT"
    fi
    sleep 1
done

echo ""
echo "5. Final snapshot (after cycles)..."
"$PYTHON" "$BASELINE" --once --output "$OUTPUT"

echo ""
echo "=== Profiling complete ==="
echo ""
echo "Results file: $OUTPUT"
echo "View with  : column -t -s , \"$OUTPUT\""
echo ""
echo "To disable tracing: launchctl unsetenv KRAB_EAR_TRACE_GIGAAM_MEM"
echo ""
echo "Analysis guide: docs/audit/gigaam-worker-memory-2026-05-05.md"
echo ""
echo "Look for:"
echo "  - gigaam_worker rss_mb trend across snapshots"
echo "  - If RSS grows linearly → H2 (buffer accumulation, add gc.collect())"
echo "  - If RSS spikes then stabilizes → H1 (MPS pool, add torch.mps.empty_cache())"
echo "  - Worker stderr (in ~/Library/Logs/KrabEar/) for [mem] and [tmalloc] lines"
