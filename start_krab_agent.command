#!/bin/bash
# Запуск Krab Ear Agent.
#
# Раньше скрипт запускал legacy-бинарник `.build/release/KrabEarAgent` напрямую
# с жёстким абсолютным путём и печатал «запущен» без проверки (ревью ChatGPT
# G2-Krab-Ear-02b1, KE02B1-04/10). Канонический вход — `scripts/start_agent.command`
# (он открывает Krab Ear.app через `open`, один экземпляр под SingleInstanceGuard),
# поэтому здесь только делегирование: аргументы и код возврата передаются как есть.
# Семантика: `open -W` ждёт завершения приложения, то есть скрипт больше не
# «возвращается сразу в фон»; для фонового запуска вызывающий использует `&`.
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
LAUNCHER="${ROOT_DIR}/scripts/start_agent.command"

if [[ ! -f "$LAUNCHER" ]]; then
    echo "❌ Канонический launcher не найден: $LAUNCHER" >&2
    exit 1
fi

exec /bin/zsh "$LAUNCHER" "$@"
