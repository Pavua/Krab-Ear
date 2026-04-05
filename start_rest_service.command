#!/bin/bash
# Запуск REST-сервиса Krab Ear (Phase EAR-1 P0)
# Этот скрипт активирует виртуальное окружение и запускает Flask сервер на порту 5005.

cd "$(dirname "$0")"

# Добавляем путь к модулям
export PYTHONPATH=$PYTHONPATH:$(pwd)/KrabEar

# Проверяем наличие venv
if [ -d ".venv_krab_ear" ]; then
    echo "Starting Krab Ear REST API via .venv_krab_ear..."
    ./.venv_krab_ear/bin/python3 KrabEar/backend/rest_server.py
else
    echo "Error: .venv_krab_ear not found. Please run installation first."
    exit 1
fi
