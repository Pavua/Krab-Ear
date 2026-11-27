#!/bin/zsh
# ==============================================================================
# Остановка Phase 1 Voice Assistant ecosystem
#
# Запуск: ./scripts/stop_voice_assistant.command
#
# Безопасность (ревью ChatGPT G2-Krab-Ear-02b1, KE02B1-05/12, 2026-11-27):
# - Voice Gateway останавливает ТОЛЬКО канонический
#   `scripts/stop_voice_gateway.command` (PID-файл + сверка владельца процесса
#   перед каждым сигналом). Раньше здесь был `lsof | kill -TERM`, потом
#   `kill -9` по PID, который мог успеть смениться (PID reuse).
# - Krab Ear.app/KrabEarAgent НЕ останавливается по имени (`pkill -f`): это
#   прод-диктовка владельца, обычно под launchd; kill по имени запрещён
#   проектом (SingleInstanceGuard — только flock). Закрыть приложение —
#   через меню Krab Ear → Quit.
# - итоговый статус «остановлено» печатается, только если остановка
#   подтверждена кодом возврата И порт Gateway после остановки свободен.
#   Канонический stop_gateway без .gateway.pid говорит «вероятно, уже
#   остановлен» и выходит 0, хотя Gateway живёт под launchd — одного кода
#   возврата мало (ревью Opus #2303, P1). Порт занят → ненулевой выход и
#   подсказка владельцу; сам скрипт ничего не завершает.
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

SCRIPT_DIR="${0:A:h}"
GATEWAY_STOP="${SCRIPT_DIR}/stop_voice_gateway.command"
VG_PORT="${VG_PORT:-8090}"
VG_LAUNCHD_LABEL="${VG_LAUNCHD_LABEL:-ai.krab.voice-gateway}"
STOP_VERIFY_ATTEMPTS="${STOP_VERIFY_ATTEMPTS:-5}"      # проверок «порт свободен»
STOP_VERIFY_INTERVAL="${STOP_VERIFY_INTERVAL:-1}"      # секунд между проверками

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_step() {
    echo -e "\n${BLUE}→${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Слушает ли кто-то порт (`nc -z` есть в базовой macOS).
is_port_open() {
    nc -z -w 1 127.0.0.1 "$1" >/dev/null 2>&1
}

FAILED=0

echo ""
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}Остановка Voice Assistant сервисов...${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"

# 1. Krab Ear.app — только сообщаем, не убиваем.
log_step "1/3: Krab Ear.app..."
if pgrep -x "KrabEarAgent" >/dev/null 2>&1; then
    log_warn "KrabEarAgent запущен и НЕ останавливается этим скриптом (прод-диктовка)."
    log_warn "Закройте приложение вручную: меню Krab Ear → Quit."
else
    log_info "Krab Ear.app не запущена"
fi

# 2. Voice Gateway — делегируем каноническому скрипту.
log_step "2/3: Остановка Voice Gateway..."
if [ ! -f "$GATEWAY_STOP" ]; then
    log_error "Канонический скрипт остановки не найден: $GATEWAY_STOP"
    FAILED=1
elif zsh "$GATEWAY_STOP"; then
    # Код возврата не доказательство: без PID-файла канонический скрипт выходит 0 при живом Gateway.
    verify_attempt=0
    while is_port_open "$VG_PORT" && (( verify_attempt < STOP_VERIFY_ATTEMPTS )); do
        verify_attempt=$(( verify_attempt + 1 ))
        sleep "$STOP_VERIFY_INTERVAL"
    done
    if is_port_open "$VG_PORT"; then
        log_error "Voice Gateway НЕ остановлен: порт $VG_PORT всё ещё слушает ❌"
        log_warn "Вероятно, Gateway под launchd (юнит $VG_LAUNCHD_LABEL), PID-файла нет."
        log_warn "Остановить может только владелец: launchctl bootout gui/\$(id -u)/$VG_LAUNCHD_LABEL"
        log_warn "Этот скрипт процесс не завершает."
        FAILED=1
    else
        log_info "Voice Gateway: остановлен, порт $VG_PORT свободен ✅"
    fi
else
    log_error "Voice Gateway НЕ остановлен: канонический скрипт вернул ошибку ❌"
    FAILED=1
fi

# 3. Note about OpenClaw/Krab agent
log_step "3/3: OpenClaw bridge..."
log_warn "OpenClaw bridge (port 8081) управляется Krab agent"
log_warn "Для его остановки используйте: /Users/pablito/Antigravity_AGENTS/new\\ Stop\\ Krab.command"

# LM Studio note
echo ""
log_warn "LM Studio (port 1234) оставлена запущенной"
log_warn "Закройте LM Studio вручную если необходимо"

# ==============================================================================
# Summary
# ==============================================================================

echo ""
if (( FAILED == 0 )); then
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}✅ Voice Gateway остановлен (Krab Ear.app не затронута)${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo ""
else
    echo -e "${RED}════════════════════════════════════════════════════════════${NC}"
    echo -e "${RED}❌ Остановка не подтверждена — см. сообщения выше${NC}"
    echo -e "${RED}════════════════════════════════════════════════════════════${NC}"
    echo ""
    exit 1
fi
