# AGENTS.md — Krab Ear (для Codex, Antigravity/Gemini и любого другого агента)

Это стартовый документ для ЛЮБОГО AI-агента, впервые открывающего этот репозиторий —
Codex, Antigravity (Gemini), или человек-новичок. Читай ЭТОТ файл первым: он
даёт ориентацию за 2 минуты и указывает, где искать глубже. Полная история
(200+ волн разработки, все находки, все инциденты) — в `CLAUDE.md` рядом; это
живой журнал проекта, не только Claude-специфичные инструкции. Читай его целиком,
когда нужен полный контекст перед серьёзной правкой — он большой (~160 КБ), но
каждый раздел плотный и проверенный практикой.

## Кто ты и что тебе доверено

Ты работаешь над Krab Ear — локальным голосовым ассистентом/транскрайбером
для macOS, который владелец использует ЕЖЕДНЕВНО для диктовки в реальном
времени. Живые данные, живая история диктовок (12000+ записей), живой бэкенд,
работающий прямо сейчас. Обращайся с прод-состоянием бережно: перед рестартом
бэкенда — `scripts/safe_backend_restart.command` (не голый `kickstart -k`, он
рвёт активную диктовку), перед git-операциями в общем чекауте — `git status`
(другая параллельная сессия может держать несохранённый WIP).

## Что это за проект

Два процесса: Swift-агент (`native/KrabEarAgent/`) — глобальный хоткей,
UI-панель, accessibility-вставка текста, супервизия бэкенда; Python-бэкенд
(`KrabEar/`) — офлайн STT (`mlx-whisper` + GigaAM v3 для RU), диаризация
(`pyannote.audio`), перевод, история транскрипций. Общаются через Unix-сокет
JSON-RPC (~360 методов). Проект билингвальный (RU/ES основные, EN вторичный) —
код, комментарии и документация на русском.

```
Swift Agent (macOS)  ◄── Unix socket JSON-RPC ──►  Python Backend
```

## С чего начать в коде

- `KrabEar/backend/service.py` — `BackendService`, диспетчер ~360 IPC-методов,
  делегирует в 18 извлечённых сервисов (`CallAssistService`, `HistoryService`,
  `TranslationService`, `RecordingCoreService`, и т.д. — полный список и роли
  в `CLAUDE.md` под "Service map").
- `KrabEar/core/engine.py` — `AudioEngine`: STT fallback-цепочка, аудио-нормализация,
  диаризация, TTS.
- `native/KrabEarAgent/HistoryPanelController.swift` + 12 extension-файлов —
  вся Swift UI-логика.
- Полный список из 70+ модулей `core/`/`backend/` с однострочным описанием
  каждого — в `CLAUDE.md`, раздел "Key layers inside KrabEar/". Не дублирую
  здесь намеренно — это единственный источник правды, дублирование дрейфует.
- API-справочник всех IPC-методов — `docs/IPC_API_REFERENCE.md`.
- Текущий план волн/приоритетов — `docs/ROADMAP-2026H2.md` (живой документ,
  обновляется после каждой волны).

## Команды

```bash
# Тесты (из корня репо, с PYTHONPATH)
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -v

# Один файл / один тест
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_X.py -v
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_X.py::ClassName::test_method -v

# Перед мержем — ubuntu-parity гейт (ловит mlx-масштаб false-green, см. ниже)
make pre-merge-check
# или точечно:
scripts/pre_merge_py312_check.sh KrabEar/tests/test_X.py

# Полный аудит-набор (orphan imports, dead modules, purge coverage, decorative wiring...)
make audit-all

# Swift-агент
cd native/KrabEarAgent && swift build -c release

# Полный цикл сборки + подпись
make sign
```

## Критичные ловушки (проверено практикой, не теория)

Каждый пункт стоил реального инцидента. Прочитай перед тем, как трогать
соответствующую область.

- **mlx-маскирование в CI**: dev-venv (`.venv_krab_ear`, Python 3.14) содержит
  `mlx-whisper`; ubuntu CI (Python 3.12) — нет. Тест, зелёный локально, может
  быть красным на ubuntu. Гоняй `make pre-merge-check` на изменённых файлах
  перед мержем.
- **MLX не потокобезопасен**: любой вызов `mlx_whisper`/`mlx.core` — только
  под `with mlx_lock():` (`core/mlx_lock.py`, реентерабельный RLock).
  Concurrent GPU access → SIGSEGV. Для необязательных операций (смена
  профиля) используй `mlx_lock().acquire(timeout=...)`, не голый `with` —
  не блокирующий лок не должен ждать вечно на некритичной операции.
