# Phase 1 Voice Assistant — Startup Scripts

Quick-start scripts для запуска Phase 1 Voice Assistant ecosystem (UI + Voice Gateway + Krab agent brain).

## Scripts

### 1. `start_voice_assistant.command`
**Запуск всех сервисов в корректном порядке с автоматическими health checks.**

```bash
./scripts/start_voice_assistant.command
```

**Что делает:**
1. ✅ Проверяет LM Studio запущена на порту 1234
2. ✅ Проверяет qwen3-30b загружена в LM Studio
3. ✅ Запускает Voice Gateway (FastAPI) через лаунчер `scripts/start_voice_gateway.command`; здоровый уже работающий Gateway переиспользует
   (гейты ниже)
4. ⏳ Проверяет OpenClaw bridge доступен (Krab agent)
5. ✅ Запускает Krab Ear.app, если агент не запущен (запущенный не трогает)
6. 📊 Выводит таблицу со статусом всех сервисов

**Гейты (скрипт отказывает, ничего не убивая):**
- порт 8090 занят, но `/health` молчит — неизвестный процесс не завершается;
- порт 8090 закрыт, но launchd-юнит `ai.krab.voice-gateway` загружен — второй Gateway с другим окружением не поднимается;
  перезапуск юнита (`launchctl kickstart gui/<uid>/ai.krab.voice-gateway`) — решение владельца;
- Krab Ear REST (`:5005`) не отвечает — лаунчер Gateway перезапустил бы прод-backend (`safe_backend_restart.command --with-rest`;
  гейт «не под записью» у него есть). Без `ALLOW_BACKEND_RESTART_VIA_LAUNCHER=1` скрипт отказывает: поднимите backend штатно.

**Требования перед запуском:**
- LM Studio установлена и **запущена вручную**
- qwen3-30b загружена в LM Studio
- Krab Voice Gateway repo доступен в `/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway`
- Krab Ear `.app` собрана

**Порты (должны быть свободны):**
- 8090 — Voice Gateway
- 8081 — OpenClaw bridge (управляется Krab agent)
- 1234 — LM Studio (вручную)

**Логи:** пишет канонический `scripts/start_voice_gateway.command` (путь он печатает); сам скрипт содержимое лога не выводит.

---

### 2. `healthcheck_voice_assistant.command`
**Проверяет здоровье всех сервисов и выводит таблицу статуса.**

```bash
./scripts/healthcheck_voice_assistant.command
```

**Вывод:**
```
Service          | Port  | Status
─────────────────┼───────┼─────────────────────────────────────
LM Studio        | 1234  | ✅ запущена, qwen3-30b loaded
Voice Gateway    | 8090  | ✅ запущена, engines: moshi, seamless
OpenClaw voice   | 8081  | ✅ запущен
Krab Ear .app    | -     | ✅ запущена (PID 12345)
```

**Exit codes:**
- 0 — все сервисы готовы
- 1 — некоторые сервисы недоступны

**Полезно для:**
- Проверки статуса перед началом работы
- CI/CD health checks
- Отладки проблем со стартапом

---

### 3. `stop_voice_assistant.command`
**Graceful shutdown всех сервисов в обратном порядке.**

```bash
./scripts/stop_voice_assistant.command
```

**Что делает:**
1. Krab Ear.app **не останавливает** (прод-диктовка; закрыть — меню Krab Ear → Quit)
2. Останавливает Voice Gateway только через канонический `scripts/stop_voice_gateway.command` (PID-файл + сверка владельца процесса); при ошибке — ненулевой выход.
   Успех печатается, только если после этого порт 8090 свободен: без PID-файла (Gateway под launchd) канонический скрипт выходит 0 при живом
   Gateway — тогда скрипт выходит с 1 и просит владельца: `launchctl bootout gui/<uid>/ai.krab.voice-gateway`
3. Оставляет OpenClaw bridge (управляется Krab agent)
4. Оставляет LM Studio запущенной (закройте вручную)

**Примечание:** Для полной остановки включая Krab agent используйте:
```bash
/Users/pablito/Antigravity_AGENTS/new\ Stop\ Krab.command
```

---

## Типичный workflow

### Первый раз

