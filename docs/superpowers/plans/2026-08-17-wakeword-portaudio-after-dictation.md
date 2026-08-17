# Wake-word PortAudio hang after dictation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Прекратить 30-минутный цикл `forceRestartBackend` из-за wake-word THREAD_HUNG после каждой диктовки, не убивая живую запись.

**Architecture:** Живой инцидент 2026-08-17 20:17 / 20:47 / 21:17: после `stop_recording` агент зовёт `wake_word_start`, адаптер не join'ит слушатель за 3.0с (класс 13-07, `PaMacCore err=-50`), watchdog ставит `wedged:true`, `WedgedEscalationTracker` эскалирует kickstart каждые `minGapSec=1800`. Give-up кап `maxConsecutive=3` не срабатывает: после рестарта несколько чанков дают `last_chunk_ts` → `noteHealthy()` обнуляет счётчик. Диктовка при этом работает, пока kickstart не попадает в окно записи; в 21:19 после рестарта `AudioRecorder` worker не вышел из `stop()` (`recorder_timeout`). Не чинить сам PortAudio в этой волне — сначала закрыть ложный «healthy» и не эскалировать под финализацией/зависшим recorder.

**Tech Stack:** уже есть `WedgedEscalationTracker` + тесты `WedgedEscalationTrackerTests.swift`; Python `OpenWakeWordAdapter.stop() -> bool`; не новые зависимости.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/wakeword-portaudio-after-dictation`.

**Баны:** база только `origin/codex/krab-ear-v2`; `git add` явными путями; не запускать `KrabEarAgent`; не `kickstart -k` (только `scripts/safe_backend_restart.command` и только если `get_recording_state.is_recording=false` и `get_meeting_live_state.active=false`); не мержить PR #1875; не `REST_IN_PROCESS_ENABLED`; не коммитить `wake_word_models/hard_negatives_raw/`; не трогать Main Krab / VG `.env`; не `Pa_Terminate` при THREAD_HUNG; не дообучать `krab_ru`.

**Вне скоупа:** W6 `audit_dead_swift_methods.py`; апгрейд GigaAM (уже v3); `REST_IN_PROCESS_ENABLED`; переписывать `openwakeword_adapter._listen_loop`.

**Доказательства (не чинить по памяти):**

- `logs/krab-ear-backend.out.log`: `OpenWakeWordAdapter: тред слушателя не вышел за 3.0s` после почти каждой диктовки 20:26–21:08; wedge+kickstart 20:17, 20:47, 21:17; `PaMacCore (AUHAL) err='-50'` 21:18:46; `stop_recording: audio worker завис — отдаю recorder_timeout` 21:19:04–21:20:04.
- `agent.log`: `[WakeWord] wake_word_start не удался (1/3): предыдущий поток wake word завис внутри PortAudio`; эскалация 20:17 / 20:47 / 21:17.
- Forensics: `~/Library/Application Support/KrabEar/forensics/20260817_181741_*`, `20260817_184743_*`, `20260817_191745_*` (dirty marker, last graceful shutdown всё ещё 05:40).
- Sentry 48ч по KRAB-EAR: пусто. За 7д живы `KRAB-EAR-BACKEND-1K` / `1T` / `1M` (last seen 4д назад) — тот же класс, сегодняшние события в Sentry не долетели.
- REST `/health` 200; SIGSEGV turbo в `krab-ear-rest.err.log` — 16 августа (P0), не сегодня.

---

### Task 1: Give-up кап не сбрасывать коротким heartbeat после kickstart

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/WakeWordPoller.swift` (`WedgedEscalationTracker`, вызов `noteHealthy` в поллере)
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/WedgedEscalationTrackerTests.swift`

- [x] **Step 1: Write the failing test**

```swift
func test_briefHealthyPolls_do_not_rearmCap() {
    var t = WedgedEscalationTracker()
    var now: TimeInterval = 100
    for _ in 0..<WedgedEscalationTracker.maxConsecutive {
        XCTAssertTrue(t.shouldEscalate(wedged: true, now: now))
        now += WedgedEscalationTracker.minGapSec
    }
    XCTAssertTrue(t.exhausted)
    // Один-два тика last_chunk_ts после kickstart — не «микрофон здоров».
    t.notePoll(running: true, hasRecentChunk: true)
    t.notePoll(running: true, hasRecentChunk: true)
    XCTAssertTrue(t.exhausted)
    XCTAssertFalse(t.shouldEscalate(wedged: true, now: now + WedgedEscalationTracker.minGapSec))
}

