# A3: различить источники stale wake-word heartbeat

**Цель:** при следующем `stale_after_reinit` увидеть, остановилось ли открытие
InputStream, чтение кадров, либо поток отдаёт только нули. Это диагностика,
не поведенческий фикс инцидента 19.09.

**База:** `origin/codex/krab-ear-v2` @ `5b2aaeb`.
**Worktree:** `codex/ear-a3-wake-diagnostics`.
**Контекст:** `.remember/CODEX_ACCEPTANCE_20260920_RU.md` §A3 (исторические
факты; локальный ignored handoff),
`docs/superpowers/specs/2026-08-23-portaudio-unkillable-read-design.md`.

**Баны:** не менять timeout, watchdog-решения, STT, Swift и настройки.
Никакой записи/рестарта продакшена, чужого WIP, новых зависимостей или
содержимого аудио/транскриптов в телеметрии. Только текущая сессия listener;
метрики при новом start должны сбрасываться, zombie generation не должна
переписывать их. Не логировать input-device name/path (может содержать PII).

## Task 1 — состояние чтения адаптера

**Files:** `KrabEar/backend/openwakeword_adapter.py`,
`KrabEar/tests/test_wake_word_heartbeat.py`.

1. RED: на фейковом потоке проверить, что после нулевого чанка heartbeat
   сохраняет свежий `last_any_chunk_ts`, но `last_chunk_ts` остаётся `None`;
   после ненулевого оба становятся свежими. Проверить `stream_opened` и
   `last_read_started_ts` во время блокирующего `read()` без ожидания его
   завершения; `last_read_completed_ts` ставится и при исключении read (даже
   если затем завис `InputStream.__exit__`). Новый start обнуляет поля;
   проверить generation-guard.
2. Run: `PYTHONPATH="$PWD/KrabEar" /Users/pablito/Antigravity_AGENTS/Krab\ Ear/.venv_krab_ear/bin/python -m pytest KrabEar/tests/test_wake_word_heartbeat.py -q`.
   Expected: новые проверки FAIL по отсутствующим полям.
3. GREEN: только скалярные поля и штампы `time.monotonic()` под уже
   существующим lock; без дополнительного ввода/вывода внутри hot loop.
4. Run ту же команду. Expected: PASS. Затем guarded-read tests PASS.

## Task 2 — снимок при двух watchdog границах

**Files:** `KrabEar/backend/wake_word_watchdog.py`,
`KrabEar/tests/test_wake_word_watchdog.py`.

1. RED: на staleness перед reinit и на `stale_after_reinit` проверить
   структурированный, ограниченный лог: `stream_opened`, возраст последнего
   любого/ненулевого чанка, возраст незавершённого read (по timestamp
   завершения, а не по timestamp последнего успешного чанка). Не печатать сами
   аудиоданные, модельный output или абсолютные monotonic-значения. Фейк
   старого heartbeat без новых полей должен оставаться допустимым.
2. Run: `PYTHONPATH="$PWD/KrabEar" /Users/pablito/Antigravity_AGENTS/Krab\ Ear/.venv_krab_ear/bin/python -m pytest KrabEar/tests/test_wake_word_watchdog.py -q`.
   Expected: новые проверки FAIL по отсутствующему диагностическому логу.
3. GREEN: один форматтер снимка, вызов только на уже существующих warning/
   escalation ветках; не менять порогов и side effects.
4. Run ту же команду. Expected: PASS.

## Общий gate и review focus

- Narrow adapter/watchdog tests, `scripts/pre_merge_py312_check.sh` на оба
  изменённых test-файла, Ruff, `git diff --check`.
- Не запускать full/ML/Swift gate под занятой памятью; если он не прошёл,
  source-ready/CI-ready не заявлять. Перед push/merge — exact-SHA CI.
- Независимый review всего diff сильной моделью: гонки lock/generation,
  отсутствие PII, не меняются ли watchdog-решения/тайминги, лог не штормит.
- Живую диктовку и wake-срабатывание документировать отдельно с владельцем;
  unit/mocked тесты не доказывают это поведение в production.