- **StateStore._lock()** — единый flock на `history.ndjson` И `settings.json`.
  Поддерживает `shared=True` (LOCK_SH) для доказано чистых чтений; per-thread
  реентерабельность помнит РЕЖИМ — вложенный exclusive поверх удерживаемого
  shared громко падает, не тихо портит данные.
- **Privacy-mode gate**: любой IPC-хендлер, возвращающий текст транскрипта,
  словарь, алиасы спикеров или аналитику из истории, ОБЯЗАН гейтиться в
  начале: `if self._cached_settings().get('privacy_mode_enabled'): return
  <EMPTY_SCHEMA_PARITY_DICT>`. Новый хендлер такого типа — новый гейт.
  `handle_purge_all_data` обязан подчищать любое новое персистентное хранилище.
- **NSAlert/NSPanel — НИКОГДА `runModal()`**: модальный run loop без
  родительского окна = AppHang на Sequoia. Только `presentAlertSheet`/
  `presentPanelSheet` из `AlertHelpers.swift`. Гейтится CI-тестом.
- **Single-instance guard — НЕ kill by name/PID**: TOCTOU-дыра на macOS без
  атомарного process handle. Только POSIX flock. Не добавляй обратно
  process-killing логику в `SingleInstanceGuard.swift`.
- **macOS shell-ловушки**: `.command`-скрипты и CI-шаги для macOS — Bash 3.2
  (нет `mapfile`/`readarray`/`declare -A`), BSD-утилиты (`pgrep`=ERE не BRE,
  `timeout` отсутствует). Каждый цикл "собрать список → обработать" должен
  fail-closed на пустой список, иначе пустота читается как успех.
- **Рестарт прод-бэкенда — только `scripts/safe_backend_restart.command`**:
  голый `launchctl kickstart -k` под активной записью теряет диктовку
  безвозвратно (аудио живёт в памяти процесса).
- **GigaAM subprocess** — воркер живёт в отдельном venv
  (`~/.venv_krab_ear_gigaam`), общение через stdin/stdout JSON. Смерть в
  простое (не во время запроса) исторически была невидима для диагностики —
  чинится в `diagnose_and_close()` (см. `core/pipeline/stt_gigaam.py`).
- **Recurring bug classes** (полный список с примерами в `CLAUDE.md` под
  "Recurring bug classes"): fail-open в except-ветке safety-проверки,
  sibling-gate asymmetry (одна ветка починена, другая унаследует тот же баг
  позже), blocking call из `async def` на shared event loop, non-idempotent
  webhook без dedup-guard, read-modify-write без atomic-записи и fail-safe.

## Экосистемные границы — НЕ трогать без явного разрешения

- Основной Краб (Telegram userbot, соседний репозиторий) запускается ТОЛЬКО
  через `~/Antigravity_AGENTS/new start_krab.command` / `new Stop Krab.command`.
  Никогда `kill -9`, `SIGHUP`, `Restart Krab.command`, прямой запуск модуля.
- Krab Voice Gateway — соседний проект, читается для интеграции, правки —
  через явный бриф координатору, не напрямую.
- Для многоагентной параллельной работы (Codex + Antigravity одновременно) —
  посмотри `ANTIGRAVITY_HANDOFF/` (устаревшие спринт-файлы, но
  `scripts/run_agent_boundary_check.command` всё ещё живой инструмент для
  проверки границ каталогов между агентами) и раздел "Worker orchestration"
  в `CLAUDE.md`.

## Тестовая дисциплина

- TDD: RED (тест падает по правильной причине) → GREEN. Баг — сначала
  воспроизводящий тест, потом фикс.
- `BackendService(...)` в тесте ОБЯЗАН `service.close()` в `tearDown` —
  иначе фоновые демон-треды роняют весь чанк тестов при завершении процесса
  (хронический класс, стоил нескольких красных CI).
- Тест-файлы, зависящие от изменённого `source`-файла, гоняй ЛОКАЛЬНО перед
  пушем — не полагайся только на CI.
- Каждый extracted-модуль в `core/pipeline/` — `make audit-all` перед мержем
  (гейт не ловится линтером/тестами, только этими аудит-скриптами).

## Куда смотреть за деталями

- `CLAUDE.md` — полная история проекта, service map, все паттерны, все
  живые уроки. Единственный источник правды для деталей.
- `docs/IPC_API_REFERENCE.md` — контракт всех IPC-методов.
- `docs/ROADMAP-2026H2.md` — актуальный план и приоритеты.
- `docs/superpowers/specs/` — спеки всех волн разработки (дизайн-документы
  перед реализацией).
- `.remember/` — журналы сессий (handoff-заметки между сессиями Claude;
  полезны как история решений, не как источник правды о текущем коде).
