#!/bin/zsh
# ------------------------------------------------------------------
# Явное обновление нативного агента Krab Ear:
# 1) форс-сборка Swift;
# 2) синхронизация runtime-бинаря;
# 3) перезапуск агента.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_BIN="$ROOT_DIR/native/runtime/KrabEarAgent"
AGENT_PATTERN="$RUNTIME_BIN --project-root $ROOT_DIR"
VENV_PY="$ROOT_DIR/.venv_krab_ear/bin/python"
SETTINGS_PATH="$HOME/Library/Application Support/KrabEar/settings.json"

preflight_fail() {
  echo "Ошибка preflight: $1"
  exit 1
}

require_cmd() {
  local name="$1"
  command -v "$name" >/dev/null 2>&1 || preflight_fail "не найдено: $name"
}

check_free_space_mb() {
  local path="$1"
  local min_mb="$2"
  local free_kb
  free_kb="$(/bin/df -k "$path" | /usr/bin/awk 'NR==2 {print $4}')"
  [ -n "$free_kb" ] || preflight_fail "не удалось определить свободное место"
  local free_mb=$((free_kb / 1024))
  if [ "$free_mb" -lt "$min_mb" ]; then
    preflight_fail "недостаточно места: ${free_mb}MB (нужно >= ${min_mb}MB)"
  fi
}

require_cmd swift
require_cmd pgrep
[ -x "$VENV_PY" ] || preflight_fail "не найден python venv: $VENV_PY"
[ -d "$ROOT_DIR/native/runtime" ] || mkdir -p "$ROOT_DIR/native/runtime"
[ -w "$ROOT_DIR/native/runtime" ] || preflight_fail "нет прав записи в $ROOT_DIR/native/runtime"
[ -w "$ROOT_DIR" ] || preflight_fail "нет прав записи в $ROOT_DIR"
check_free_space_mb "$ROOT_DIR" 200

UPDATE_CHANNEL="stable"
if [ -f "$SETTINGS_PATH" ]; then
  UPDATE_CHANNEL="$("$VENV_PY" - <<'PY' "$SETTINGS_PATH"
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
try:
    payload=json.loads(p.read_text(encoding='utf-8'))
except Exception:
    print("stable")
    raise SystemExit(0)
value=str(payload.get("update_channel","stable")).strip().lower()
print(value if value in {"stable","beta"} else "stable")
PY
)"
fi

echo "Обновляю Krab Ear Agent... (channel: $UPDATE_CHANNEL)"
"$ROOT_DIR/scripts/start_agent.command" --force-rebuild --launched-by-launchd >/dev/null 2>&1 &

started=0
for _ in {1..60}; do
  if pgrep -f "$AGENT_PATTERN" >/dev/null 2>&1; then
    started=1
    break
  fi
  sleep 0.25
done

if [ "$started" -eq 1 ]; then
  echo "Готово. Агент обновлён и запущен."
else
  echo "Предупреждение: агент ещё запускается. Проверьте лог: ~/Library/Application Support/KrabEar/agent.log"
fi
