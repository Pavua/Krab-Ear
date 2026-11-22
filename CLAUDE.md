# CLAUDE.md

> Сжато 2026-10-19: длинные уроки/измерения вынесены дословно в [`docs/CLAUDE_ARCHIVE.md`](docs/CLAUDE_ARCHIVE.md) (метка `⟶ [CLAUDE_ARCHIVE §L<n>]` у пункта). Правило и идентификаторы — здесь, история — там.

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Before heavy runtime/CI work, open [`docs/AGENT_SKILLS.md`](docs/AGENT_SKILLS.md) for shared agent skill workflows.

## Project Overview

Krab Ear is a local voice assistant/transcriber for macOS. It runs as a two-process system:
- **Native Swift agent** (`native/KrabEarAgent/`) — handles global hotkey (Right Option), UI panel, accessibility paste, and supervises the Python backend via Unix socket IPC.
- **Python backend** (`KrabEar/`) — performs offline STT via `mlx-whisper`, speaker diarization via `pyannote.audio`, translation, and manages transcription history.

The project is bilingual (RU/ES primary, EN secondary). Code comments, UI labels, and docs are in Russian.

## Телефонный STT-профиль Voice Gateway (source, 2026-09-07)

`POST /v1/stt/transcribe` с `request_profile=voice_gateway_call`: RU идёт через
подписываемый IPC `transcribe_ephemeral_call` к единственному уже загруженному
subprocess GigaAM в BackendService. Неизвестный язык сохраняется как явный auto;
фиксированные ES/EN используют общий STT с соответствующим кодом языка.
Все языки профиля идут без history/диаризации/контекста владельца/multipass.
Standalone REST по-прежнему
не создаёт GigaAM. Полный контракт и исходы —
[`docs/IPC_API_REFERENCE.md`](docs/IPC_API_REFERENCE.md#transcribe_ephemeral_call).

`CallSTTService` удерживает отслеживаемую обработку после таймаута клиента;
router/adapter pin запрещает выгрузку/подмену сессии. Privacy читается строго
через `StateStore.call_privacy_mode`; ошибка и повреждение настроек не дают
default OFF. `close()` возвращает False до drain и не закрывает используемые
pipes. Новые source gates не доказывают живую скорость/качество: Gateway-флаги
`KRAB_STT_EAR_CALL_PROFILE_ENABLED` и `KRAB_SCREENING_AUTO_LANGUAGE_ENABLED`
остаются выключенными до отдельной qualification. План:
[`2026-09-07-vg-gigaam-call-profile.md`](docs/superpowers/plans/2026-09-07-vg-gigaam-call-profile.md).

**Оперативный фронт: `docs/NOW.md`** (одна страница). Журнал волн: `docs/ROADMAP-2026H2.md` (обновлять после каждой волны; не очередь задач для исполнителей). Рельсы: `docs/EXECUTOR_PLAYBOOK.md`. Старый `docs/ROADMAP.md` — архив.

## Architecture

```
┌─────────────────────────┐    Unix socket (JSON-RPC)    ┌──────────────────────────┐
│  Swift Agent (macOS)    │ ◄────────────────────────── ►│  Python Backend           │
│  - HotkeyManager        │                              │  - IPCServer              │
│  - PasteService         │                              │  - BackendService (hub)   │
│  - HistoryPanel         │    Krab Ear.app/             │    → CallAssistSvc        │
│  - BackendSupervisor    │    (bundle wraps agent       │    → HistorySvc           │
│  - KrabEarTheme         │     + Python venv)           │    → TranslationSvc       │
│  - CollapsibleSection   │                              │    → SettingsSvc          │
│  - RealtimeOverlay      │                              │    → RecordingCoreSvc     │
│  - NotificationService  │                              │    → TextProcessingSvc    │
│  - LaunchAgentManager   │                              │    → TextScoringSvc       │
│  - SystemAudioDucking   │                              │    → AnalyticsSvc         │
│                         │                              │    → AudioAnalyticsSvc    │
│                         │                              │    → HealthCheckSvc       │
│                         │                              │    → STTManagementSvc     │
│                         │                              │    → AppleIntegrationSvc  │
│                         │                              │    → CallSessionSvc       │
│                         │                              │    → LiveSubsSvc          │
│                         │                              │  - AudioRecorder          │
│                         │                              │  - Transcriber            │
│                         │                              │  - Translator             │
│                         │                              │  - LLMRewriter            │
│                         │                              │  - StateStore (NDJSON)    │
│                         │                              │  - MetricsCollector       │
│                         │                              │  - VGWSClient             │
└─────────────────────────┘                              └──────────────────────────┘
```

### Architecture detail → moved

The service map, the full per-module catalogue (`backend/`, `core/`), the native Swift agent file map and the `.app` bundle notes were moved **verbatim** to [`docs/claude/architecture.md`](docs/claude/architecture.md). Read it before touching any backend service, IPC handler or Swift controller.

## Krab ecosystem start/stop (НЕ трогать из этого проекта!)

Основной Краб (Telegram userbot) запускается/останавливается ТОЛЬКО через:
```bash
~/Antigravity_AGENTS/new\ start_krab.command   # СТАРТ  (абсолютный путь: /Users/<you>/Antigravity_AGENTS/)
~/Antigravity_AGENTS/new\ Stop\ Krab.command    # СТОП
```

**ЗАПРЕЩЕНО:** `kill -9`, `SIGHUP`, `Restart Krab.command`, прямой `python -m src.main`.
OpenClaw Gateway: `openclaw gateway stop && sleep 2 && openclaw gateway start` (НЕ SIGHUP).

## Common Commands

### Python backend

```bash
# Activate virtualenv
source .venv_krab_ear/bin/activate

# Run IPC backend service
python KrabEar/main.py --data-dir ~/.krab_ear_data

# Run REST server (port 5005)
PYTHONPATH=$PYTHONPATH:$(pwd)/KrabEar python KrabEar/backend/rest_server.py

# Install dependencies
pip install -r KrabEar/requirements.txt
```

### Tests

```bash
# Run all tests (from repo root, with PYTHONPATH set)
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -v

# Run a single test file
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_engine_cleanup.py -v

# Run a single test case
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_engine_cleanup.py::EngineCleanupTestCase::test_removes_repeated_last_sentence -v

# Tests can also be run via unittest directly
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar/tests/test_backend_service.py -v
```

Tests use `unittest.TestCase` with fake/stub collaborators (e.g., `FakeRecorder`, `FakeTranscriber`). Integration tests create temp directories for `StateStore`. No external services required for test suite. Current count: **~6500+ passed** across 246+ test files.

### Swift agent

```bash
# Build native agent
cd native/KrabEarAgent && swift build -c release

# The compiled binary goes to native/runtime/KrabEarAgent

# Rebuild + sign Swift agent (full cycle)
cd native/KrabEarAgent && swift build -c release && cp -f .build/release/KrabEarAgent ../runtime/KrabEarAgent && codesign -s - -f ../runtime/KrabEarAgent
```

### Makefile shortcuts

```bash
# Use Makefile for common operations
make test          # Run all Python tests
make build         # Build Swift agent
make sign          # Build + copy + codesign
make lint          # Flake8 on Python backend

# Marathon / audit targets (W793)
make audit-orphans            # Orphan import check on service.py (W746/W771 guard)
make audit-orphans ARGS=--strict  # Same but also checks lowercase fn calls
make audit-handlers           # IPC handler complexity report (LOC, cyclomatic, risky calls)
make audit-handlers ARGS=--json  # Machine-readable JSON output
make dispatch-tests           # Run only dispatch-invariant test files (fast gate)
make service-loc              # Print current service.py line count
```

### One-click shortcuts
- `Start Krab Ear.command` — full launch (venv setup + agent start)
- `Update Krab Ear Agent.command` — rebuild Swift agent only
- `start_rest_service.command` — launch Flask REST API
- `scripts/repair_permissions.command` — reset TCC + re-grant Accessibility/Microphone (PR #234)
- `scripts/create_local_signing_identity.command` — create `Krab Ear Dev Local` self-signed identity for stable TCC grants (PR #235)
- `scripts/build_distribution_dmg.command` — build distribution DMG for sharing (PR #229)
- `scripts/install_agent_launchagent.command` — opt-in launchd KeepAlive for Swift agent (Wave 59 self-recovery)

### Launch app
```bash
# Open the .app bundle (production, launchd-managed backend)
open "Krab Ear.app"

# Or run agent binary directly (dev mode, manual backend)
python KrabEar/main.py --data-dir ~/.krab_ear_data &
./native/runtime/KrabEarAgent
```

### Sentry / observability
```bash
# Enable Sentry crash reporting (set DSN via IPC)
# Socket path: production = default_data_dir()/krabear.sock (see KrabEar/core/config.py)
#              dev        = ~/.krab_ear_data/backend.sock (via --data-dir flag)
python3 -c "
import os, socket, json
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
# Production socket (launchd Variant B):
sock.connect(os.path.expanduser('~/Library/Application Support/KrabEar/krabear.sock'))
sock.sendall(json.dumps({'id':'1','method':'set_settings','params':{'sentry_dsn':'https://YOUR_DSN@sentry.io/PROJECT_ID'}}).encode()+b'\n')
print(sock.recv(4096).decode())
"

# Sentry alerts also land in: ~/Library/Logs/KrabEar/sentry_errors.log (when log level=debug)
# (log dir = default_data_dir()/logs/ — matches data dir, not hardcoded)
```

### Performance benchmarks (PR #237 / #242)
```bash
# Run benchmark suite and store baseline snapshot
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_performance_benchmarks.py -v

# View regression history (stored in .perf_baselines/):
python scripts/check_performance_budget.py

# Regression gate runs automatically in CI; fails if p95 latency regresses >15%
```

### Call automation (Phase 3 — Telnyx / Twilio)
```bash
# Set provider credentials via IPC (one-time)
# Telnyx: sign up at telnyx.com → Mission Control → API Keys
# Twilio:  sign up at twilio.com → Console → Account SID + Auth Token

# Switch active provider (default: telnyx)
# set_settings { "call_provider": "telnyx" }   # or "twilio"
# set_settings { "telnyx_api_key": "KEY_...", "telnyx_phone": "+1..." }
# set_settings { "twilio_account_sid": "AC...", "twilio_auth_token": "...", "twilio_phone": "+1..." }
```

## Important Patterns

> Hard rules only. The remaining pattern notes were moved **verbatim** to [`docs/claude/patterns.md`](docs/claude/patterns.md) — read it when touching the matching area (recording, IPC, privacy gates, settings, Swift UI patterns).


- **Runtime vs static settings reads (Wave 58 lesson)**: ВСЕ startup-time reads of user-overridable settings MUST use `self._get_runtime_setting(key, default)` (lines 593-601 в `service.py`), NOT `DEFAULT_SETTINGS.get(key, default)`. The latter reads the static dict imported at module load, ignoring settings.json runtime overrides — caused chronic warmup-timeout warnings (Wave 58 fix: rewriter_warmup line 187 + stt_warmup line 229). Legit fallback usages of `DEFAULT_SETTINGS` are nested: `cached_settings.get(key, DEFAULT_SETTINGS.get(key, hardcoded))` — runtime first, static as ultimate fallback.
- **Dead code removal workflow**: extract logic into new service → add delegation calls in `BackendService.handle_request` → verify all tests pass → remove original methods from `BackendService`. Wave 65 batch 1 shipped (PR #410, 19 removed → 306 active); subsequent batches reduced count to **296 active** (post v2.0.3). **Critical audit lesson**: dead handler candidates MUST be checked in Python test scope too (grep `assert_dispatch`, `_handle_X` direct calls, `handle_request` calls) — naive Swift-only grep over-counts by ~4×. Reference: Wave 65 audit found 177 candidates but only 19 confirmed dead after full test scope check.
- **🔴 BackendService daemon-thread teardown (ubuntu-chunk flake root)**: `BackendService.__init__` starts daemon threads (`DiskSpaceMonitor`, `RecapScheduler`, `ExportScheduler`); without `self.service.close()` in `tearDown` they hit `_enter_buffered_busy` stderr-lock fatal at shutdown → `exit(1)`, failing the whole chunk. **Rule: every test instantiating `BackendService` MUST call `service.close()` in tearDown.** Reproduce only via in-process multi-file chunk run. ⟶ [CLAUDE_ARCHIVE §L555]
- **`core/pipeline/stt_sherpa.py`** — `SherpaOnnxSTTAdapter`: lazy-load через `_load_lock` (double-checked locking), graceful fallback (`is_available()`=False). MUST be factory-imported в `stt_router_factory.build_router` (`stt_sherpa_enabled`), иначе падает `audit_dead_extracted_modules`. 🔴 Новым модулям `core/pipeline/` обязателен локальный `make audit-all`; тестам нужен `--per-file-ignores` (W293 не ослаблен) ⟶ [CLAUDE_ARCHIVE §L556]
- **Single-instance guard**: гарантия — только POSIX `flock`. Startup не завершает процессы по PID из-за TOCTOU: в `SingleInstanceGuard.swift` и `main.swift` запрещены `pgrep`, `killOtherAgentInstances`, `SIGKILL`, `SIGTERM`, `kill(`. Тесты `LaunchAgentManagerTests` и `PermissionWizardTests` требуют temp `LaunchAgents` и spy `AutostartManaging`; реальные `~/Library/LaunchAgents` и `/bin/launchctl` запрещены. ⟶ [CLAUDE_ARCHIVE §L564]
- **Lazy-load lock**: adapter MUST guard'ить check-then-load per-adapter lock + double-check, иначе double-load (OOM; MLX → SIGSEGV). Locks: `ParakeetSTTAdapter._load_lock` (+`mlx_lock()`), `GigaAMAdapter._spawn_lock`/`_model_lock`. 🔴 КАЖДЫЙ load entry-point: `ParakeetSTTAdapter.warmup()` звал `parakeet_mlx.from_pretrained()` минуя locks и `_load_failed` → SIGSEGV/кирпич. **Rule: каждый `from_pretrained`/model-load site MUST идти через ОДИН locked `_load_model()`, never inline.** ⟶ [CLAUDE_ARCHIVE §L578]

- **🔴 Аварийные сигналы — только `faulthandler`, никогда Python-обработчик**: `signal.signal()` на синхронные сигналы (SIGSEGV/SIGBUS/SIGFPE/SIGILL/SIGABRT) запрещён — CPython зацикливает крэш. Только `faulthandler.enable(all_threads=True)`. В обработчиках запрещены IO/логи: только флаг (`service.main()._signal_handler`, `IPCServer.request_stop_from_signal`). Гейт: `tests/test_fault_signal_handler_2026_08_07.py`. ⟶ [CLAUDE_ARCHIVE §L580]
- **NSAlert/NSPanel sheets — NEVER `runModal()`**: a modal loop blocks main thread → AppHang. ALL `NSAlert`/`NSSavePanel`/`NSOpenPanel` MUST use non-blocking helpers in `AlertHelpers.swift`: `presentAlertSheet(_:for:completion:)`, `presentPanelSheet(_:for:completion:)` (`@MainActor`, `window==nil` → log+bail). Guarded by `test_nsAlertRunModal_onlyInAllowlistedFiles` (allowlist `PermissionWizard.swift`; knownSites `DiagnosticsTabView.swift`); new `runModal()` outside allowlist fails CI. ⟶ [CLAUDE_ARCHIVE §L594]
- **HistoryItem NaN/Inf guard (wave-28, #1707)**: `HistoryItem.from_dict()` coerces NaN/Inf `confidence`/`duration` to `0.0`. All IPC handlers that compute float fields from history (metadata_enricher, word_timing, speech_pace, noise_profiler, period_comparison, etc.) MUST wrap computed floats with `v if math.isfinite(v) else 0.0` before returning — a single NaN leaks JSON-serialization `null` to Swift and can crash numeric UI components.
- **Concurrent recording lifecycle lock**: `RecordingCoreService._rt_lock` serializes `RealtimePartialTranscriber` and `RealtimeSilenceFilter` start/stop. Any new background daemon wired to recording start/stop MUST be started/stopped under this lock to avoid races with rapid start→stop→start cycles.
- **Subprocess interpreter validation (wave-33, #1733)**: `STT_GIGAAM_VENV_PYTHON` (and any similar settings-driven subprocess path) MUST be validated via `Path.resolve().is_relative_to(Path.home())` + basename allowlist `{'python','python3','python3.12',...}` before passing to `subprocess.Popen`. An attacker can `set_settings {STT_GIGAAM_VENV_PYTHON: '/usr/bin/malicious'}` to execute arbitrary local binaries.
- **Obsidian / file-write path containment (wave-32, #1730)**: Any IPC handler writing to a user-configurable path (vault_path, export_path, etc.) MUST validate the resolved path is under `Path.home()` AND not inside known sensitive subdirs (`~/.ssh`, `~/.gnupg`, `Library/Keychains`). Enforce at the IPC boundary (handle_configure), not the internal write path.
- **Sharing/archive path containment (wave-33, #1737)**: File operations using attacker-controlled fields from persisted JSON (e.g., the shares index `filename` key) MUST use `resolved_path.is_relative_to(base_dir)` before unlink/rmtree. A planted `filename='../../x'` enables arbitrary file deletion.
- **Realtime filter settings bounds (wave-34, #1738)**: `rt_silence_check_sec`, `rt_silence_window_sec`, `realtime_silence_threshold_db`, `rt_partial_interval_sec` MUST be clamped in `__init__` AND added to `settings_validator._RANGE_FIELDS`. `Event.wait(timeout≤0)` returns immediately → CPU spin. All new real-time tunable parameters should be added to `_RANGE_FIELDS` with `(min, max)` bounds.
- **Live E2E smoke tools** — `scripts/run_e2e_smokes.command` запускает временный dev-бэкенд на temp data-dir и выполняет тесты без риска для прод-истории. `scripts/e2e_ipc_smoke.py` проверяет корректность вывода методов и CRUD, а `scripts/e2e_privacy_gates.py` — отсутствие утечек приватности. 🔴 Dev-сокет = `<data-dir>/krabear.sock` (НЕ `backend.sock`). Запускать после любых правок в `dispatch/handlers`. ⟶ [CLAUDE_ARCHIVE §L608]

## Non-goals (from PRD)

- Merging Krab/Ear/Voice into a single runtime — they remain separate projects with API boundaries.
- Krab Ear does not implement web scraping; external tool/reasoning goes through OpenClaw gateway.

## Runtime artifact directories (gitignored, local only)

These top-level directories are created at runtime and are excluded from version control via `.gitignore`. Do NOT commit them.

| Directory | Created by | Contents |
|-----------|-----------|----------|
| `.smoke_incidents/` | Smoke-test routines | Timestamped incident `.md` reports (backend down, agent missing, etc.) |
| `dist/` | `scripts/build_distribution_dmg.command` | Distribution DMG builds and staging app copies |
| `logs/` | launchd backend/REST launchagents | `krab-ear-backend.{out,err}.log`, `krab-ear-rest.{out,err}.log` |
| `data/` | E4/E5 runtime | Transcription data dir (dev mode) |
| `.claire/` | Claire agent sessions | Parallel agent session state (same category as `.ralphy/`, `.remember/`) |
| `.coordination/` | Autonomous cycle scripts | Agent boundary snapshots |
| `.benchmarks/` | pytest-benchmark | Benchmark result files |
| `.hypothesis/` | Hypothesis property tests | Auto-managed; has internal `*` gitignore |
| `.ruff_cache/` | ruff linter | Auto-managed; has internal `*` gitignore |

## Working guidelines for Claude sessions

### 🔴 Shell-совместимость гейтов: macOS = Bash 3.2 + BSD-утилиты (2026-07-23, два живых случая)

Два «инструмента, которые выглядели рабочими, но не работали НИКОГДА» — найдены в один день,
класс один: shell-конструкция, валидная в dev-окружении, но не в целевом.
1. **`ensure_agent_running.command`**: `pgrep` на macOS использует **ERE**, а в паттерне стояла
   BRE-альтернация `\|` → искался литерал `KrabEarAgent|native`. 12 подряд `FAIL agent still
   absent` и ни одного успеха при ЖИВОМ агенте (+ лишний `open` при работающем агенте →
   риск дубля против SingleInstanceGuard). Три смок-отчёта подряд диагностировали это как
   «мало 15 секунд» — symptom-fix, который не помог бы.
2. **macOS CI (`ci.yml`) не запускал Python-тесты вообще**: раннер даёт **Bash 3.2**, где нет
   `mapfile`/`readarray` → `files` пуст → все 16 чанков пропущены → job **SUCCESS**. Логи:
   `mapfile: command not found` / `Total test files: 0`. GNU `timeout` на macOS тоже отсутствует
   (нужен `coreutils`/`gtimeout`).

**Правила**: (а) в CI-шагах и `.command`-скриптах для macOS — только Bash 3.2-совместимое
(никаких `mapfile`, `readarray`, `declare -A`, `${var^^}`); (б) всякий цикл «собрать список →
обработать» обязан иметь **fail-closed** гард на пустой список (`[ "$n" -eq 0 ] && exit 1`),
иначе пустота читается как успех; (в) BSD vs GNU: `pgrep`=ERE, `sed -i` требует суффикс,
`timeout` отсутствует — проверяй утилиту в ЦЕЛЕВОЙ ОС, а не только локально;
(г) для «сторожевых» скриптов пиши тест, который поднимает реальный процесс/состояние и
проверяет РЕАЛЬНУЮ утилиту паттерном, извлечённым из скрипта (`test_ensure_agent_running_contract.py`,
`test_safe_backend_restart_contract.py` — образцы).

### 🔴 Рестарт backend в проде — ТОЛЬКО через safe-скрипт (инцидент 2026-07-22)

`launchctl kickstart -k gui/501/ai.krab.ear.backend` под АКТИВНОЙ записью безвозвратно теряет
диктовку (аудио живёт в памяти backend-процесса; GUI получает «Backend не ответил за таймаут»).
Живой инцидент: деплой приёмки codex сделал kickstart ровно в момент диктовки владельца — две
записи потеряны. **Правило: для рестарта прод-backend использовать
`scripts/safe_backend_restart.command` (проверяет `get_recording_state`/`get_meeting_live_state`
через IPC, отказывается под записью/встречей, `--wait N` — подождать, `--with-rest` — + REST-юнит,
после kickstart ждёт ping-готовность). Голый kickstart — только когда сокет уже мёртв/завис**
(скрипт сам это определяет и пропускает проверку). UX-хвост инцидента в основном ЗАКРЫТ волной
R2 «владение записью» (конец июля): `RecordingStopCoordinator` ретраит транспорт (2×) и поллит
`stop_in_progress` (до 5 мин), сохраняет generation-токен (`recordingStopRecoveryPending`) и
повторяет стоп на следующем нажатии хоткея; backend `stop_recording` идемпотентен (replay
терминального ответа по токену), аудио при рестарте спасает R1 rescue-скан («Восстановленные
записи» + тост `audio.recording_rescued`). Оставшаяся дельта (проверено разведкой 2026-08-11):
сохранённый стоп НЕ дострелится сам после реконнекта — только по ручному нажатию.

### 🔴 Самоблокировка через передачу работы в пул потоков (класс, 02.09.2026)

Держатель берёт `mlx_lock` в своём потоке и внутри удержания отдаёт работу в
`ThreadPoolExecutor`; рабочий поток берёт ТОТ ЖЕ лок и ждёт того, кто его
породил. `RLock` реентерабелен ТОЛЬКО для своего потока — передача в пул это
ломает. Симптом в проде: `GigaAM-MLX: mlx_lock занят дольше 25.0с` на каждой
итерации превью, «preview worker не завершился за 1.5 с», диктовка 90с на 30с
речи и «Все доступные STT-движки вышли из строя».

Оба известных места закрыты: превью (`transcriber.transcribe_preview` — лок
отпускается сразу после пробы занятости, PR #1972: 1с вместо 90с на живом
замере) и `bulk_reprocess.py` (внешний захват снят целиком, PR #1974). Третьего
сиблинга нет: прочие держатели `mlx_lock` (`audio_lang_id`, адаптеры,
`_transcribe_model`, `scripts/debug_whisper.py`) зовут MLX сами, без делегации.

Снятие в bulk потребовало пройти ВСЕ MLX-пути внутри `engine.transcribe` и
доказать, что каждый берёт лок сам, в своём потоке: `_transcribe_model` (whisper
+ RU-finetune, оба `submit`), GigaAM-MLX `_infer_chunk`, parakeet, voxtral,
`AudioLanguageID._run_detect`, `set_quality_profile`, пост-STT `mx.clear_cache()`;
пути мимо адаптеров и мимо `_transcribe_model` (`_transcribe_remote` — сеть,
диаризация — PyTorch) MLX не трогают.

🔴 **Межпроцессный flock самоблокируется так же** (замерено): другой поток того
же процесса, открыв НОВЫЙ fd на тот же файл, не получает `LOCK_EX` — flock
привязан к описанию открытого файла, не к процессу.

Правило: захват лока перед вызовом, который уходит в другой поток, — всегда
подозрителен. Регресс-тест обязан проверять захват ИЗ ДРУГОГО ПОТОКА: для
своего потока RLock реентерабелен, и баг невидим. Гард на такой инвариант пишем
разбором AST, а не подстрокой: честный комментарий про `mlx_lock` в исправленном
файле роняет подстрочную проверку (напоролись при PR #1974).

### MLX thread-safety in any session

При любой работе с mlx-whisper / MLX-based STT — обязательно оборачивать ALL MLX inference в `with mlx_lock():` context manager. MLX не потокобезопасен и concurrent GPU access вызывает SIGSEGV. (See "MLX thread-safety" in Important Patterns section for details и примеры.)


### Moved workflow notes

CI runner boundary, status dashboard, sub-agent model selection, free-breadth workforce, Gemini design rule, TCC troubleshooting, parallel PR workflow, dead-handler audit method and Voice Assistant Mode notes live **verbatim** in [`docs/claude/workflows.md`](docs/claude/workflows.md). Read it before delegating work, touching TCC/permissions, or using Gemini/agy.
