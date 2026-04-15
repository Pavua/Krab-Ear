#!/bin/zsh
# ------------------------------------------------------------------
# Восстановление прав Accessibility для Krab Ear (.app bundle).
#
# Почему это нужно:
# macOS TCC привязывает разрешения к cdhash бинарника. При ad-hoc signing
# (codesign -s -) каждая пересборка даёт новый cdhash → TCC считает app
# новым → разрешение нужно выдавать заново. Этот скрипт:
#  1) Убивает старые процессы (standalone binary + bundle);
#  2) Чистит stale TCC записи для нашего bundle ID;
#  3) Открывает System Settings → Accessibility;
#  4) Запускает .app bundle чтобы при первом paste показал диалог.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE_ID="com.antigravity.krab-ear"
APP_BUNDLE="$ROOT_DIR/Krab Ear.app"

echo "----------------------------------------------------------------"
echo "Krab Ear: восстановление прав Accessibility"
echo "----------------------------------------------------------------"
echo ""

if [ ! -d "$APP_BUNDLE" ]; then
  echo "❌ Не найден .app bundle: $APP_BUNDLE"
  echo "   Сначала выполните: make build && make sign"
  exit 1
fi

echo "1/4 Убиваем старые процессы..."
pkill -f "Krab Ear.app/Contents/MacOS/KrabEarAgent" 2>/dev/null || true
# standalone binary (устаревший путь запуска) — kill тоже
pkill -f "native/runtime/KrabEarAgent" 2>/dev/null || true
pkill -f "native/KrabEarAgent/.build/release/KrabEarAgent" 2>/dev/null || true

echo "2/4 Чистим stale TCC записи для $BUNDLE_ID..."
# Текущий bundle ID
tccutil reset Accessibility "$BUNDLE_ID" >/dev/null 2>&1 || true
tccutil reset PostEvent "$BUNDLE_ID" >/dev/null 2>&1 || true
tccutil reset ListenEvent "$BUNDLE_ID" >/dev/null 2>&1 || true
tccutil reset AppleEvents "$BUNDLE_ID" >/dev/null 2>&1 || true
# Legacy identifiers из прошлых версий (на всякий случай)
tccutil reset Accessibility com.krabear.agent >/dev/null 2>&1 || true
tccutil reset Accessibility KrabEarAgent >/dev/null 2>&1 || true

echo "3/4 Открываем System Settings → Privacy & Security → Accessibility..."
echo ""
echo "  В открывшемся окне:"
echo "    — Удалите любые старые записи 'Krab Ear.app' и 'KrabEarAgent' кнопкой (−)"
echo "    — НЕ нажимайте '+' вручную — macOS само предложит при запросе"
echo ""
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" || true

echo "4/4 Запускаем Krab Ear.app (через 3 секунды)..."
sleep 3
open "$APP_BUNDLE" --args --show-history

echo ""
echo "----------------------------------------------------------------"
echo "Готово! Дальнейшие шаги:"
echo "  1. Активируйте Right Option → скажите фразу → отпустите"
echo "  2. macOS спросит разрешение Accessibility — нажмите 'Open System Settings'"
echo "  3. Включите тумблер 'Krab Ear.app' (только его, без KrabEarAgent)"
echo "  4. Paste заработает. До следующей пересборки — всё ок."
echo "----------------------------------------------------------------"
