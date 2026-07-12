# Krab Ear — Локальный голосовой ассистент для macOS

> Офлайн-транскрипция, перевод и авто-вставка текста одним нажатием клавиши.
> Работает полностью на устройстве. Никаких облачных сервисов, никакой телеметрии.

---

## Возможности

- 🎙️ **Офлайн STT** — распознавание речи с 5 адаптерами: `mlx-whisper`, Parakeet, SenseVoice, WhisperX, Voxtral
- 🤖 **Voice Assistant Mode** — 4-й таб "Разговор с AI", двойной Right Option, трёхуровневая архитектура (UI + Orchestration + Brain)
- 🌐 **Перевод** — RU↔ES, EN→RU, Auto, Bilingual; глоссарий с автодополнением
- 👥 **Диаризация** — определение спикеров через `pyannote.audio`, ускорение на MPS
- 🔊 **TTS** — Silero (RU) + Kokoro (EN) + macOS say fallback, opt-in режим
- 🤖 **LLM-постобработка** — локальная правка текста (qwen3-4b через LM Studio) с защитой от галлюцинаций
- ⌨️ **Авто-вставка** — готовый текст вставляется в активное приложение через Accessibility API
- 📂 **История транскриптов** — append-only NDJSON, нечёткий поиск, коллекции, архивация
- 📊 **Аналитика** — дэшборд, тепловая карта активности, тренды качества, статистика спикеров
- 📤 **9 форматов экспорта** — TXT, MD, SRT, CSV, HTML, JSON, Obsidian, шаблоны, расписание
- 🔌 **REST API** — Flask на порту 5005; webhooks; SSE-стриминг событий
- 🔒 **Безопасность** — HMAC-подпись IPC, rate limiting, PII-редакция, аудит-лог

---

## Скриншот

![Krab Ear UI](docs/screenshot_placeholder.png)

*Панель истории / Live-оверлей / Настройки*

---

## Быстрый старт для пользователей

> Хотите просто пользоваться — без погружения в код? Читайте **[Руководство пользователя (RU)](docs/USER_MANUAL.md)** — там пошагово расписано всё: установка, горячие клавиши, диктовка, перевод, Voice Assistant и решение типичных проблем.

Краткий старт:

1. Распакуйте архив и дважды кликните **`Start Krab Ear.command`**.
2. Выдайте два разрешения: **Микрофон** и **Универсальный доступ (Accessibility)**.
3. Нажмите **Right Option** → говорите → нажмите **Right Option** снова → текст вставится.

**Требования:** macOS 13+, Apple Silicon (M1–M4), Python 3.11+.

---

## Быстрый старт для разработчиков

```bash
# 1. Клонируйте репозиторий
git clone https://github.com/antigravity/krab-ear.git && cd "Krab Ear"

# 2. Запустите одним файлом — создаст venv, установит зависимости, запустит агент
open "Start Krab Ear.command"

# 3. Нажмите Right Option — начнётся запись; повторное нажатие остановит и вставит текст
```

---

## Архитектура

```
┌─────────────────────────┐   Unix socket (JSON-RPC)   ┌──────────────────────┐
│  Swift Agent (macOS)    │ ◄────────────────────────► │  Python Backend      │
│  - HotkeyManager        │                            │  - IPCServer (195+   │
│  - PasteService         │   Krab Ear.app/            │    методов)          │
│  - HistoryPanel         │   (bundle: agent +         │  - AudioEngine (STT) │
│  - RealtimeOverlay      │    Python venv)            │  - Translator        │
│  - KrabEarTheme         │                            │  - LLMRewriter       │
│  - LaunchAgentManager   │                            │  - StateStore (NDJSON│
└─────────────────────────┘                            │  - MetricsCollector  │
                                                       │  - Flask REST :5005  │
                                                       └──────────────────────┘
```

Pipeline v2: `запись → нормализация → STT → очистка → диаризация → перевод → LLM → вставка`

---

## Voice Assistant Mode (Phase 1)

Новый таб "Разговор с AI" — полноценный голосовой помощник на основе локальных моделей. 

- **Запуск:** Right Option (двойной нажим для включения режима) или кнопка в UI
- **Engines:** Kyutai Moshi 7B (EN) + SeamlessStreaming 2.5B (RU/ES)
- **Мозг:** Qwen3-30B через Voice Gateway (OpenClaw), общая память с Krab агентом
- **Архитектура:** трёхуровневая (UI → Orchestration → Brain) с WS streaming

