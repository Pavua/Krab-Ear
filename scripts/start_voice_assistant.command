#!/bin/zsh
# ==============================================================================
# Startup script для Phase 1 Voice Assistant ecosystem
# Запускает сервисы в корректном порядке с health checks.
#
# Требования:
# 1. LM Studio запущена с qwen3-30b loaded (port 1234)
# 2. Voice Gateway repo доступен (KRAB_VOICE_GATEWAY_DIR или соседний каталог)
# 3. Krab Ear.app собрана
#
# Порты:
# - 1234  : LM Studio (требует вручную запустить)
# - 8090  : Voice Gateway
# - 8081  : OpenClaw bridge (Krab agent voice endpoint)
#
# Безопасность (ревью ChatGPT G2-Krab-Ear-02b1, 2026-11-27):
# - скрипт НИЧЕГО не убивает: ни по порту (`lsof | kill -9`), ни по имени
#   (`pkill -f`). Занятый порт 8090 с неживым /health или уже запущенный
#   KrabEarAgent — это отказ/пропуск, а не повод завершать чужой процесс;
# - жизненный цикл Gateway делегирован каноническому
#   `scripts/start_voice_gateway.command` (PID-файл, проверка владельца);
# - успех/«запущено» печатается только после проверки, а не после `sleep`.
# ==============================================================================

set -euo pipefail

# Colors для output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Paths. В zsh нет BASH_SOURCE (раньше SCRIPT_DIR молча становился текущим
# каталогом) — берём путь самого скрипта через ${0:A}.
SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${SCRIPT_DIR:h}"
GATEWAY_LAUNCHER="${SCRIPT_DIR}/start_voice_gateway.command"
KRAB_EAR_APP="${PROJECT_ROOT}/Krab Ear.app"

# Endpoints / тайминги (переопределяются окружением — в том числе тестами).
LM_STUDIO_PORT="${LM_STUDIO_PORT:-1234}"
LM_STUDIO_MODELS_URL="${LM_STUDIO_MODELS_URL:-http://127.0.0.1:${LM_STUDIO_PORT}/v1/models}"
LM_STUDIO_MODEL_ID="${LM_STUDIO_MODEL_ID:-qwen3-30b}"
VG_PORT="${VG_PORT:-8090}"
VG_LAUNCHD_LABEL="${VG_LAUNCHD_LABEL:-ai.krab.voice-gateway}"
REST_PORT="${REST_PORT:-5005}"
VG_HEALTH_URL="${VG_HEALTH_URL:-http://127.0.0.1:${VG_PORT}/health}"
OPENCLAW_PORT="${OPENCLAW_PORT:-8081}"
STARTUP_TIMEOUT="${STARTUP_TIMEOUT:-15}"          # секунд
HEALTH_POLL_INTERVAL="${HEALTH_POLL_INTERVAL:-0.5}"  # секунд между опросами
AGENT_START_ATTEMPTS="${AGENT_START_ATTEMPTS:-10}"

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

# Слушает ли кто-то порт. `nc -z` есть в базовой macOS (раньше `timeout 1 bash`,
# а GNU timeout в базовой macOS отсутствует — проверка всегда «порт закрыт»).
is_port_open() {
    local port=$1
    nc -z -w 1 127.0.0.1 "$port" >/dev/null 2>&1
}

# Один запрос health с жёстким таймаутом соединения.
health_ok() {
    curl -s -f --connect-timeout 2 --max-time 3 "$1" >/dev/null 2>&1
}

# Poll for health check. Целочисленный счётчик попыток: раньше elapsed считался
# через bc («.5», «1.0») и сравнивался `[ -lt ]` — на дробном значении сравнение
# падало, цикл завершался после ПЕРВОГО неудачного опроса.
wait_for_health() {
    local url=$1
    local max_wait=$2
    local service_name=$3
    local attempt=0
    local max_attempts=$(( max_wait * 2 ))
    (( max_attempts < 1 )) && max_attempts=1

    log_step "Проверяю здоровье $service_name (ожидание ${max_wait}s)..."

    while (( attempt < max_attempts )); do
        if health_ok "$url"; then
            log_info "$service_name готов ✅"
            return 0
        fi
        sleep "$HEALTH_POLL_INTERVAL"
        attempt=$(( attempt + 1 ))
    done

    log_error "$service_name не ответил за $max_wait секунд ❌"
    return 1
}

