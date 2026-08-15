# Krab Ear — обзор проекта (Always On)

Это правило нужно держать активированным как **Always On** в интерфейсе
Antigravity (Rules → этот файл → Activation Mode). Полная версия того же
контента — `AGENTS.md` в корне репозитория; ещё глубже — `CLAUDE.md` рядом
(история проекта, все паттерны, все живые уроки — единственный источник
правды для деталей, не дублируй его содержимое сюда).

## Кто ты и что тебе доверено

Krab Ear — локальный голосовой ассистент/транскрайбер для macOS, которым
владелец пользуется ЕЖЕДНЕВНО для живой диктовки. Живой бэкенд работает
прямо сейчас, живая история диктовок (12000+ записей). Обращайся с
прод-состоянием бережно: рестарт бэкенда — ТОЛЬКО через
`scripts/safe_backend_restart.command` (голый `launchctl kickstart -k` под
активной записью теряет диктовку безвозвратно). Перед git-операциями в
общем чекауте — `git status` (параллельная сессия может держать
несохранённый WIP, не трогай чужие незакоммиченные файлы).

## Архитектура

Два процесса, общаются через Unix-socket JSON-RPC (~360 методов):
Swift-агент (`native/KrabEarAgent/`) — хоткей, UI-панель,
accessibility-вставка текста, супервизия бэкенда. Python-бэкенд
(`KrabEar/`) — офлайн STT (`mlx-whisper` + GigaAM v3 для RU), диаризация
(`pyannote.audio`), перевод, история транскрипций.

- `KrabEar/backend/service.py` — диспетчер IPC-методов, делегирует в 18
  извлечённых сервисов.
- `KrabEar/core/engine.py` — `AudioEngine`: STT fallback-цепочка.
- `native/KrabEarAgent/HistoryPanelController.swift` + extension-файлы —
  Swift UI.
- Полный список модулей и их роль — только в `CLAUDE.md`, "Key layers".
- `docs/IPC_API_REFERENCE.md` — контракт всех IPC-методов.
- `docs/ROADMAP-2026H2.md` — актуальный план волн.

## Команды

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -v
make pre-merge-check     # ubuntu-parity гейт перед мержем — обязателен
make audit-all           # orphan imports, dead modules, purge coverage, decorative wiring
cd native/KrabEarAgent && swift build -c release
make sign                 # полная сборка + подпись Swift-агента
```

## Критичные ловушки (проверено практикой)

- **mlx-маскирование в CI**: dev-venv (Python 3.14) содержит `mlx-whisper`;
  ubuntu CI (3.12) — нет. Тест зелёный локально может быть красным на
  ubuntu. Гоняй `make pre-merge-check` на изменённых файлах перед мержем.
- **MLX не потокобезопасен**: любой вызов `mlx_whisper`/`mlx.core` — только
  под `with mlx_lock():` (`core/mlx_lock.py`). Concurrent GPU access →
  SIGSEGV. Для необязательных операций — `mlx_lock().acquire(timeout=...)`,
  не голый `with` (не должен ждать вечно на некритичной операции).
- **StateStore._lock()** — единый flock на `history.ndjson` и
  `settings.json`. Поддерживает `shared=True` для доказано чистых чтений;
  per-thread реентерабельность помнит РЕЖИМ — вложенный exclusive поверх
  удерживаемого shared громко падает, не тихо портит данные.
- **Privacy-mode gate**: любой IPC-хендлер, возвращающий текст транскрипта,
  словарь, алиасы спикеров или аналитику из истории, ОБЯЗАН гейтиться:
  `if self._cached_settings().get('privacy_mode_enabled'): return
  <EMPTY_SCHEMA_PARITY_DICT>`. Новый хендлер такого типа — новый гейт.
- **NSAlert/NSPanel — НИКОГДА `runModal()`**: модальный run loop без
  родительского окна = AppHang на Sequoia. Только `presentAlertSheet`/
  `presentPanelSheet` из `AlertHelpers.swift`.
- **Single-instance guard — НЕ kill by name/PID**: TOCTOU-дыра на macOS без
  атомарного process handle. Только POSIX flock.
- **macOS shell-ловушки**: скрипты для macOS — Bash 3.2 (нет
  `mapfile`/`readarray`/`declare -A`), BSD-утилиты (`pgrep`=ERE не BRE,
  `timeout` отсутствует). Цикл "собрать список → обработать" должен
  fail-closed на пустой список.
- **GigaAM subprocess** — воркер в отдельном venv
  (`~/.venv_krab_ear_gigaam`), stdin/stdout JSON. Смерть в простое
  диагностируется через `diagnose_and_close()` в `core/pipeline/stt_gigaam.py`.
- Полный список "Recurring bug classes" (fail-open в except, sibling-gate
  asymmetry, blocking call из async на shared event loop, non-idempotent
  webhook, read-modify-write без atomic-записи) — в `CLAUDE.md`.

## Экосистемные границы — НЕ трогать без явного разрешения

- Основной Краб (Telegram userbot, соседний репозиторий) — только через
  `~/Antigravity_AGENTS/new start_krab.command` / `new Stop Krab.command`.
  Никогда `kill -9`, `SIGHUP`, прямой запуск модуля.
- Krab Voice Gateway — соседний проект, правки — через явный бриф
  координатору, не напрямую.
- Для координации с параллельными агентами (Codex одновременно) —
  `scripts/run_agent_boundary_check.command` проверяет границы каталогов.

## Тестовая дисциплина

- TDD: сначала воспроизводящий RED-тест, потом фикс.
- `BackendService(...)` в тесте ОБЯЗАН `service.close()` в `tearDown` —
  иначе фоновые демон-треды роняют весь чанк тестов при завершении процесса.
- Тест-файлы, зависящие от изменённого source-файла, гоняй ЛОКАЛЬНО перед
  пушем — не полагайся только на CI.