Полная документация: [`CHANGELOG.md`](CHANGELOG.md#phase-1-voice-assistant-mode-foundation-complete) • 
Roadmap: [`ROADMAP_ECOSYSTEM.md`](ROADMAP_ECOSYSTEM.md)

---

## STT Adapters

5 адаптеров для разных сценариев:

| Адаптер | Язык | Особенность | Размер |
|---------|------|-------------|--------|
| `mlx-whisper` (default) | EN/RU/ES/12+ | Сбалансированный, Metal GPU | 398 MB |
| **Parakeet-TDT-1.1B** | EN | Лучше всех для английского, OpenASR leader | 430 MB |
| **SenseVoice** | RU/EN/ZH | RU + эмоция, event detection | 290 MB |
| **WhisperX** | 99 языков | Timestamps + диаризация в STT | 650 MB |
| **Voxtral** | EN | STT + reasoning (экспериментально) | 1.2 GB |

Выбор модели автоматический по длительности. Детали: [`docs/PHASE4_PIPELINE_IMPLEMENTATION_PLAN.md`](docs/PHASE4_PIPELINE_IMPLEMENTATION_PLAN.md)

---

## TTS

Двухрежимная система синтеза речи:

- **Silero** — русский, быстрый, качественный (primary для RU)
- **Kokoro** — английский, натуральный (primary для EN)
- **Fallback** — macOS `say` (всегда доступна; для RU-текста без явного
  голоса использует русский голос `Milena`, а не системный дефолт — иначе
  речь звучит с заметным иностранным акцентом)

Включение: `KRAB_EAR_TTS_ENABLED=1` в окружении (env-var-only — `TTS_ENABLED`
не входит в `DEFAULT_SETTINGS`, поэтому у него нет `set_settings` IPC/UI
переключателя; менять можно только через окружение процесса, `.env`/`.secrets`
или launchd plist). Для launchd-инсталляций (`scripts/install_backend_launchagent.command`,
`scripts/install_rest_launchagent.command`) флаг `KRAB_EAR_TTS_ENABLED=1`
теперь зашит в оба plist-шаблона (`KrabEar/launchagents/ai.krab.ear.backend.plist.template`,
`KrabEar/launchagents/ai.krab.ear.rest.plist.template`) — новые установки
включают Silero/Kokoro как primary автоматически. Существующие launchd-сервисы
нужно переустановить (`scripts/install_backend_launchagent.command` /
`scripts/install_rest_launchagent.command`) или прописать переменную в
`~/Library/LaunchAgents/ai.krab.ear.*.plist` вручную, чтобы подхватить флаг.

**После любого TTS-фикса или изменения `TTS_ENABLED` оба процесса нужно
перезапустить** (backend держит IPC `synthesize_speech`, REST — `POST
/v1/tts/synthesize` для Voice Gateway; правки кода/env не подхватываются на
лету):

```bash
launchctl kickstart -k gui/$UID/ai.krab.ear.backend
launchctl kickstart -k gui/$UID/ai.krab.ear.rest
```

---

## Скрипты запуска

Одноклик команды в корне репо:

- **`Start Krab Ear.command`** — полный старт (venv + backend + агент)
- **`start_voice_assistant.command`** — запуск всех 4 сервисов (Voice Gateway, STT, TTS, UI)
- **`healthcheck_*.command`** — диагностика каждого компонента
- **`stop_*.command`** — graceful shutdown

---

## CLI

```bash
# Запуск IPC-бэкенда
source .venv_krab_ear/bin/activate
python KrabEar/main.py --data-dir ~/.krab_ear_data

# Запуск REST-сервера (порт 5005)
./start_rest_service.command
```

Любой параметр из `core/config.py` переопределяется через `KRAB_EAR_<ИМЯ>`.

---

## REST API

```bash
# Транскрипция файла
curl -X POST http://localhost:5005/transcribe \
  -F "file=@recording.wav"

# Метрики (latency p95, confidence)
curl http://localhost:5005/metrics
```

Полная документация: [`docs/REST_API_REFERENCE.md`](docs/REST_API_REFERENCE.md)
IPC-методы (195+): [`docs/IPC_API_REFERENCE.md`](docs/IPC_API_REFERENCE.md)

---

## Стек технологий

| Слой | Технология |
|---|---|
| STT | `mlx-whisper` (Metal GPU) |
| Диаризация | `pyannote.audio` + `torch` MPS |
| Перевод | офлайн-модели + кэш на диске |
| LLM | LM Studio / qwen3-4b |
| Backend | Python 3.11, Flask, Pydantic |
| Native agent | Swift 6.0, macOS 13+ |
| Хранилище | NDJSON (append-only, file-lock) |

---

## Статистика проекта

| Показатель | Значение |
|---|---|
| IPC-методов | 195+ |
| Тестов | 4 099 (0 ошибок) |
| Форматов экспорта | 9 |
| Pipeline | v2 (детерминированный) |
| Python-модулей | 152 |

---

## Документация

### Быстрые ссылки
- [Руководство пользователя (для конечного пользователя)](docs/USER_MANUAL.md)
- [Руководство пользователя (техническое)](docs/USER_GUIDE.md)
- [REST API Reference](docs/REST_API_REFERENCE.md)
- [IPC API Reference](docs/IPC_API_REFERENCE.md)
- [Архитектура](docs/ARCHITECTURE-KRAB-EAR.md)
- [Changelog](CHANGELOG.md)

### Roadmap и планы
- [Экосистема Krab (Voice/Ear/Agent)](ROADMAP_ECOSYSTEM.md) — общий roadmap
- [Roadmap Krab Ear](ROADMAP_KRAB_EAR.md) — локальные приоритеты

### Фазовые документы
- [Phase 4: Pipeline & STT адаптеры](docs/PHASE4_PIPELINE_IMPLEMENTATION_PLAN.md)
- [Phase 1: Voice Assistant Mode](CHANGELOG.md#phase-1-voice-assistant-mode-foundation-complete)

---

## Лицензия

MIT — см. `LICENSE`.

---

## Авторы

Разработано в рамках **Antigravity Agents** — экосистемы локальных AI-ассистентов для macOS.
