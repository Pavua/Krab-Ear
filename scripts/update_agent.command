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
APP_BUNDLE="$ROOT_DIR/Krab Ear.app"
APP_BUNDLE_BIN="$APP_BUNDLE/Contents/MacOS/KrabEarAgent"
PACKAGE_DIR="$ROOT_DIR/native/KrabEarAgent"
BUILD_BIN="$PACKAGE_DIR/.build/release/KrabEarAgent"
BUNDLE_ID="com.antigravity.krab-ear"
# Матчим оба пути: .app bundle (рекомендуемый LaunchServices-путь) и legacy standalone runtime.
AGENT_PATTERN_BUNDLE="$APP_BUNDLE_BIN"
AGENT_PATTERN_LEGACY="$RUNTIME_BIN --project-root $ROOT_DIR"
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
require_cmd codesign
require_cmd open
[ -x "$VENV_PY" ] || preflight_fail "не найден python venv: $VENV_PY"
[ -d "$ROOT_DIR/native/runtime" ] || mkdir -p "$ROOT_DIR/native/runtime"
[ -w "$ROOT_DIR/native/runtime" ] || preflight_fail "нет прав записи в $ROOT_DIR/native/runtime"
[ -w "$ROOT_DIR" ] || preflight_fail "нет прав записи в $ROOT_DIR"
[ -d "$APP_BUNDLE" ] || preflight_fail "не найден .app bundle: $APP_BUNDLE"
[ -w "$APP_BUNDLE/Contents/MacOS" ] || preflight_fail "нет прав записи в $APP_BUNDLE/Contents/MacOS"
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

# Останавливаем ранее запущенный агент (как .app bundle, так и legacy standalone).
# Без этого pkill после rebuild обновление бинаря приведёт к сбою TCC cdhash
# и "висящему" экземпляру агента.
pkill -f "$AGENT_PATTERN_BUNDLE" 2>/dev/null || true
pkill -f "$AGENT_PATTERN_LEGACY" 2>/dev/null || true
# Небольшая пауза, чтобы процессы успели завершиться до копирования бинаря.
for _ in {1..20}; do
  if ! pgrep -f "$AGENT_PATTERN_BUNDLE" >/dev/null 2>&1 \
     && ! pgrep -f "$AGENT_PATTERN_LEGACY" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

echo "Собираю нативный агент (Swift release)"
swift build -c release --package-path "$PACKAGE_DIR"

[ -x "$BUILD_BIN" ] || preflight_fail "build artifact не найден: $BUILD_BIN"

# Синхронизируем свежесобранный бинарь в обе точки доставки:
#   * native/runtime/KrabEarAgent — legacy путь (dev-сценарии, smoke-тесты);
#   * Krab Ear.app/Contents/MacOS/KrabEarAgent — production, запускаемый через LaunchServices.
cp -f "$BUILD_BIN" "$RUNTIME_BIN"
chmod +x "$RUNTIME_BIN"
cp -f "$BUILD_BIN" "$APP_BUNDLE_BIN"
chmod +x "$APP_BUNDLE_BIN"

# Выбираем signing identity:
#   Если в login Keychain есть self-signed identity "Krab Ear Dev Local",
#   используем её — cdhash стабилен → TCC не сбрасывает permissions при rebuild.
#   Fallback: ad-hoc (-s -) — backward-compatible, но TCC revoke при каждой сборке.
#   Для создания identity: ./scripts/create_local_signing_identity.command
LOCAL_IDENTITY="Krab Ear Dev Local"
if security find-identity -v -p codesigning 2>/dev/null | grep -q "$LOCAL_IDENTITY"; then
  SIGN_ID="$LOCAL_IDENTITY"
  echo "Подписываю с identity: \"$SIGN_ID\" (stable cdhash, TCC-safe)"
else
  SIGN_ID="-"
  echo "Подписываю ad-hoc (TCC revoke при rebuild; запустите scripts/create_local_signing_identity.command)"
fi

codesign --force --sign "$SIGN_ID" --timestamp=none --identifier "$BUNDLE_ID" "$RUNTIME_BIN" >/dev/null 2>&1 || true
codesign --force --sign "$SIGN_ID" "$APP_BUNDLE" >/dev/null 2>&1 || true

# Запускаем через LaunchServices (`open`) — macOS создаёт управляемый
# application.com.antigravity.krab-ear.* job, тот же самый, что появляется при
# клике иконки в Dock. Это гарантирует отсутствие дубликата в Dock и
# отсутствие orphan-процесса `com.apple.xpc.launchd.unmanaged.KrabEarAgent.*`,
# который раньше появлялся при backgrounded-exec из shell.
open "$APP_BUNDLE"

started=0
for _ in {1..60}; do
  if pgrep -f "$AGENT_PATTERN_BUNDLE" >/dev/null 2>&1; then
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