# ==============================================================================
# Pre-flight checks
# ==============================================================================

log_step "Запуск pre-flight проверок..."

# Check LM Studio
if ! is_port_open "$LM_STUDIO_PORT"; then
    log_error "LM Studio не запущена на порту $LM_STUDIO_PORT"
    log_warn "Пожалуйста запустите LM Studio и загрузите $LM_STUDIO_MODEL_ID"
    exit 1
fi
log_info "LM Studio запущена ✅"

# Verify the model is loaded (точный ID, а не любая модель со словом «qwen»)
if ! curl -s --connect-timeout 2 --max-time 5 "$LM_STUDIO_MODELS_URL" 2>/dev/null \
        | grep -qiF "$LM_STUDIO_MODEL_ID"; then
    log_error "$LM_STUDIO_MODEL_ID не загружена в LM Studio"
    log_warn "Пожалуйста загрузите $LM_STUDIO_MODEL_ID через LM Studio UI"
    exit 1
fi
log_info "$LM_STUDIO_MODEL_ID загружена ✅"

# Check the canonical Voice Gateway launcher
if [ ! -f "$GATEWAY_LAUNCHER" ]; then
    log_error "Канонический launcher Voice Gateway не найден: $GATEWAY_LAUNCHER"
    exit 1
fi
log_info "Launcher Voice Gateway найден ✅"

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

# 1. Voice Gateway
log_step "1/3: Voice Gateway..."

if is_port_open "$VG_PORT"; then
    # Порт занят. Свой здоровый Gateway — переиспользуем. Иначе НЕ убиваем:
    # владелец процесса неизвестен (раньше `lsof -ti | xargs kill -9`).
    if health_ok "$VG_HEALTH_URL"; then
        log_info "Voice Gateway уже запущен и здоров — не перезапускаю ✅"
    else
        log_error "Порт $VG_PORT занят, но $VG_HEALTH_URL не отвечает."
        log_warn "Неизвестный процесс не завершаю. Остановите его вручную"
        log_warn "(scripts/stop_voice_gateway.command) и запустите скрипт снова."
        exit 1
    fi
else
    # Лаунчер Gateway (репо VG): (а) при упавшем Krab Ear REST (:REST_PORT) зовёт
    # safe_backend_restart.command --with-rest — рестарт прод-backend (гейт под
    # записью у него есть, но это всё равно вмешательство в прод); (б) при
    # упавшей launchd-копии поднял бы ВТОРОЙ Gateway с другим окружением.
    # Оба случая здесь не делаем молча (ревью Opus #2303, P2).
    if launchctl print "gui/$(id -u)/$VG_LAUNCHD_LABEL" >/dev/null 2>&1; then
        log_error "Порт $VG_PORT закрыт, но юнит $VG_LAUNCHD_LABEL загружен в launchd."
        log_warn "Второй Gateway с другим окружением не поднимаю. Перезапуск юнита — решение владельца:"
        log_warn "launchctl kickstart gui/\$(id -u)/$VG_LAUNCHD_LABEL"
        exit 1
    fi
    if ! is_port_open "$REST_PORT" && [ "${ALLOW_BACKEND_RESTART_VIA_LAUNCHER:-0}" != "1" ]; then
        log_error "Krab Ear REST (:$REST_PORT) не отвечает: лаунчер Gateway перезапустил бы прод-backend."
        log_warn "Поднимите backend штатно: scripts/safe_backend_restart.command --with-rest"
        log_warn "или повторите с ALLOW_BACKEND_RESTART_VIA_LAUNCHER=1 (осознанно)."
        exit 1
    fi
    if ! zsh "$GATEWAY_LAUNCHER"; then
        log_error "Launcher Voice Gateway завершился с ошибкой"
        exit 1
    fi
    if ! wait_for_health "$VG_HEALTH_URL" "$STARTUP_TIMEOUT" "Voice Gateway"; then
        # Содержимое лога НЕ печатаем: там могут быть фрагменты разговоров
        # и ключи. Диагностику смотрит владелец, а не терминал.
        log_error "Voice Gateway startup failed — смотрите лог, который пишет launcher Gateway."
        exit 1
    fi
