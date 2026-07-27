# R2 Task 7 — checkpoint lease и гонок записи

**Дата:** 2026-07-27

**Рабочая ветка:** `codex/user3-r2-task7-20260727`
**Рабочая копия:** `/Users/USER3/KrabEar-R2-Task7-PZiaeW/Krab-Ear`

## Статус

Task 7 реализован и прошёл целевые проверки. Это code-only checkpoint: живой
runtime основной учётной записи не запускался и не перезапускался. Полная
release-сборка и cross-binary e2e остаются обязательным Task 8 гейтом перед
merge/deploy.

## Что закрыто

- `start_request_id` — opaque идентичность start, сохраняемая в поколении и
  возвращаемая через start/state/meeting-ответы. Повтор того же owner и того же
  ID идемпотентно возвращает G1; иной ID не перехватывает чужую запись.
- Строгий stop-lease `(generation_token, source, owner_revision)`. Если новый
  клиент предъявил revision, backend атомарно отклоняет stale owner/revision
  независимо от legacy `recording_owner_enforce`.
- Pending Quick Capture cancel фиксирует intent, но не посылает tokenless stop.
  Компенсация возможна только после получения token и revision конкретного Q1.
- Потерянный ответ dictation/Quick start не ретраит side-effect start. Клиент
  читает backend-state и принимает G1 только при exact `start_request_id`.
- Late meeting promote fenced по token и revision: nil/mismatch не очищают
  pending route и не могут стереть уже опубликованную G2.
- `meeting_session_service` сохраняет lease и при `owner_mismatch` не запускает
  teardown/finished для чужой или устаревшей остановки.

## Проверка

В этом worktree успешно выполнены:

```text
PYTHONPATH=<worktree>/KrabEar <.venv_krab_ear>/bin/python -m unittest -q \
  KrabEar/tests/test_recording_generation.py \
  KrabEar/tests/test_recording_stop_gate.py \
  KrabEar/tests/test_meeting_session_service_W_C2a.py
# 100 tests — OK

cd native/KrabEarAgent
swift test --filter RecordingStopCoordinatorTests
# 40 tests — OK

swift test --filter 'MeetingLivePanelTests|HotkeyOwnerGuardTests|QuickCaptureWiringTests'
# 67 tests — OK
```

Также зелёные `swiftc -parse` затронутых Swift-файлов и `git diff --check`.
Логи тестовых double намеренно содержат имитированные ошибки recorder/CAS; итог
наборов — PASS.

## Перед Task 8

1. Прогнать `swift build -c release` и полную Swift-сьюту, когда длительная
   транскрибация основной среды не создаёт конкуренцию CPU/disk.
2. Создать и выполнить throwaway cross-binary smoke из R2-плана: dictation →
   promote meeting → meeting_stop; lost-start/cancel Quick Capture; stale
   revision stop. Нельзя направлять такой smoke в живой пользовательский data_dir.
3. После live evidence принимать merge/deploy только всей атомарной R2-волной
   Tasks 1–8; частичная выкладка запрещена.
