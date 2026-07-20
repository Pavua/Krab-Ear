#!/bin/bash
# install_gigaam_venv.command
#
# Создаёт изолированный venv для GigaAM v3 (RU STT). Krab Ear main venv
# (Python 3.14, torch 2.11) держим отдельно, чтобы не смешивать зависимости.
# Этот скрипт ставит GigaAM v3 в ~/.venv_krab_ear_gigaam с Python 3.12.
#
# 2026-07-20 (спека docs/superpowers/specs/2026-07-20-gigaam-v3-upgrade-design.md):
# апгрейд v2 → v3. Пакет gigaam==0.1.0 (PyPI) содержит ТОЛЬКО v1/v2 — v3-модели
# есть только в git-исходнике. Новый пакет ослабил torch-пин до torch>=2.6
# (extra [torch]). Ставим с ПИНОВАННОГО коммита (воспроизводимость для DMG-получателей).
# Прод-mode = v3_e2e_rnnt (нативная пунктуация/капитализация/числа; забенчено быстрее v2).
#
# После install: см. memory/reference_gigaam_install_working.md для интеграции в Krab Ear.

set -e

VENV_PATH="$HOME/.venv_krab_ear_gigaam"
PYTHON_BIN="/opt/homebrew/bin/python3.12"
# Пиновано на коммит 2026-07-14 (первый с v3 + multilingual). НЕ floating master.
GIGAAM_REPO="https://github.com/salute-developers/GigaAM.git"
GIGAAM_COMMIT="559d88d6b72541412743929f633a6ae7c9950b85"

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

echo "→ Клонирую GigaAM (git, пиновано на $GIGAAM_COMMIT)…"
SRC_DIR="$VENV_PATH/src/GigaAM"
mkdir -p "$VENV_PATH/src"
if [ ! -d "$SRC_DIR/.git" ]; then
    git clone "$GIGAAM_REPO" "$SRC_DIR"
fi
git -C "$SRC_DIR" fetch --all --quiet
git -C "$SRC_DIR" checkout --quiet "$GIGAAM_COMMIT"

echo "→ Ставлю gigaam v3 из исходника с extra [torch] (torch>=2.6)…"
pip install -e "$SRC_DIR"'[torch]'

echo ""
echo "→ Smoke import + проверка v3-модели в реестре…"
"$VENV_PATH/bin/python" -c "
import gigaam, inspect
print('✓ gigaam загружается')
print('  load_model signature:', inspect.signature(gigaam.load_model))
names = getattr(gigaam, '_MODEL_HASHES', {})
assert 'v3_e2e_rnnt' in names, 'v3_e2e_rnnt отсутствует в реестре — не тот коммит?'
print('  ✓ v3_e2e_rnnt в реестре моделей')
import torch
print('  torch:', torch.__version__, 'mps:', torch.backends.mps.is_available())
"

echo ""
echo "✅ Готово! venv: $VENV_PATH"
echo "Размер: $(du -sh "$VENV_PATH" | awk '{print $1}')"
echo ""
echo "Следующие шаги:"
echo "  1. set_settings { 'stt_gigaam_enabled': true } через IPC"
echo "  2. Запустить smoke transcribe (см. memory/reference_gigaam_install_working.md)"
echo "  3. Создать backend/workers/gigaam_worker.py + adapter (B-3)"
