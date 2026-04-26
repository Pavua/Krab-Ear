#!/bin/bash
# install_gigaam_venv.command
#
# Создаёт изолированный venv для GigaAM-RNNT v2 (RU STT, ~2.5× меньше WER чем whisper-large-v3).
# Krab Ear main venv (Python 3.14, torch 2.11) НЕ совместим с pin'ами GigaAM 0.1.0.
# Этот скрипт ставит GigaAM в ~/.venv_krab_ear_gigaam с Python 3.12 + torch 2.5.1.
#
# После install: см. memory/reference_gigaam_install_working.md для интеграции в Krab Ear.

set -e

VENV_PATH="$HOME/.venv_krab_ear_gigaam"
PYTHON_BIN="/opt/homebrew/bin/python3.12"

echo "=== GigaAM venv installer ==="
echo "Target: $VENV_PATH"
echo "Python: $PYTHON_BIN"
echo ""

if [ ! -x "$PYTHON_BIN" ]; then
    echo "❌ Python 3.12 не найден по пути $PYTHON_BIN"
    echo "Установи: brew install python@3.12"
    exit 1
fi

if [ -d "$VENV_PATH" ]; then
    echo "⚠️  $VENV_PATH уже существует."
    read -p "Удалить и пересоздать? [y/N] " yn
    if [[ "$yn" =~ ^[Yy]$ ]]; then
        rm -rf "$VENV_PATH"
    else
        echo "Прерван по запросу пользователя."
        exit 0
    fi
fi

echo ""
echo "→ Создаю venv…"
"$PYTHON_BIN" -m venv "$VENV_PATH"

echo "→ Активирую и обновляю pip…"
# shellcheck disable=SC1091
source "$VENV_PATH/bin/activate"
pip install --upgrade pip

echo "→ Ставлю torch 2.5.1 + onnxruntime 1.23.0 (для совместимости с GigaAM)…"
pip install "torch==2.5.1" "torchaudio==2.5.1" "onnxruntime==1.23.0" "onnx==1.19.0"

echo "→ Ставлю gigaam (downgrade'нет onnx/onnxruntime автоматически)…"
pip install gigaam

echo ""
echo "→ Smoke import…"
"$VENV_PATH/bin/python" -c "
import gigaam, inspect
print('✓ gigaam загружается')
print('  load_model signature:', inspect.signature(gigaam.load_model))
print('  attrs:', [x for x in dir(gigaam) if not x.startswith('_')][:10])
"

echo ""
echo "✅ Готово! venv: $VENV_PATH"
echo "Размер: $(du -sh "$VENV_PATH" | awk '{print $1}')"
echo ""
echo "Следующие шаги:"
echo "  1. set_settings { 'stt_gigaam_enabled': true } через IPC"
echo "  2. Запустить smoke transcribe (см. memory/reference_gigaam_install_working.md)"
echo "  3. Создать backend/workers/gigaam_worker.py + adapter (B-3)"
