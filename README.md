# Krab Ear — Локальный голосовой ассистент для macOS

> Офлайн-транскрипция, перевод и авто-вставка текста одним нажатием клавиши.
> Работает полностью на устройстве. Никаких облачных сервисов, никакой телеметрии.

---

## Возможности

- 🎙️ **Офлайн STT** — распознавание речи через `mlx-whisper` на Metal GPU (Apple Silicon)
- 🌐 **Перевод** — RU↔ES, EN→RU, Auto, Bilingual; глоссарий с автодополнением
- 👥 **Диаризация** — определение спикеров через `pyannote.audio`, ускорение на MPS
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

## Быстрый старт

```bash
# 1. Клонируйте репозиторий
git clone https://github.com/antigravity/krab-ear.git && cd "Krab Ear"

# 2. Запустите одним файлом — создаст venv, установит зависимости, запустит агент
open "Start Krab Ear.command"

# 3. Нажмите Right Option — начнётся запись; повторное нажатие остановит и вставит текст
```

**Требования:** macOS 13+, Apple Silicon (M1–M4), Python 3.11+.

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

- [Руководство пользователя](docs/USER_GUIDE.md)
- [REST API Reference](docs/REST_API_REFERENCE.md)
- [IPC API Reference](docs/IPC_API_REFERENCE.md)
- [Архитектура](docs/ARCHITECTURE.md)
- [Changelog](docs/CHANGELOG.md)

---

## Лицензия

MIT — см. `LICENSE`.

---

## Авторы

Разработано в рамках **Antigravity Agents** — экосистемы локальных AI-ассистентов для macOS.
