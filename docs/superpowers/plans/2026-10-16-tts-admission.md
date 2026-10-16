# TTS: ограниченный допуск и явные ошибки Implementation Plan

> Исполнение: superpowers:executing-plans, inline. Полномочия владельца:
> Ear fixes/tests/merge/deploy; соседние сервисы не менять.

**Goal:** не запускать несколько синтезов через IPC/REST одновременно в одном
Python-процессе и не возвращать успешное пустое аудио при отказе включённого TTS.

**Architecture:** module-global неблокирующий lock в
`TTSService.handle_synthesize_speech`, общем для обеих достижимых поверхностей.
Он удерживается до выхода синтеза и очистки его subprocess/temp-файлов.
Busy и отказ включённого синтеза возвращают inner `ok:false`, `error_code`
и пустую аудиосхему. IPC transport envelope сохраняется; REST отображает
только эти operational errors в HTTP503, validation остаётся HTTP400.

**Tech Stack:** Python threading, unittest/pytest, Flask; без новых зависимостей.

**База:** `da6c8e8d91890e8db0680e675a63e42a1ee5d815`,
`origin/codex/krab-ear-v2`. Собственный чистый worktree:
`/Users/pablito/.codex/worktrees/ear-source-closeout-20261014/Krab Ear`,
ветка `codex/ear-tts-admission-20261016`.

## Ограничения и границы

- Не менять foreign WIP canonical, настройки/TCC, историю, Keychain,
  шифрование/миграцию, LM Studio, соседние приложения и OrbStack.
- Оба TTS-флага False — намеренное отключение: прежний success `engine:none`.
- Gate ограничивает один Python-процесс; Backend и REST вместе могут
  выполнять два синтеза. Direct bytes API `synthesize_speech` остаётся
  внутренним негейтированным методом; текущий production caller — только handle.
- Не менять контракт REST STT `deadline_sec`: у default profile очередь
  и Future имеют отдельные бюджеты; call profile использует абсолютный срок.
- Не объявлять исторические AppHang/HTTP504 исправленными: причинность UNKNOWN.
- GPU/lifecycle только после свежего preflight и нового quiet ACK.

## Task 1: воспроизведение и исправление

Files: `KrabEar/backend/tts_service.py`, `KrabEar/backend/rest_server.py`,
`KrabEar/tests/test_tts_admission_20261016.py`.

- [x] RED: реальные handle двух экземпляров TTSService; блокируется только
  заменённый audio backend. Второй вызов должен быстро вернуть busy, не
  попасть в backend позднее; после завершения первого допуск переиспользуется.
- [x] RED: пустой результат включённого backend и его исключение — unavailable;
  оба выключенных флага сохраняют success none; exception не теряет permit.
- [x] RED: настоящий Flask route + настоящий TTSService: busy/unavailable503,
  validation400, disabled200. Не использовать боевые endpoint/модели/данные.
- [x] Запуск после quiet: `PYTHONPATH="$PWD/KrabEar" /Users/pablito/Antigravity_AGENTS/"Krab Ear"/.venv_krab_ear/bin/python3 -m pytest KrabEar/tests/test_tts_admission_20261016.py -q`.
- [x] Минимальный gate и schema-preserving operational results; GREEN той же команды.
- [x] Реальный loopback HTTP fixture E2E с заменой только audio backend:
  задержанный первый ответ, конкурентный503, успешный следующий, cleanup.

## Task 2: гейты и выпуск

- [x] Зависимые TTS/REST/privacy/auth/IPC tests отдельно, без больших ML-import chunks.
- [ ] `scripts/pre_merge_py312_check.sh KrabEar/tests/test_tts_admission_20261016.py`;
  при отказе harness не писать local parity PASS; hosted Ubuntu отдельно.
- [x] `make audit-all`; lint/diff check; независимый adversarial whole-diff review.
  Astra Ultra PASS на frozen production/test/IPC-doc bytes, новых P1/P2 нет.
- [ ] PR с точным head CI, non-draft, merge, оба post-merge workflow точного SHA.
- [ ] Отдельный runtime GO/HOLD; safe lifecycle и smoke только в новом quiet окне.
- [ ] Документы: source/CI/merge/deploy/live отдельно, оставшиеся UNKNOWN сохранить.

## Проверено локально

Service RED: 6 fail + disabled control PASS; REST RED: 2 fail (400 вместо503) + control PASS.
GREEN: 11 новых тестов (включая HTTP + Unix IPC), 52 TTS, 105 зависимых.
`make audit-all`: все14 scanners PASS. Flake8 и diff-check PASS.
Shared `/tmp/py312`: prefix Anaconda, MLX присутствует, pip отсутствует; harness
не запускался во избежание мутации общей среды. Local Ubuntu parity UNKNOWN;
hosted Python3.12 без MLX обязателен до merge.
Mic E2E INCOMPLETE: Cua не активирует TextEdit, windowNotFoundAtPosition;
настройки приглушения не менялись, parent PID/start/runs прежние.
