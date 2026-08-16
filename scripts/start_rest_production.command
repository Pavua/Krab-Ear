#!/bin/bash
cd "$(dirname "$0")/.."
source .venv_krab_ear/bin/activate
export KRAB_EAR_MLX_WHISPER_WORKER="${KRAB_EAR_MLX_WHISPER_WORKER:-1}"
PYTHONPATH=$(pwd)/KrabEar exec gunicorn -c KrabEar/gunicorn_config.py "backend.rest_server:create_app()"
