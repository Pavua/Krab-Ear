#!/bin/zsh
# ------------------------------------------------------------------
# Восстановление прав TCC для Krab Ear (.app bundle).
#
# Почему это нужно:
# macOS TCC привязывает разрешения к cdhash бинарника. При ad-hoc signing
# (codesign -s -) каждая пересборка даёт новый cdhash → TCC считает app
# новым → разрешения нужно выдавать заново. Этот скрипт:
#  1) Останавливает старые процессы (SIGTERM, не SIGKILL; не под активной записью);
#  2) Чистит stale TCC записи (Accessibility, Microphone, ScreenCapture, ListenEvent);
#  3) Открывает System Settings → Accessibility;
#  4) Выводит инструкции на русском;
#  5) Перезапускает .app bundle.
# ------------------------------------------------------------------

# Ревью G2-Krab-Ear-02b2, KE02B2-10: раньше скрипт безусловно слал `pkill -9` агенту (хоть
# посреди диктовки/встречи) и глотал ошибки tccutil. Теперь: busy-гейт до остановки,
# SIGTERM с ожиданием вместо SIGKILL, провал сброса основного bundle id — ненулевой код.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE_ID="com.antigravity.krab-ear"
APP_BUNDLE="$ROOT_DIR/Krab Ear.app"

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help)
      echo "Usage: $0 [--force]"
      echo "  --force  не проверять активную запись/встречу (аудио активной записи будет потеряно)"
      exit 0 ;;
    *) echo "unknown arg: $arg" >&2; echo "Usage: $0 [--force]" >&2; exit 2 ;;
  esac
done

echo "----------------------------------------------------------------"
echo "Krab Ear: восстановление прав TCC (Accessibility / Mic / Screen)"
echo "----------------------------------------------------------------"
echo ""

if [ ! -d "$APP_BUNDLE" ]; then
  echo "Не найден .app bundle: $APP_BUNDLE"
  echo "   Сначала выполните: make build && make sign"
  exit 1
fi

# 0. Busy-гейт: до любой остановки. Состояние неизвестно = отказ.
if [ "$FORCE" -ne 1 ]; then
  if ! "$ROOT_DIR/scripts/safe_backend_restart.command" --check-only >/dev/null 2>&1; then
    echo "Backend занят записью/встречей либо его состояние неизвестно — ничего не остановлено." >&2
    echo "Дождитесь конца записи и повторите, либо запустите с --force (аудио активной записи будет потеряно)." >&2
    exit 1
  fi
fi

AGENT_PATTERNS=(
  "Krab Ear.app/Contents/MacOS/KrabEarAgent"
  "native/runtime/KrabEarAgent"
  "native/KrabEarAgent/.build/release/KrabEarAgent"
)

echo "1/5 Останавливаем старые процессы (SIGTERM)..."
for pattern in "${AGENT_PATTERNS[@]}"; do
  pkill -f "$pattern" 2>/dev/null || true
done
agent_gone=0
for _ in {1..20}; do
  alive=0
  for pattern in "${AGENT_PATTERNS[@]}"; do
    if pgrep -f "$pattern" >/dev/null 2>&1; then alive=1; fi
  done
  if [ "$alive" -eq 0 ]; then agent_gone=1; break; fi
  sleep 0.5
done
if [ "$agent_gone" -ne 1 ]; then
  echo "Агент не завершился по SIGTERM за 10 с — SIGKILL не посылаю (мог бы оборвать запись)." >&2
  echo "Завершите его вручную и повторите." >&2
  exit 1
fi

# Сброс TCC. Accessibility и Microphone основного bundle id обязаны сброситься (без них
# скрипт теряет смысл); остальные сервисы — best-effort, как и раньше; legacy-идентификаторы
# (com.krabear.agent / KrabEarAgent) могут отсутствовать в базе — это не ошибка.
TCC_FAILED=0
tcc_reset() {
  local service="$1" target="$2" required="$3"
  if ! tccutil reset "$service" "$target" >/dev/null 2>&1; then
    if [ "$required" = "1" ]; then
      echo "  ! tccutil reset $service $target не удался" >&2
      TCC_FAILED=1
    fi
  fi
}

echo "2/5 Сбрасываем Accessibility..."
tcc_reset Accessibility "$BUNDLE_ID" 1
tcc_reset Accessibility com.krabear.agent 0
tcc_reset Accessibility KrabEarAgent 0

echo "3/5 Сбрасываем Microphone..."
tcc_reset Microphone "$BUNDLE_ID" 1

echo "4/5 Сбрасываем ScreenCapture и ListenEvent..."
tcc_reset ScreenCapture "$BUNDLE_ID" 0
tcc_reset ListenEvent "$BUNDLE_ID" 0
tcc_reset PostEvent "$BUNDLE_ID" 0
tcc_reset AppleEvents "$BUNDLE_ID" 0

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

if [ "$TCC_FAILED" -ne 0 ]; then
  echo "Сброс TCC выполнен НЕ полностью (см. строки с '!' выше) — права могли остаться прежними." >&2
  exit 1
fi
