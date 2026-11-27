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
#   подтверждена кодом возврата; иначе — ненулевой выход.
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

SCRIPT_DIR="${0:A:h}"
GATEWAY_STOP="${SCRIPT_DIR}/stop_voice_gateway.command"

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
    log_info "Voice Gateway: канонический скрипт остановки завершился успешно ✅"
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
