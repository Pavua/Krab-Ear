#!/bin/zsh
# ------------------------------------------------------------------
# Восстановление прав TCC для Krab Ear (.app bundle).
#
# Почему это нужно:
# macOS TCC привязывает разрешения к cdhash бинарника. При ad-hoc signing
# (codesign -s -) каждая пересборка даёт новый cdhash → TCC считает app
# новым → разрешения нужно выдавать заново. Этот скрипт:
#  1) Убивает старые процессы;
#  2) Чистит stale TCC записи (Accessibility, Microphone, ScreenCapture, ListenEvent);
#  3) Открывает System Settings → Accessibility;
#  4) Выводит инструкции на русском;
#  5) Перезапускает .app bundle.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE_ID="com.antigravity.krab-ear"
APP_BUNDLE="$ROOT_DIR/Krab Ear.app"

echo "----------------------------------------------------------------"
echo "Krab Ear: восстановление прав TCC (Accessibility / Mic / Screen)"
echo "----------------------------------------------------------------"
echo ""

if [ ! -d "$APP_BUNDLE" ]; then
  echo "Не найден .app bundle: $APP_BUNDLE"
  echo "   Сначала выполните: make build && make sign"
  exit 1
fi

echo "1/5 Убиваем старые процессы..."
pkill -9 -f "Krab Ear.app/Contents/MacOS/KrabEarAgent" 2>/dev/null || true
pkill -f "native/runtime/KrabEarAgent" 2>/dev/null || true
pkill -f "native/KrabEarAgent/.build/release/KrabEarAgent" 2>/dev/null || true
sleep 1

echo "2/5 Сбрасываем Accessibility..."
tccutil reset Accessibility "$BUNDLE_ID" >/dev/null 2>&1 || true
tccutil reset Accessibility com.krabear.agent >/dev/null 2>&1 || true
tccutil reset Accessibility KrabEarAgent >/dev/null 2>&1 || true

echo "3/5 Сбрасываем Microphone..."
tccutil reset Microphone "$BUNDLE_ID" >/dev/null 2>&1 || true

echo "4/5 Сбрасываем ScreenCapture и ListenEvent..."
tccutil reset ScreenCapture "$BUNDLE_ID" >/dev/null 2>&1 || true
tccutil reset ListenEvent "$BUNDLE_ID" >/dev/null 2>&1 || true
tccutil reset PostEvent "$BUNDLE_ID" >/dev/null 2>&1 || true
tccutil reset AppleEvents "$BUNDLE_ID" >/dev/null 2>&1 || true

echo ""
echo "----------------------------------------------------------------"
echo "ИНСТРУКЦИЯ: что сделать в System Settings"
echo "----------------------------------------------------------------"
echo ""
echo "  1. Найди Krab Ear в списке Универсального доступа."
echo "  2. Нажми '-' чтобы удалить запись."
echo "  3. Нажми '+' чтобы добавить заново."
echo "  4. Выбери: $APP_BUNDLE"
echo "  5. Toggle должен включиться."
echo "  6. Повтори для Microphone и Screen Recording в соседних pane."
echo ""
echo "----------------------------------------------------------------"

echo "5/5 Открываем System Settings → Privacy & Security → Accessibility..."
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" || true

echo ""
echo "Перезапускаем Krab Ear.app через 3 секунды..."
sleep 3
open "$APP_BUNDLE"

echo ""
echo "----------------------------------------------------------------"
echo "Готово! Дальнейшие шаги:"
echo ""
echo "  1. Активируйте Right Option → скажите фразу → отпустите."
echo "  2. macOS может показать диалог разрешения — подтвердите его."
echo "  3. В System Settings включите тумблер напротив Krab Ear."
echo "  4. Также проверьте Microphone и Screen Recording."
echo "  5. После включения вставка работает без диалогов."
echo ""
echo "  После следующей пересборки агента — запустите скрипт снова."
echo "----------------------------------------------------------------"
