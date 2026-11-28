# AGENTS.md — Krab Ear (для Codex, Antigravity/Gemini и любого другого агента)

## Skill index for AI assistants

Смотрите также [AGENT_SKILLS.md](docs/AGENT_SKILLS.md) перед runtime/CI/Sentry work.

Это стартовый документ для ЛЮБОГО AI-агента, впервые открывающего этот репозиторий —
Codex, Antigravity (Gemini), Cursor или человек-новичок.

- Start with the relevant current sections of docs/NOW.md, including active streams/ownership, runtime/release gates, Brain and bans. For read-only review, scope historical reads to the actual question. Before an executor's first edit, follow docs/EXECUTOR_PLAYBOOK.md §0 in full: read NOW.md completely and the assigned card, stop if no card, verify an isolated owned worktree from origin/codex/krab-ear-v2. Follow all applicable bans, stages and DoD; scoped reading never overrides §0. All agents must read applicable hard rules in CLAUDE.md and its referenced guidance before touching that area. Archives are historical evidence, not current state.

- tgrep first from the indexed root when healthy; rg is the fallback and verification for doubtful/empty results; ast-grep for structural search. For an unreachable server inspect the exact owned service/label/PID and health; never use broad pkill, start duplicate servers or restart another agent’s process.

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
  в docs/claude/architecture.md, service map).
- `KrabEar/core/engine.py` — `AudioEngine`: STT fallback-цепочка, аудио-нормализация,
  диаризация, TTS.
- `native/KrabEarAgent/HistoryPanelController.swift` + 12 extension-файлов —
  вся Swift UI-логика.
- Полный список из 70+ модулей `core/`/`backend/` с однострочным описанием
  каждого — в docs/claude/architecture.md; command reference: docs/claude/session-reference.md. Не дублирую
  здесь намеренно — это единственный источник правды, дублирование дрейфует.
- API-справочник всех IPC-методов — `docs/IPC_API_REFERENCE.md`.
- Оперативный фронт — `docs/NOW.md`. Журнал волн — `docs/ROADMAP-2026H2.md`
  (не читать как бэклог). Как работать — `docs/EXECUTOR_PLAYBOOK.md`.

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
- **LM Studio / brain (2026-09-05):** 15+ ГБ — общий слот Краба (gemma-26B), не autoload
  Ear во время Cursor/диктовки; `enforce_brain` никогда; пустой каталог ≠ Studio down.
  Канон — [`docs/NOW.md`](docs/NOW.md) §brain; Q2 Main Krab — `ANTIGRAVITY_HANDOFF/2026-09-05-krab-8080-model-routing.md`.
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

- CLAUDE.md retains the standing hard rules; docs/claude/architecture.md, patterns.md, session-reference.md and archived handoffs contain detailed guidance. Read the applicable section before changes; verify present state against the current tree.
- `docs/IPC_API_REFERENCE.md` — контракт всех IPC-методов.
- `docs/NOW.md` — что делать сейчас (одна страница).
- `docs/EXECUTOR_PLAYBOOK.md` — рельсы для слабых исполнителей.
- `docs/ROADMAP-2026H2.md` — журнал волн, не очередь задач.
- `docs/superpowers/specs/` — спеки всех волн разработки (дизайн-документы
  перед реализацией).
- `.remember/` — журналы сессий (handoff-заметки между сессиями Claude;
  полезны как история решений, не как источник правды о текущем коде).

## Context continuity
Maintain your own concise semantic handoff at meaningful work boundaries and before compaction, including exact Git SHA, task, constraints, evidence, remaining risks and ownership of active jobs/PRs. Preserve mandatory gates; link logs instead of copying them. Never overwrite another agent's handoff. Use the local context economics guide when available. A native compact may happen unexpectedly; keep essential progress current without waiting for a reminder.
