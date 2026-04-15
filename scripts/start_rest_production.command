#!/bin/bash
cd "$(dirname "$0")/.."
source .venv_krab_ear/bin/activate
PYTHONPATH=$(pwd)/KrabEar exec gunicorn -c KrabEar/gunicorn_config.py "backend.rest_server:create_app()"
