#!/bin/zsh
# ------------------------------------------------------------------
# Восстановление прав Accessibility для Krab Ear Agent:
# 1) готовит runtime-бинарь;
# 2) открывает системный раздел Accessibility;
# 3) показывает бинарь в Finder для добавления через кнопку "+".
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PACKAGE_DIR="$ROOT_DIR/native/KrabEarAgent"
BUILD_BIN="$PACKAGE_DIR/.build/release/KrabEarAgent"
RUNTIME_DIR="$ROOT_DIR/native/runtime"
RUNTIME_BIN="$RUNTIME_DIR/KrabEarAgent"

echo "----------------------------------------------------------------"
echo "Krab Ear: восстановление прав Accessibility"
echo "----------------------------------------------------------------"

mkdir -p "$RUNTIME_DIR"

if [ ! -x "$BUILD_BIN" ] || find "$PACKAGE_DIR/Sources" "$PACKAGE_DIR/Package.swift" -type f -newer "$BUILD_BIN" -print -quit | grep -q .; then
  echo "Собираю нативный агент..."
  swift build -c release --package-path "$PACKAGE_DIR"
fi

cp "$BUILD_BIN" "$RUNTIME_BIN"
chmod +x "$RUNTIME_BIN"
codesign --force --sign - --timestamp=none --identifier com.krabear.agent "$RUNTIME_BIN" >/dev/null 2>&1 || true

# Чистим старые/битые записи разрешений, чтобы не залипали ghost-энтри.
tccutil reset Accessibility com.krabear.agent >/dev/null 2>&1 || true
tccutil reset Accessibility KrabEarAgent >/dev/null 2>&1 || true
tccutil reset ListenEvent com.krabear.agent >/dev/null 2>&1 || true
tccutil reset ListenEvent KrabEarAgent >/dev/null 2>&1 || true

echo
echo "1) Сейчас откроются разделы Accessibility и Input Monitoring."
echo "2) Добавьте через '+' файл:"
echo "   $RUNTIME_BIN"
echo "3) Включите тумблер доступа (в обоих разделах, если система покажет)."
echo

open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
open "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
open -R "$RUNTIME_BIN"

echo "Готово. После выдачи прав перезапустите Krab Ear."
