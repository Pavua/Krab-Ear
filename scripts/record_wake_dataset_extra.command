#!/bin/bash
# Двойной клик: ДОПОЛНИТЕЛЬНАЯ запись для wake-word «Краб» (короткое «Краб», созвучные слова, команды без слова).
# Пишет в тот же каталог, что и основной набор; уже записанное не трогает. Ничего никуда не загружает.
cd "$(dirname "$0")/.." || { echo "Не удалось перейти в корень репозитория"; exit 1; }
PY=".venv_krab_ear/bin/python"
if [ ! -x "$PY" ]; then
  # В git worktree своего venv нет: он лежит в основном каталоге репозитория.
  MAIN_ROOT="$(cd "$(git rev-parse --git-common-dir)/.." 2>/dev/null && pwd)"
  [ -x "$MAIN_ROOT/.venv_krab_ear/bin/python" ] && PY="$MAIN_ROOT/.venv_krab_ear/bin/python"
fi
if [ ! -x "$PY" ]; then
  echo "Нет $PY — создайте venv проекта (см. Start Krab Ear.command)"; read -r -p "Enter для выхода"; exit 1
fi
"$PY" scripts/record_wake_dataset.py --set extra "$@"
read -r -p "Готово. Enter для выхода"
