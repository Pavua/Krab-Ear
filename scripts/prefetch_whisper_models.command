#!/bin/bash
# prefetch_whisper_models.command
#
# Скачивает MLX-Whisper модели из HuggingFace в локальный кэш без загрузки на GPU.
# Идемпотентен: повторный запуск пропускает уже скачанные модели (snapshot_download
# сравнивает SHA256 blob-файлов и скипует загрузку при cache hit).
#
# Зачем: первый STT-вызов вызывал "STT модель отсутствует в кэше" warning ×5 при старте.
# После запуска этого скрипта все три модели chain'а предзагружены в ~/.cache/huggingface/.
#
# Использование:
#   ./scripts/prefetch_whisper_models.command          # download all models
#   ./scripts/prefetch_whisper_models.command --dry-run # print info without downloading
#
# Опции:
#   --dry-run   Показать список моделей и статус кэша без загрузки
#   HF_TOKEN    (env var, опционально) токен для gated-моделей на HuggingFace

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_PATH="$PROJECT_ROOT/.venv_krab_ear"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
fi

# Models in fallback chain order (same as AudioEngine STT chain)
MODELS=(
    "mlx-community/whisper-large-v3-mlx"
    "mlx-community/whisper-large-v3-turbo"
    "mlx-community/whisper-medium-mlx"
)

echo "=== Krab Ear — prefetch_whisper_models ==="
echo "Project: $PROJECT_ROOT"
echo ""

# Activate virtualenv
if [ ! -d "$VENV_PATH" ]; then
    echo "❌ Virtualenv не найден: $VENV_PATH"
    echo "Сначала запусти: Start Krab Ear.command"
    exit 1
fi

source "$VENV_PATH/bin/activate"

# Verify huggingface_hub is available
if ! python3 -c "import huggingface_hub" 2>/dev/null; then
    echo "❌ huggingface_hub не установлен в venv."
    echo "Запусти: source $VENV_PATH/bin/activate && pip install huggingface-hub"
    exit 1
fi

# HF_TOKEN (optional — only needed for gated models)
HF_TOKEN_ARG=""
if [ -n "${HF_TOKEN:-}" ]; then
    HF_TOKEN_ARG="--token $HF_TOKEN"
    echo "ℹ️  HF_TOKEN задан — gated-модели будут доступны."
else
    echo "ℹ️  HF_TOKEN не задан — пропустим gated-модели если встретятся."
fi
echo ""

# Determine HF cache root
HF_CACHE_DIR="${HF_HOME:-$HOME/.cache/huggingface}/hub"

# Helper: check if a model is fully cached
is_cached() {
    local repo_id="$1"
    # HuggingFace stores repos as models--<org>--<name>
    local cache_name
    cache_name="models--$(echo "$repo_id" | tr '/' '--')"
    local snapshots_dir="$HF_CACHE_DIR/$cache_name/snapshots"
    if [ -d "$snapshots_dir" ] && [ -n "$(ls -A "$snapshots_dir" 2>/dev/null)" ]; then
        echo "HIT"
    else
        echo "MISS"
    fi
}

if [ "$DRY_RUN" -eq 1 ]; then
    echo "--- DRY-RUN: no downloads will be performed ---"
    echo ""
    echo "Models in STT fallback chain:"
    echo ""
    for model in "${MODELS[@]}"; do
        status=$(is_cached "$model")
        if [ "$status" = "HIT" ]; then
            echo "  [CACHED] $model"
        else
            echo "  [MISSING] $model  →  would download from https://huggingface.co/$model"
        fi
    done
    echo ""
    echo "Cache dir: $HF_CACHE_DIR"
    echo ""
    echo "To prefetch: run without --dry-run flag."
    exit 0
fi

# Download models
DOWNLOADED=0
SKIPPED=0
FAILED=0

for model in "${MODELS[@]}"; do
    echo "--- Model: $model ---"
    status=$(is_cached "$model")

    if [ "$status" = "HIT" ]; then
        echo "  ✅ Cache hit — skipping download."
        SKIPPED=$((SKIPPED + 1))
        echo ""
        continue
    fi

    echo "  ⬇️  Cache miss — downloading..."

    # Use huggingface_hub snapshot_download; ignore_patterns to skip large audio test files
    if python3 - <<PYEOF
import sys
try:
    from huggingface_hub import snapshot_download
    token = None
    import os
    hf_token = os.environ.get("HF_TOKEN")
    snapshot_download(
        repo_id="${model}",
        repo_type="model",
        token=hf_token if hf_token else None,
        ignore_patterns=["*.bin", "*.pt", "*.ot"],  # skip non-MLX weights
    )
    print("  ✅ Downloaded successfully.")
except Exception as e:
    print(f"  ❌ Failed: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
    then
        DOWNLOADED=$((DOWNLOADED + 1))
    else
        echo "  ⚠️  Download failed — model will be fetched on first STT call."
        FAILED=$((FAILED + 1))
    fi
    echo ""
done

echo "=== Summary ==="
echo "  Downloaded : $DOWNLOADED"
echo "  Cached (skipped): $SKIPPED"
echo "  Failed     : $FAILED"
echo ""

if [ "$FAILED" -gt 0 ]; then
    echo "⚠️  $FAILED model(s) failed. Check network / HF_TOKEN and retry."
    exit 1
else
    echo "✅ All models ready. Cold-cache warnings on next startup will be eliminated."
fi
