#!/bin/zsh
# ==============================================================================
# Startup script для Phase 1 Voice Assistant ecosystem
# Запускает все сервисы в корректном порядке с health checks
#
# Требования:
# 1. LM Studio запущена с qwen3-30b loaded (port 1234)
# 2. Voice Gateway repo доступен
# 3. Python venv'ы установлены
#
# Порты:
# - 1234  : LM Studio (требует вручную запустить)
# - 8090  : Voice Gateway
# - 8081  : OpenClaw bridge (Krab agent voice endpoint)
# ==============================================================================

set -euo pipefail

# Colors для output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VG_DIR="/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway"
KRAB_EAR_APP="${PROJECT_ROOT}/Krab Ear.app"

# Log files
TMPDIR="${TMPDIR:-/tmp}"
VG_LOG="${TMPDIR}/krab_voice_gateway.log"
OPENCLAW_LOG="${TMPDIR}/krab_openclaw_voice.log"

# Timeouts (seconds)
STARTUP_TIMEOUT=15
HEALTH_POLL_INTERVAL=0.5

# ==============================================================================
# Helper functions
# ==============================================================================

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
    echo -e "\n${BLUE}→${NC} $1"
}

# Check if a service is listening on a port
is_port_open() {
    local port=$1
    timeout 1 bash -c "echo >/dev/tcp/127.0.0.1/$port" 2>/dev/null || return 1
}

# Poll for health check with timeout
wait_for_health() {
    local url=$1
    local max_wait=$2
    local service_name=$3
    local elapsed=0

    log_step "Проверяю здоровье $service_name (ожидание ${max_wait}s)..."

    while [ $elapsed -lt $max_wait ]; do
        if curl -s -f "$url" > /dev/null 2>&1; then
            log_info "$service_name готов ✅"
            return 0
        fi
        sleep $HEALTH_POLL_INTERVAL
        elapsed=$(echo "$elapsed + $HEALTH_POLL_INTERVAL" | bc)
    done

    log_error "$service_name не ответил за $max_wait секунд ❌"
    return 1
}

# ==============================================================================
# Pre-flight checks
# ==============================================================================

log_step "Запуск pre-flight проверок..."

# Check LM Studio
if ! is_port_open 1234; then
    log_error "LM Studio не запущена на порту 1234"
    log_warn "Пожалуйста запустите LM Studio и загрузите qwen3-30b"
    exit 1
fi
log_info "LM Studio запущена ✅"

# Verify qwen3-30b loaded
if ! curl -s http://127.0.0.1:1234/v1/models 2>/dev/null | grep -q "qwen"; then
    log_error "qwen3-30b не загружена в LM Studio"
    log_warn "Пожалуйста загрузите qwen3-30b через LM Studio UI"
    exit 1
fi
log_info "qwen3-30b загружена ✅"

# Check Voice Gateway dir
if [ ! -d "$VG_DIR" ]; then
    log_error "Voice Gateway директория не найдена: $VG_DIR"
    exit 1
fi
log_info "Voice Gateway директория найдена ✅"

# Check Krab Ear.app exists
if [ ! -d "$KRAB_EAR_APP" ]; then
    log_error "Krab Ear.app не найдена: $KRAB_EAR_APP"
    exit 1
fi
log_info "Krab Ear.app найдена ✅"

# ==============================================================================
# Startup sequence
# ==============================================================================

log_step "Запуск сервисов..."

# 1. Start Voice Gateway
log_step "1/3: Запуск Voice Gateway..."
cd "$VG_DIR"

# Activate venv if it exists
if [ -f ".venv_voice_gateway/bin/activate" ]; then
    source ".venv_voice_gateway/bin/activate"
elif [ -f ".venv/bin/activate" ]; then
    source ".venv/bin/activate"
else
    log_warn "venv не найден в Voice Gateway, попытка запустить python напрямую"
fi

# Kill any existing VG process on port 8090
if is_port_open 8090; then
    log_warn "Порт 8090 уже занят, пытаюсь остановить старый процесс..."
    lsof -ti:8090 | xargs kill -9 2>/dev/null || true
    sleep 1
fi

# Start Voice Gateway in background
nohup python -m app.main > "$VG_LOG" 2>&1 &
VG_PID=$!
log_info "Voice Gateway PID: $VG_PID (лог: $VG_LOG)"

# Wait for VG health
if ! wait_for_health "http://127.0.0.1:8090/health" "$STARTUP_TIMEOUT" "Voice Gateway"; then
    log_error "Voice Gateway startup failed"
    cat "$VG_LOG" | tail -20
    exit 1
fi

# 2. Check for Krab OpenClaw bridge (port 8081)
# Note: This is typically managed by Krab agent (Telegram bot)
# We just verify it's available for voice endpoints
log_step "2/3: Проверяю OpenClaw voice bridge (port 8081)..."

if is_port_open 8081; then
    log_info "OpenClaw voice bridge запущен ✅"
else
    log_warn "OpenClaw voice bridge не доступен на порту 8081"
    log_warn "Убедитесь что Krab agent запущен (это отдельный процесс)"
    log_warn "Продолжаю только с Voice Gateway..."
fi

# 3. Launch Krab Ear.app
log_step "3/3: Запуск Krab Ear.app..."

# Kill any existing Krab Ear process
pkill -f "KrabEarAgent" 2>/dev/null || true
sleep 1

# Open app
open "$KRAB_EAR_APP"
sleep 2

# Get PID if available
KRAB_PID=$(pgrep -f "KrabEarAgent" | head -1 || echo "unknown")
log_info "Krab Ear.app запущена (PID: $KRAB_PID)"

# ==============================================================================
# Summary & Health Check
# ==============================================================================

log_step "Итого: все сервисы запущены"

echo ""
echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
echo -e "Service          | Port  | Status"
echo -e "─────────────────┼───────┼─────────────────────────────────────"
echo -e "LM Studio        | 1234  | ${GREEN}✅ запущена${NC}, qwen3-30b loaded"
echo -e "Voice Gateway    | 8090  | ${GREEN}✅ запущена${NC}, PID $VG_PID"

if is_port_open 8081; then
    echo -e "OpenClaw voice   | 8081  | ${GREEN}✅ запущена${NC}"
else
    echo -e "OpenClaw voice   | 8081  | ${YELLOW}⏳ ожидание (Krab agent)${NC}"
fi

echo -e "Krab Ear .app    | -     | ${GREEN}✅ запущена${NC}, PID $KRAB_PID"
echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"

echo ""
echo "📋 Логи:"
echo "  Voice Gateway: $VG_LOG"
echo "  OpenClaw:      $OPENCLAW_LOG"
echo ""
echo "🚀 Дальнейшие шаги:"
echo "  1. Откройте Krab Ear > вкладка 'Conversation' (новая вкладка)"
echo "  2. Нажмите кнопку 'Start Conversation' или используйте Right Option key"
echo "  3. Говорите, AI будет отвечать в реальном времени"
echo ""
echo "⚠️  Если услышите ошибку, проверьте логи:"
echo "  tail -f $VG_LOG"
echo ""
