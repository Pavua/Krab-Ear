# Wave 440 — Backend Log Digest Enhancements

**Date:** 2026-05-22  
**Status:** Proposed → implemented in `scripts/backend_log_digest.py`

---

## Context

The existing `krab-ear-backend-log-scanner` scheduled task (`.claude/scheduled-tasks/krab-ear-backend-log-scanner/SKILL.md`) detects only:
- Generic ERROR / CRITICAL / Traceback lines
- WARNING keywords: `warmup`, `rewriter`, `circuit`, `timeout`, `reconnect`

The 2026-05-22 digest caught GigaAM cascade + LM Studio chronics but **missed 6 signal categories** documented below.

---

## New Detection Categories

### 1. MLX Subprocess Crashes (SIGKILL / SIGABRT)

**Why:** MLX watchdog (`core/mlx_subprocess.py`) kills the subprocess on GPU hang. These crashes are silent in the existing scan because they appear as INFO-level log lines, not ERRORs.

**Log patterns:**
```
[MLX] subprocess killed after timeout: SIGKILL
mlx_subprocess.*returncode=-9
mlx_subprocess.*SIGABRT
MLXWatchdog.*process.*exited with code -9
subprocess_timeout.*mlx
```

**Threshold:** ≥1 occurrence → ALERT (each kill = GPU hang event = user-visible transcription failure)

**Output section:** `## MLX Subprocess Crashes`

---

### 2. Audio Device Disconnect Mid-Recording

**Why:** `backend/recorder.py` catches `sounddevice` exceptions when the audio device disappears. Produces a WARNING with partial data. Users lose the recording silently.

**Log patterns:**
```
PortAudioError.*Input overflowed
sounddevice.*DeviceUnavailable
AudioRecorder.*device.*disappeared
recording interrupted.*device
InputOverflowError
```

**Threshold:** ≥1 occurrence → ALERT (each = recording lost)

**Output section:** `## Audio Device Interruptions`

---

### 3. Settings Backup Key Count Growth Anomaly

**Why:** `backend/settings_backup.py` takes rolling backups before each settings write. A sudden jump from 175 → 200+ keys in one session indicates either schema bloat or a rogue IPC caller running `set_settings` in a tight loop.

**Log patterns:**
```
settings.*keys.*[2-9][0-9]{2,}
SettingsBackup.*backup written.*keys=[2-9][0-9]+
settings_validator.*unknown key
unexpected.*settings.*key.*count
```

**Threshold:** Any backup with >190 keys OR key count delta >20 within 24h → ALERT

**Output section:** `## Settings Schema Anomaly`

---

### 4. REST Server 5xx Error Rate

**Why:** `backend/rest_server.py` (port 5005) serves HTTP-based transcription. 5xx errors indicate backend failures that callers don't see via IPC. Existing scan ignores Flask error logs.

**Log patterns:**
```
"[A-Z]+ /[^ ]+ HTTP/[0-9.]+" 5[0-9]{2}
rest_server.*500
rest_server.*Internal Server Error
flask.*500
POST /transcribe.*500
GET /metrics.*503
```

**Threshold:** ≥3 per 24h window → ALERT (occasional 500 is tolerable; 3+ = pattern)

**Output section:** `## REST 5xx Errors`

---

### 5. Disk Space Trajectory

**Why:** On 2026-05-22 free disk was 0.29 GB. Existing scan has no trajectory tracking — it can't distinguish "stable at 0.3 GB" from "dropped 2 GB in last hour."

**Detection approach:**
- Read last N lines of log for `DiskSpaceMonitor` entries.
- Parse the `free_gb` value over time and compute Δ per hour.
- Also check for the warning threshold log line directly.

**Log patterns:**
```
DiskSpaceMonitor.*free_gb=[0-9.]+
DiskSpaceMonitor.*free.*[0-9.]+ GB
disk.*space.*below.*threshold
free space.*[0-9.]+ GB.*warning
```

**Threshold:**
- Absolute: free_gb < 0.5 → ALERT
- Trajectory: Δ > -1.0 GB in last 24h (dropping fast) → ALERT

**Output section:** `## Disk Space Trajectory`

---

### 6. History.ndjson Growth Rate Anomaly

**Why:** `backend/state_store.py` is append-only with compaction. Sudden growth (many recordings or a compaction failure) should surface. A buggy IPC client spamming `save_history_item` can create thousands of entries in minutes.

**Log patterns:**
```
StateStore.*compaction.*items=[0-9]{4,}
history\.ndjson.*size=[0-9]{7,}
state_store.*loaded.*[0-9]{4,} items
compaction.*skipped
history.*[0-9]{4,} entries
```

**Threshold:**
- Item count > 5000 in one load → ALERT
- Compaction skipped ≥2× in 24h → ALERT
- File size > 50 MB → ALERT

**Output section:** `## History Store Growth`

---

## Implementation

A standalone Python script `scripts/backend_log_digest.py` implements all 6 categories in addition to the existing ERROR/WARNING scan. The SKILL.md for `krab-ear-backend-log-scanner` is updated to call this script instead of inline `grep` commands.

### Script location
```
scripts/backend_log_digest.py
```

### Invocation
```bash
python3 scripts/backend_log_digest.py \
  --log "/Users/pablito/Library/Application Support/KrabEar/backend.log" \
  --lines 3000 \
  --out "/Users/pablito/Antigravity_AGENTS/Krab Ear/.remember/backend-error-digest-$(date +%Y-%m-%d).md"
```

### Updated SKILL.md snippet
```
Steps:
1. Run: python3 /path/to/scripts/backend_log_digest.py --lines 3000
2. If script exits non-zero or prints ALERT lines — append to digest file.
3. Report top findings. <150 words.
```

---

## Output Format Addition

The digest file gains two new sections after existing ERROR Summary and WARNING Summary:

```markdown
## MLX Subprocess Crashes
- 2 crashes in last 24h (SIGKILL × 2)

## Audio Device Interruptions
- 0 events (healthy)

## Settings Schema Anomaly
- Max keys seen: 178 (below 190 threshold)

## REST 5xx Errors
- 0 events (healthy)

## Disk Space Trajectory
- Current free: 0.29 GB  ⚠️ BELOW 0.5 GB THRESHOLD
- 24h delta: -0.08 GB (stable)

## History Store Growth
- Last loaded: 1842 items (healthy)
- Compaction: last ran 6h ago
```

---

## Healthy vs Alert Summary Line

The script prints a single summary line first:

```
DIGEST STATUS: 1 ALERT(s) — disk_space_low | 0 NEW ERRORS | 7 WARNINGS
```

or

```
DIGEST STATUS: healthy
```