fi

# 2. Check for Krab OpenClaw bridge (port 8081)
# Note: This is typically managed by Krab agent (Telegram bot)
log_step "2/3: Проверяю OpenClaw voice bridge (port ${OPENCLAW_PORT})..."

OPENCLAW_UP=0
if is_port_open "$OPENCLAW_PORT"; then
    OPENCLAW_UP=1
    log_info "OpenClaw voice bridge запущен ✅"
else
    log_warn "OpenClaw voice bridge не доступен на порту ${OPENCLAW_PORT}"
    log_warn "Убедитесь что Krab agent запущен (это отдельный процесс)"
    log_warn "Продолжаю только с Voice Gateway..."
fi

# 3. Krab Ear.app
log_step "3/3: Krab Ear.app..."

# KrabEarAgent — прод-диктовка владельца (обычно под launchd). Запущенный агент
# НЕ перезапускаем и не убиваем (`pkill -f` рвал диктовку, а `open` поверх живого
# агента — конфликт с SingleInstanceGuard). Точное имя процесса, не `-f`.
if pgrep -x "KrabEarAgent" >/dev/null 2>&1; then
    log_info "KrabEarAgent уже запущен — не трогаю ✅"
else
    open "$KRAB_EAR_APP"
    started=0
    attempt=0
    while (( attempt < AGENT_START_ATTEMPTS )); do
        if pgrep -x "KrabEarAgent" >/dev/null 2>&1; then
            started=1
            break
        fi
        sleep "$HEALTH_POLL_INTERVAL"
        attempt=$(( attempt + 1 ))
    done
    if (( started == 0 )); then
        log_error "Krab Ear.app не поднялась: процесс KrabEarAgent не появился ❌"
        exit 1
    fi
    log_info "Krab Ear.app запущена ✅"
fi
KRAB_PID="$(pgrep -x "KrabEarAgent" | head -1 || true)"
KRAB_PID="${KRAB_PID:-unknown}"

# ==============================================================================
# Summary & Health Check
# ==============================================================================

if (( OPENCLAW_UP == 1 )); then
    log_step "Итого: все сервисы запущены"
else
    log_step "Итого: запущено частично (OpenClaw bridge недоступен — DEGRADED)"
fi

echo ""
echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
echo -e "Service          | Port  | Status"
echo -e "─────────────────┼───────┼─────────────────────────────────────"
echo -e "LM Studio        | ${LM_STUDIO_PORT}  | ${GREEN}✅ запущена${NC}, ${LM_STUDIO_MODEL_ID} loaded"
echo -e "Voice Gateway    | ${VG_PORT}  | ${GREEN}✅ здоров${NC}"

if (( OPENCLAW_UP == 1 )); then
    echo -e "OpenClaw voice   | ${OPENCLAW_PORT}  | ${GREEN}✅ запущена${NC}"
else
    echo -e "OpenClaw voice   | ${OPENCLAW_PORT}  | ${YELLOW}⏳ недоступен (Krab agent)${NC}"
fi

echo -e "Krab Ear .app    | -     | ${GREEN}✅ запущена${NC}, PID $KRAB_PID"
echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"

echo ""
echo "🚀 Дальнейшие шаги:"
echo "  1. Откройте Krab Ear > вкладка 'Conversation' (новая вкладка)"
echo "  2. Нажмите кнопку 'Start Conversation' или используйте Right Option key"
echo "  3. Говорите, AI будет отвечать в реальном времени"
echo ""
echo "⚠️  Если услышите ошибку — смотрите лог Voice Gateway (путь пишет launcher Gateway)."
echo ""
