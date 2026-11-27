#!/bin/bash
# Запуск REST-сервера Krab Ear под gunicorn (production-режим).
#
# Ревью ChatGPT G2-Krab-Ear-02b1, KE02B1-09: раньше не было `set -e`, результат
# `cd` и `source` не проверялся — при отсутствии venv скрипт молча шёл дальше и
# запускал первый попавшийся `gunicorn` из PATH с относительными путями.
# Здесь: fail-closed на каталог и venv, абсолютные пути, явная ошибка.
# (`-u` не включаем: `activate` в разных версиях venv ссылается на несуществующие
# переменные.)
set -eo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd -P)" || {
    echo "❌ Не удалось определить корень репозитория" >&2
    exit 1
}
cd "$ROOT_DIR" || exit 1

VENV_ACTIVATE="${ROOT_DIR}/.venv_krab_ear/bin/activate"
if [[ ! -f "$VENV_ACTIVATE" ]]; then
    echo "❌ venv не найден: $VENV_ACTIVATE" >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$VENV_ACTIVATE"

export KRAB_EAR_MLX_WHISPER_WORKER="${KRAB_EAR_MLX_WHISPER_WORKER:-1}"
PYTHONPATH="${ROOT_DIR}/KrabEar" exec gunicorn -c "${ROOT_DIR}/KrabEar/gunicorn_config.py" "backend.rest_server:create_app()"
