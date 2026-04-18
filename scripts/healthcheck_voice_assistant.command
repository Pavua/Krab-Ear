#!/bin/zsh
# ==============================================================================
# Health check script для Phase 1 Voice Assistant ecosystem
# Проверяет все 4 сервиса и выводит таблицу статуса
#
# Запуск: ./scripts/healthcheck_voice_assistant.command
# ==============================================================================

set -euo pipefail

# Colors для output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ==============================================================================
# Helper functions
# ==============================================================================

check_port() {
    local port=$1
    timeout 1 bash -c "echo >/dev/tcp/127.0.0.1/$port" 2>/dev/null || return 1
}

get_lm_studio_status() {
    if ! check_port 1234; then
        echo "❌ не запущена"
        return 1
    fi

    # Try to get models
    local models=$(curl -s http://127.0.0.1:1234/v1/models 2>/dev/null | grep -o '"id":"[^"]*' | head -1 | cut -d'"' -f4 || echo "unknown")

    if echo "$models" | grep -q "qwen"; then
        echo "✅ запущена, $models loaded"
        return 0
    else
        echo "⏳ запущена, ожидание qwen3-30b (текущая: $models)"
        return 0
    fi
}

get_voice_gateway_status() {
    if ! check_port 8090; then
        echo "❌ не запущена"
        return 1
    fi

    # Try to get health + engines
    local health=$(curl -s http://127.0.0.1:8090/health 2>/dev/null)

    if echo "$health" | grep -q "running"; then
        # Try to extract engines if available
        local engines=$(echo "$health" | grep -o '"engine":"[^"]*' | cut -d'"' -f4 | paste -sd ',' - || echo "ok")
        echo "✅ запущена, engines: $engines"
        return 0
    else
        echo "✅ запущена"
        return 0
    fi
}

get_openclaw_status() {
    if ! check_port 8081; then
        echo "❌ не доступен (Krab agent не запущен?)"
        return 1
    fi

    # Try OpenClaw voice status endpoint
    local status=$(curl -s http://127.0.0.1:8081/v1/voice/status 2>/dev/null || echo "{}")

    if echo "$status" | grep -q "running\|active"; then
        echo "✅ запущен"
        return 0
    else
        echo "✅ доступен"
        return 0
    fi
}

get_krab_ear_status() {
    local pid=$(pgrep -f "KrabEarAgent" | head -1 || echo "")

    if [ -z "$pid" ]; then
        echo "❌ не запущена"
        return 1
    else
        echo "✅ запущена (PID $pid)"
        return 0
    fi
}

# ==============================================================================
# Main
# ==============================================================================

echo ""
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}Phase 1 Voice Assistant Health Check${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo ""

# Collect all statuses
LM_STUDIO=$(get_lm_studio_status)
VG_STATUS=$(get_voice_gateway_status)
OPENCLAW=$(get_openclaw_status)
KRAB_EAR=$(get_krab_ear_status)

# Track overall health
OVERALL_OK=0

# Output table
echo "Service          | Port  | Status"
echo "─────────────────┼───────┼─────────────────────────────────────"

echo -n "LM Studio        | 1234  | "
if echo "$LM_STUDIO" | grep -q "✅"; then
    echo -e "${GREEN}${LM_STUDIO}${NC}"
    OVERALL_OK=$((OVERALL_OK + 1))
else
    echo -e "${RED}${LM_STUDIO}${NC}"
fi

echo -n "Voice Gateway    | 8090  | "
if echo "$VG_STATUS" | grep -q "✅"; then
    echo -e "${GREEN}${VG_STATUS}${NC}"
    OVERALL_OK=$((OVERALL_OK + 1))
else
    echo -e "${RED}${VG_STATUS}${NC}"
fi

echo -n "OpenClaw voice   | 8081  | "
if echo "$OPENCLAW" | grep -q "✅"; then
    echo -e "${GREEN}${OPENCLAW}${NC}"
    OVERALL_OK=$((OVERALL_OK + 1))
else
    echo -e "${YELLOW}${OPENCLAW}${NC}"
fi

echo -n "Krab Ear .app    | -     | "
if echo "$KRAB_EAR" | grep -q "✅"; then
    echo -e "${GREEN}${KRAB_EAR}${NC}"
    OVERALL_OK=$((OVERALL_OK + 1))
else
    echo -e "${RED}${KRAB_EAR}${NC}"
fi

echo ""

# Summary
if [ $OVERALL_OK -eq 4 ]; then
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}✅ Все сервисы готовы к работе!${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    exit 0
elif [ $OVERALL_OK -ge 3 ]; then
    echo -e "${YELLOW}════════════════════════════════════════════════════════════${NC}"
    echo -e "${YELLOW}⚠️  Некоторые сервисы недоступны${NC}"
    echo -e "${YELLOW}════════════════════════════════════════════════════════════${NC}"
    exit 1
else
    echo -e "${RED}════════════════════════════════════════════════════════════${NC}"
    echo -e "${RED}❌ Основные сервисы недоступны${NC}"
    echo -e "${RED}════════════════════════════════════════════════════════════${NC}"
    exit 1
fi