1. **Запустите LM Studio вручную**
   - Откройте LM Studio
   - Загрузите `lmstudio-community/Qwen3-30B-A3B-Instruct-2507-MLX-4bit`
   - Убедитесь что модель загружена (видно в UI)

2. **Убедитесь что Krab agent запущен**
   ```bash
   /Users/pablito/Antigravity_AGENTS/new\ start_krab.command
   ```

3. **Запустите Voice Assistant сервисы**
   ```bash
   /Users/pablito/Antigravity_AGENTS/Krab\ Ear/scripts/start_voice_assistant.command
   ```

4. **Проверьте здоровье**
   ```bash
   /Users/pablito/Antigravity_AGENTS/Krab\ Ear/scripts/healthcheck_voice_assistant.command
   ```

5. **Используйте Voice Assistant**
   - Откройте Krab Ear
   - Перейдите на вкладку "Conversation"
   - Нажмите "Start Conversation" или используйте Right Option key
   - Говорите!

### Каждый день

```bash
# Просто запустите скрипт (он всё сделает)
./scripts/start_voice_assistant.command

# Проверьте что всё работает
./scripts/healthcheck_voice_assistant.command
```

### Остановка

```bash
./scripts/stop_voice_assistant.command

# Для полной остановки включая Krab agent
/Users/pablito/Antigravity_AGENTS/new\ Stop\ Krab.command
```

---

## Troubleshooting

### "LM Studio не запущена на порту 1234"
**Решение:** Запустите LM Studio вручную и подождите пока загрузится.

### "qwen3-30b не загружена в LM Studio"
**Решение:** Откройте LM Studio и выберите модель `lmstudio-community/Qwen3-30B-A3B-Instruct-2507-MLX-4bit` из библиотеки, затем загрузите.

### "Voice Gateway startup failed"
**Решение:** Проверьте лог Voice Gateway (путь печатает канонический `scripts/start_voice_gateway.command`).
Скрипт сам содержимое лога в терминал не выводит.

### "OpenClaw voice bridge не доступен"
**Решение:** Убедитесь что Krab agent запущен:
```bash
/Users/pablito/Antigravity_AGENTS/new\ start_krab.command
```

### Порт уже занят
**Решение:** Порт занят процессом, чей /health не отвечает. Скрипт его НЕ убивает.
Остановите Gateway штатно (`./scripts/stop_voice_assistant.command`), а если порт занят
чужим процессом — разберитесь с ним вручную (`lsof -nP -iTCP:8090 -sTCP:LISTEN`).

---

## Architecture overview

```
┌─────────────────────────────────────────────────────────────┐
│ Krab Ear.app (macOS)                                        │
│ - Conversation UI tab                                        │
│ - WebSocket client для Voice Gateway                        │
│ └─ Right Option key hotkey                                  │
└────────────┬──────────────────────────────────────────────┘
             │ ws://localhost:8090/v1/sessions
             │
┌────────────▼──────────────────────────────────────────────┐
│ Voice Gateway (FastAPI, port 8090)                        │
│ - Moshi 7B engine (EN streaming STT)                       │
│ - SeamlessStreaming engine (RU/ES/EN translation)         │
│ - Session management                                       │
│ └─ Real-time bidirectional audio + text                   │
└────────────┬──────────────────────────────────────────────┘
             │ HTTP bridge to Krab agent (port 8081)
             │
┌────────────▼──────────────────────────────────────────────┐
│ Krab agent (OpenClaw, port 8081)                          │
│ - Qwen3-30B via LM Studio (port 1234)                     │
│ - Call Assistant                                           │
│ - Tools + memory                                           │
│ └─ Full AI brain                                          │
└────────────────────────────────────────────────────────────┘
```

---

## Notes

- **Voice Gateway venv:** автоматически активируется если существует `.venv_voice_gateway` или `.venv`
- **LM Studio:** вне управления этих скриптов (запустите вручную, закройте вручную)
- **Krab agent:** вне управления Voice Assistant скриптов (управляется отдельной командой)
- **Logs:** лог Voice Gateway пишет канонический launcher; скрипты Voice Assistant логи не печатают

---

**Phase 1 Voice Assistant MVP**  
Krab Ear + Voice Gateway + Qwen3-30B  
Real-time conversation mode with 160-400ms end-to-end latency
