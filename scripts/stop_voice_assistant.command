#!/bin/zsh
# ==============================================================================
# Graceful shutdown script для Phase 1 Voice Assistant ecosystem
# Останавливает все сервисы в обратном порядке
#
# Запуск: ./scripts/stop_voice_assistant.command
# ==============================================================================

set -euo pipefail

# Colors для output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_step() {
    echo -e "\n${BLUE}→${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# ==============================================================================
# Shutdown sequence
# ==============================================================================

echo ""
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}Остановка Voice Assistant сервисов...${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"

# 1. Stop Krab Ear.app
log_step "1/3: Остановка Krab Ear.app..."
if pgrep -f "KrabEarAgent" > /dev/null; then
    pkill -f "KrabEarAgent" || true
    sleep 1
    log_info "Krab Ear.app остановлена ✅"
else
    log_warn "Krab Ear.app не запущена"
fi

# 2. Stop Voice Gateway
log_step "2/3: Остановка Voice Gateway..."

# Find Python process on port 8090
if lsof -Pi :8090 -sTCP:LISTEN -t > /dev/null 2>&1; then
    VG_PID=$(lsof -Pi :8090 -sTCP:LISTEN -t | head -1)
    kill -TERM $VG_PID 2>/dev/null || true

    # Wait a bit for graceful shutdown
    sleep 2

    # Force kill if still running
    if kill -0 $VG_PID 2>/dev/null; then
        kill -9 $VG_PID 2>/dev/null || true
        log_info "Voice Gateway остановлена (force kill) ✅"
    else
        log_info "Voice Gateway остановлена (graceful) ✅"
    fi
else
    log_warn "Voice Gateway не запущена на порту 8090"
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
echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}✅ Сервисы остановлены${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
echo ""
