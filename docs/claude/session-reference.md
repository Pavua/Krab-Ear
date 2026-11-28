# Agent context reference (on demand)

Preserved project guidance. Read the relevant section before work; mandatory rules and gates in AGENTS.md and CLAUDE.md remain in force. Historical commands or measurements are not current-state evidence.

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