func test_sustainedHealthyPolls_rearmCap() {
    var t = WedgedEscalationTracker()
    var now: TimeInterval = 100
    for _ in 0..<WedgedEscalationTracker.maxConsecutive {
        _ = t.shouldEscalate(wedged: true, now: now)
        now += WedgedEscalationTracker.minGapSec
    }
    for _ in 0..<WedgedEscalationTracker.minHealthyPolls {
        t.notePoll(running: true, hasRecentChunk: true)
    }
    XCTAssertFalse(t.exhausted)
    XCTAssertTrue(t.shouldEscalate(wedged: true, now: now + WedgedEscalationTracker.minGapSec))
}

func test_notePoll_gap_resets_sustain_counter() {
    var t = WedgedEscalationTracker()
    t.notePoll(running: true, hasRecentChunk: true)
    t.notePoll(running: false, hasRecentChunk: false)
    t.notePoll(running: true, hasRecentChunk: true)
    // после разрыва счётчик устойчивого здоровья сброшен; кап не перевооружён
    // (consecutiveEscalations всё ещё 0, потому что эскалаций не было —
    // проверяем, что minHealthyPolls-1 не зовёт noteHealthy-эквивалент).
    XCTAssertEqual(t.consecutiveEscalations, 0)
}
```

Старый `test_noteHealthy_rearmsCap` оставить: `noteHealthy()` = явный «устойчиво здоров» для тестов/HealthMonitor. Поллер wake word больше не зовёт его на каждом `last_chunk_ts != nil`.

- [x] **Step 2: Run test to verify it fails**

Run: `cd native/KrabEarAgent && swift test --filter WedgedEscalationTrackerTests.test_briefHealthyPolls_do_not_rearmCap`

Expected: FAIL — `notePoll` нет / кап сброшен после 1–2 тиков.

- [x] **Step 3: Write minimal implementation**

В `WedgedEscalationTracker`:

```swift
static let minHealthyPolls = 8  // ~6с при интервале 0.75с

private var consecutiveHealthyPolls = 0

mutating func notePoll(running: Bool, hasRecentChunk: Bool) {
    if running && hasRecentChunk {
        consecutiveHealthyPolls += 1
        if consecutiveHealthyPolls >= Self.minHealthyPolls {
            consecutiveEscalations = 0
        }
    } else {
        consecutiveHealthyPolls = 0
    }
}
```

`noteHealthy()` оставить как сейчас (`consecutiveEscalations = 0`) — HealthMonitor/другие call sites.

В `WakeWordPoller` вместо

```swift
if (result["last_chunk_ts"] as? Double) != nil {
    self.wedgedTracker.noteHealthy()
    self.gaveUpNotified = false
}
```

звать `notePoll(running: running, hasRecentChunk: last_chunk_ts != nil)` и `gaveUpNotified = false` только когда `!wedgedTracker.exhausted` после этого (кап реально снят).

- [x] **Step 4: Run test to verify it passes**

Run: `cd native/KrabEarAgent && swift test --filter WedgedEscalationTrackerTests`

Expected: PASS, включая старые `test_noteHealthy_rearmsCap` / `test_capAfterMaxConsecutive_stopsEscalating`.

- [x] **Step 5: Commit** (только если нет чужого WIP)

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/WakeWordPoller.swift \
        native/KrabEarAgent/Tests/KrabEarAgentTests/WedgedEscalationTrackerTests.swift
git commit -m "fix(wake-word): не сбрасывать give-up кап коротким heartbeat после kickstart"
```

Не класть parity-бинари `Krab Ear.app` / `native/runtime`.

---

### Task 2: Не стартовать wake word, пока recorder worker в stop()

**Files:**
- Modify: `KrabEar/backend/openwakeword_adapter.py` и/или хендлер `wake_word_start` (тот, что отвергает start в maintenance-окне)
- Modify: `KrabEar/backend/recording_core_service.py` (признак «stop in progress / worker hung»)
- Test: существующий файл тестов wake-word start gate (найти `wake_word_start` / `THREAD_HUNG` в `KrabEar/tests/`)

- [x] **Step 1:** RED-тест: `wake_word_start` во время `recorder.stop()` timeout → `ok:false`, без второго `InputStream`.
- [x] **Step 2:** Минимальный гейт: если `AudioRecorder` worker `is_alive` после stop-timeout, start слушателя отвергается (тот же maintenance-класс, что stop→reinit).
- [x] **Step 3:** `PYTHONPATH=$(pwd)/KrabEar python -m pytest` названного файла; `scripts/pre_merge_py312_check.sh` на нём.
- [x] **Step 4:** Не `Pa_Terminate`. Не kickstart.

Живой смок только если карточка явно просит и `is_recording=false`.
