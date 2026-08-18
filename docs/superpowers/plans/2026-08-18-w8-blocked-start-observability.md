# W8 — наблюдаемость заблокированного wake_word_start + честный heartbeat

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:test-driven-development. Шаги — чекбоксы (`- [ ]`).

**Goal:** Закрыть два дефекта, внесённых/оставленных волной W7 (#1928): (1) состояние «recorder worker завис после stop()» стало ТИХИМ бессрочным простоем wake word + диктовки, без эскалации и без уведомления; (2) give-up кап по-прежнему снимается замороженным `last_chunk_ts`, т.е. защищает не тот подкласс, ради которого писался.

**Architecture:** Находки Fable-ревью диапазона `e425c5ee..39f92f8b` (2026-08-18), обе подтверждены построчным гейтом Opus.

*Дефект A (HIGH, регрессия инварианта 2026-08-09).* Цепочка, все звенья проверены в коде:
`AudioRecorder.stop()` таймаутит → worker физически жив (`is_worker_thread_alive=True`), `is_recording` уже False → Swift снимает паузу и шлёт `wake_word_start` → W7-гейт `_reinit_is_recording_gate` (комбинированный: запись ИЛИ живой worker, `service.py:1957-2013`) отвергает старт с reason `"recording in progress"` (`openwakeword_adapter.py:516`) → Swift сравнивает reason ТОЧНОЙ строкой (`WakeWordPoller.swift:363`), трактует как транзиентный, бюджет не жжёт, ретрай каждые 10 с вечно → сессия слушателя НИКОГДА не создаётся, а `stop()` занулил `_active_model` (`openwakeword_adapter.py:288`) → `WakeWordWatchdog._check` видит `running=False, model=None` → ветка «чистая пауза» → `_anomaly_since=None` + `_reset_episode()` (`wake_word_watchdog.py:193-199`) → staleness не копится → `wedged` недостижим → путь `DEFERRED_WORKER_HUNG` (`wake_word_watchdog.py:250-268`), построенный 2026-08-09 ИМЕННО против «тихого бессрочного простоя wake-word подсистемы» (его собственный комментарий), не достигается — до `coordinator.reinit` дело не доходит.
Параллельно новые диктовки падают `unmanaged_recording` (`recorder.py:202-206`), `AudioSelfHealer` не триггерится (он смотрит на пустой РЕЗУЛЬТАТ состоявшейся записи), `HealthMonitor` доволен (ping отвечает). Итог: до W7 состояние лечилось разрушительно, но гарантированно (kickstart ≤30 мин); после W7 — не лечится и молчит.

*Дефект B (MEDIUM).* `WakeWordPoller.swift:280`: `hasRecentChunk = (result["last_chunk_ts"] as? Double) != nil` — проверка НАЛИЧИЯ. `_last_chunk_ts` штампуется только при `flat.any()` (`openwakeword_adapter.py:800`) и зануляется лишь в stop/reinit — при зависании `stream.read()` (класс 13-07) он ЗАМЕРЗАЕТ non-nil, `running` остаётся true. Значит 8 «здоровых» тиков набираются за ~6 с у мёртвого микрофона, кап снимается, 30-минутная карусель для этого подкласса не капится. Регрессии против старого кода нет (тот снимал кап с ОДНОГО тика), но заявленная в #1928 защита не покрывает целевой класс.

**Живые улики (не по памяти):** `backend.log` 2026-08-17 `21:37:08/13/20` и `21:52:30/35/43` — `AudioRecorder worker не завершился за 3.0 с при stop()` + `stop_recording: audio worker завис — отдаю recorder_timeout`, по три подряд. Тогда гейта ещё не было (Python-половина #1928 встала 23:28), поэтому wake word стартовал поверх. Forensics `20260817_181741/184743/191745/204633`. Sentry по этому классу молчит с 08-13 (отдельная проблема, вне скоупа).

**Tech Stack:** существующие `WedgedEscalationTracker` + `WedgedEscalationTrackerTests.swift`; `WakeWordWatchdog` уже принимает `is_recording` (чистый `recorder.is_recording`, `service.py:1489`); идиома роста ts уже есть — `WakeWordDetectionTracker.shouldTrigger` (`WakeWordPoller.swift:47-60`). Новых зависимостей нет.

**База:** `origin/codex/krab-ear-v2` (`750ea3b6`). Worktree: `.claude/worktrees/stoic-haibt-629bc7`, ветка `feat/w8-wakeword-blocked-start-observability`.

**Баны:** не откатывать #1928 (направление верное); не трогать сам `_listen_loop`/PortAudio/`Pa_Terminate`; `git add` явными путями; не запускать собранный `KrabEarAgent` из worktree; не `kickstart -k` под запись (только `scripts/safe_backend_restart.command`); не мержить #1875; не `REST_IN_PROCESS_ENABLED`; не коммитить `wake_word_models/hard_negatives_raw/`.

**Вне скоупа:** молчание Sentry с 08-13; W6 `audit_dead_swift_methods.py`; починка самого PortAudio-зависания (это отдельная карточка — здесь только наблюдаемость и честность сигнала).

**🔴 Ловушка, обязательная к соблюдению:** Swift сравнивает reason ТОЧНОЙ строкой (`why == Self.recordingInProgressReason`). Новый reason БЕЗ парной правки Swift уедет в персистентную ветку → `failedStartAttempts` сгорит за 3 попытки → wake word тихо мёртв до перетыкания тумблера, т.е. хуже исходного бага. Python и Swift правятся ОДНИМ коммитом.

---

### Task 1 (дефект B): здоровый тик = РОСТ `last_chunk_ts`, а не его наличие

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/WakeWordPoller.swift` (`WedgedEscalationTracker.notePoll`, вызов в `tick`)
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/WedgedEscalationTrackerTests.swift`

- [x] **Step 1 — RED.** Тесты, падающие на текущем коде:
  `test_frozenChunkTs_doesNotRearmCap` — кап исчерпан, затем 20 тиков с ОДНИМ И ТЕМ ЖЕ ts → `exhausted` остаётся true.
  `test_growingChunkTs_rearmsCapAfterStreak` — 8+ тиков с растущим ts → `exhausted` становится false.
  `test_nilChunkTs_resetsStreak` — серия прерывается nil и начинается заново.
- [x] **Step 2 — GREEN.** `notePoll(running:chunkTs:)` принимает сам `Double?`; внутри baseline (идиома `shouldTrigger`): здоровым считается тик, где `running && ts != nil && baseline != nil && ts > baseline`; baseline обновляется всегда; `ts == nil` → baseline = nil и серия сбрасывается. Первый тик после старта серию не засчитывает (нет доказательства роста).
- [x] **Step 3 — REFACTOR/гейт.** `swift build -c release`; прогнать весь `WedgedEscalationTrackerTests`.

### Task 2 (дефект A): отдельный reason + watchdog видит заблокированный старт

**Files:**
- Modify: `KrabEar/backend/openwakeword_adapter.py` (различать причины отказа), `KrabEar/backend/wake_word_watchdog.py` (`is_worker_hung`, ветка «чистая пауза»), `KrabEar/backend/service.py` (проводка колбэка), `native/KrabEarAgent/Sources/KrabEarAgent/WakeWordPoller.swift` (новый reason в транзиентный список + WARN)
- Test: `KrabEar/tests/test_wake_word_blocked_start_observability_2026_08_18.py`, `WedgedEscalationTrackerTests.swift`

- [x] **Step 1 — RED (Python).** Тесты: (а) `handle_wake_word_start` при `is_recording=False, is_start_blocked=True` возвращает reason `"recorder worker hung"`, а при реальной записи — прежний `"recording in progress"` (контракт для Swift); (б) `WakeWordWatchdog._check` при `running=False, model=None, is_recording()=False, is_worker_hung()=True` НЕ сбрасывает эпизод и по достижении `wake_word_stale_sec` эскалирует (`wedged` + ErrorBus), а при `is_recording()=True` — сбрасывает, как раньше.
- [x] **Step 2 — GREEN (Python).** Adapter принимает оба колбэка и различает причину. Watchdog принимает `is_worker_hung`, в ветке «чистая пауза» ведёт аномалию по нему вместо безусловного `_reset_episode()`. `service.py` прокидывает `is_worker_hung=lambda: bool(recorder.is_worker_thread_alive) and not bool(recorder.is_recording)`.
- [x] **Step 3 — Swift-парность (ОБЯЗАТЕЛЬНО в том же коммите).** Константа нового reason; условие транзиентности принимает ОБА; для worker-hung — WARN однократно (видно в `agent.log`), бюджет не жжётся.
- [ ] **Step 4 — гейт.** `pytest` изменённых файлов; `scripts/pre_merge_py312_check.sh` (ubuntu-parity); `flake8` CI-командой; `make audit-all`; `swift build -c release`.

### Task 3: деплой и живая проверка

- [ ] PR в `codex/krab-ear-v2`, дождаться зелёного `krab-ear-ci` по ПОЛНОМУ SHA.
- [ ] После мержа — `scripts/build_and_deploy.command` (проверив `is_recording=false`), `launchctl kickstart -k gui/501/ai.krab.ear.agent`, parity-бинари коммитом.
- [ ] Живая проверка: `wake_word_status` даёт `running/not wedged`; в `agent.log` (только `grep -a`!) нет всплеска отказов; обновить `docs/NOW.md`.
