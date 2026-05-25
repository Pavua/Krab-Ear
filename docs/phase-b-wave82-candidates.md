# Phase B Wave 82 — ERROR_REGISTRY Candidate Audit

**Date**: 2026-05-24  
**Log analyzed**: `~/Library/Application Support/KrabEar/backend.log` (tail 5000 lines, ~2026-05-09 to 2026-05-23)  
**Registry baseline**: 49 codes (46 named + `stt.gigaam.ffmpeg_missing`, `rewriter.lm_studio_500`, `rewriter.lm_studio_stream_gpu_lost`)

---

## Summary

6 new candidates found. 0 of the 9 hunt categories from the task brief produced
patterns beyond what's already wired (recording start failures, history corruption,
MLX subprocess SIGABRT, REST 5xx, audio device disconnect, diarization MPS bus error,
file-lock contention, translation cache — all absent or already covered).
The genuine gaps are below.

---

## Candidate 1 — `system.proc_cmdline_permission` (PRIORITY: HIGH)

**Frequency**: 6 occurrences (2026-05-13)  
**Severity**: `error`  
**Sample log line**:
```
2026-05-13 14:01:18,495 [KrabEar.Backend.Service] ERROR: Ошибка метода get_memory_stats
PermissionError: [Errno 13] force permission denied (originated from sysctl(KERN_PROCARGS2) -> errno 0)
The above exception was the direct cause of the following exception:
SystemError: <built-in function proc_cmdline> returned a result with an exception set
```

**Description**: `psutil.process_iter(["cmdline"])` on macOS Sonoma/Sequoia raises
`PermissionError` for sandboxed processes when `sysctl(KERN_PROCARGS2)` is blocked by
TCC or SIP. `get_memory_stats` IPC call crashes silently — Swift analytics panel shows
blank data with no user-visible error.

**Proposed entry**:
```python
"system.proc_cmdline_permission": {
    "user_msg_ru": "Нет доступа к данным процессов — статистика памяти недоступна",
    "user_msg_en": "Process cmdline access denied — memory stats unavailable",
    "actionable": True,
    "action_id": "open_privacy_settings",
    "action_label": "Настройки конфиденциальности",
    "severity": "error",
    "dedupe_seconds": 3600,
},
```

**Wiring point**: `backend/service.py` `_handle_get_memory_stats`, catch `(PermissionError, SystemError)`.

---

## Candidate 2 — `disk.critical` (PRIORITY: HIGH)

**Frequency**: 3 occurrences (2026-05-22, disk dropped to 0.22 GB — near write failure)  
**Severity**: `critical`  
**Sample log line**:
```
2026-05-22 02:27:52,044 [KrabEar.Backend.DiskMonitor] WARNING: Дисковое пространство CRITICAL: 0.29 GB свободно
2026-05-22 11:27:54,803 [KrabEar.Backend.DiskMonitor] WARNING: Дисковое пространство CRITICAL: 0.22 GB свободно
```

**Description**: `DiskSpaceMonitor` already distinguishes WARN vs CRITICAL thresholds in
its log messages, but both are emitted under the single `disk.low_space` code (severity
`warn`). At CRITICAL levels (<1 GB) the backend is at risk of `history.write_fail` within
minutes. Needs a separate code at `critical` severity so the Swift toast uses the red
priority path and demands user action.

**Proposed entry**:
```python
"disk.critical": {
    "user_msg_ru": (
        "Критически мало места на диске ({free_gb:.1f} ГБ) — "
        "запись может прерваться. Удалите файлы немедленно."
    ),
    "user_msg_en": "Disk critically low ({free_gb:.1f} GB) — recording may fail. Delete files now.",
    "actionable": True,
    "action_id": "open_logs",
    "action_label": "Открыть папку данных",
    "severity": "critical",
    "dedupe_seconds": 300,
},
```

**Wiring point**: `backend/disk_monitor.py` — emit `disk.critical` when `free_gb < DISK_CRITICAL_GB`, keep `disk.low_space` for the warn tier.

---

## Candidate 3 — `startup.stt_model_cache_miss` (PRIORITY: MEDIUM)

**Frequency**: 2 occurrences on 2026-05-22 and 2026-05-23  
**Severity**: `warn`  
**Sample log line**:
```
2026-05-22 16:54:38,844 [KrabEar.Backend.Service] WARNING: Startup diagnostics DEGRADED —
  warnings: STT модель mlx-community/whisper-large-v3-mlx отсутствует в кэше —
  первый запуск займёт больше времени
```

**Description**: `StartupDiagnostics` enters DEGRADED state when the active Whisper model
is not yet in the HuggingFace cache (e.g. after clean install or cache eviction by another
tool). User currently sees nothing — the next transcription attempt silently waits during
model download (can be 2–10 min on a cold start). No error code exists for this state.

**Proposed entry**:
```python
"startup.stt_model_cache_miss": {
    "user_msg_ru": (
        "STT модель не загружена в кэш — первая транскрибация займёт "
        "несколько минут (загрузка модели)."
    ),
    "user_msg_en": "STT model not cached — first transcription will take several minutes.",
    "actionable": False,
    "action_id": None,
    "action_label": "",
    "severity": "warn",
    "dedupe_seconds": 86400,
},
```

**Wiring point**: `backend/service.py` `_handle_startup_diagnostics` / startup init path when `diagnostics["status"] == "DEGRADED"` and message contains "отсутствует в кэше".

---

## Candidate 4 — `stt.whisper_looping_drop` (PRIORITY: MEDIUM)

**Frequency**: 1 explicit drop + 3 repetition-loop warnings (across log)  
**Severity**: `warn`  
**Sample log lines**:
```
2026-05-18 21:51:31,613 [KrabEar.Engine] WARNING: Whisper repetition loop detected: repeated_bigram x54: тихо, тихо,
2026-05-18 21:51:32,955 [KrabEar.Backend.Service] WARNING: postprocess: drop reason=looping_artifact, len=4646,
  sample='Тихо, тихо, т Субтитры сделал DimaTorzok ...'
2026-05-18 21:51:32,955 [KrabEar.Backend.Service] WARNING: Retry transcribe с soft cleanup: raw_text len=4646,
  duration=747.2s (post-process drop'нул весь текст)
```

**Description**: `stt.repetition_loop` already covers detection, but NOT the downstream
outcome where `postprocess` drops the entire output (4646 chars) and the system retries.
The retry itself has no dedicated code — user sees no feedback that a full transcript was
silently discarded and a retry was triggered. This is distinct from the detection event.

**Proposed entry**:
```python
"stt.postprocess_drop": {
    "user_msg_ru": (
        "Транскрипция отброшена из-за артефакта повторений "
        "— повтор с мягкой очисткой."
    ),
    "user_msg_en": "Transcript dropped due to looping artifact — retrying with soft cleanup.",
    "actionable": False,
    "action_id": None,
    "action_label": "",
    "severity": "warn",
    "dedupe_seconds": 60,
},
```

**Wiring point**: `backend/service.py` where `drop reason=looping_artifact` is logged, before the retry call.

---

## Candidate 5 — `rewriter.lm_studio_circuit_cascade` (PRIORITY: LOW)

**Frequency**: 5 HALF_OPEN→OPEN transitions in 2 separate cascade storms  
**Severity**: `warn`  
**Sample log lines**:
```
2026-05-18 21:42:10,779 [KrabEar.Backend.LLMRewriter] WARNING: Circuit breaker: CLOSED -> OPEN (10 fails подряд), cooldown 60 сек
2026-05-18 21:43:16,309 [KrabEar.Backend.LLMRewriter] WARNING: Circuit breaker: HALF_OPEN -> OPEN (проба провалилась), cooldown теперь 120 сек
2026-05-18 21:45:18,470 [KrabEar.Backend.LLMRewriter] WARNING: Circuit breaker: HALF_OPEN -> OPEN, cooldown теперь 240 сек
2026-05-18 21:49:19,732 [KrabEar.Backend.LLMRewriter] WARNING: Circuit breaker: HALF_OPEN -> OPEN, cooldown теперь 480 сек
```

**Description**: `rewriter.circuit_open` fires once on `CLOSED→OPEN`, but HALF_OPEN→OPEN
"cascade" transitions (where probe attempts keep failing and cooldown doubles) are not
reported. During the 2026-05-18 storm the rewriter was silently dead for ~8 minutes with
no additional user notification after the first toast. A cascade-specific code would let
the Swift side escalate severity from `warn` to `error` after the second HALF_OPEN→OPEN.

**Proposed entry**:
```python
"rewriter.circuit_cascade": {
    "user_msg_ru": (
        "Rewriter не восстанавливается — пробы провалились "
        "(cooldown {cooldown_sec}s). LM Studio может быть перегружен."
    ),
    "user_msg_en": "Rewriter probe keeps failing — LM Studio may be overloaded.",
    "actionable": False,
    "action_id": None,
    "action_label": "",
    "severity": "error",
    "dedupe_seconds": 120,
},
```

**Wiring point**: `backend/llm_rewriter.py` circuit-breaker logic, on each `HALF_OPEN → OPEN` transition.

---

## Candidate 6 — `stt.gigaam_longform_hf_gated_cascade` (PRIORITY: LOW)

**Frequency**: 20 occurrences (recurring cascade — same audio files hit both padding_mismatch → longform → LocalEntryNotFoundError in sequence)  
**Severity**: `warn`  
**Sample log line**:
```
2026-05-22 00:27:42,888 [KrabEar.Engine] WARNING: GigaAM AudioChunker failed (108.0s):
  RuntimeError: Argument #4: Padding size should be less than the corresponding input dimension ...
2026-05-22 00:29:18,561 [KrabEar.Engine] WARNING: GigaAM transcribe failed (duration=108.0s, longform=True):
  LocalEntryNotFoundError: An error happened while trying to locate the file on the Hub ...
```

**Description**: `stt.padding_mismatch` fires on the AudioChunker failure and
`stt.gigaam_hf_cache_miss` fires on the LocalEntryNotFoundError. However, these two
always occur as a sequential pair on long audio (>60s), effectively generating 2 toasts
per failing recording. The cascade pair itself is not deduplicated — on 2026-05-22 six
files each triggered the pair = 12 toasts in a 15-minute window. A combined cascade code
with aggressive deduplication (3600s) would suppress the storm.

**Proposed entry**:
```python
"stt.gigaam_longform_unavailable": {
    "user_msg_ru": (
        "GigaAM: длинная запись не может быть обработана "
        "(модель сегментации не кеширована) — используется Whisper."
    ),
    "user_msg_en": "GigaAM longform unavailable (segmentation model not cached) — falling back to Whisper.",
    "actionable": False,
    "action_id": None,
    "action_label": "",
    "severity": "warn",
    "dedupe_seconds": 3600,
},
```

**Wiring point**: `core/engine.py` GigaAM longform fallback path — replace the double-emit with this single code.

---

## Not-Found Categories (clean bill)

| Category | Result |
|---|---|
| Recording start failures | No log entries |
| History store JSON corruption | No log entries |
| MLX SIGABRT/SIGKILL subprocess | No log entries |
| REST 5xx error rate | 1 isolated HTML 500 on 2026-05-12 — already covered by `rewriter.lm_studio_500` |
| Settings backup growth anomaly | Stable at 173 keys (normal) |
| Audio device disconnect mid-recording | No `Input overflowed` / portaudio entries |
| Diarization MPS bus error | 10 successful loads, 0 errors |
| File-lock contention | No entries |
| Translation cache miss rate | No translation cache log entries at all |
| Whisper hallucination NEW bigrams | `числа всегда` (x8) and `потому что` (x6) are new bigrams; `stt.repetition_loop` already wired for the detection — no new code needed, but bigrams could be added to hallucination_manager defaults |

---

## Priority Order for Wiring

| # | Code | Priority | Why |
|---|---|---|---|
| 1 | `disk.critical` | HIGH | 0.22 GB observed — backend was minutes from write failure with no critical-severity alert |
| 2 | `system.proc_cmdline_permission` | HIGH | 6 ERROR-level crashes in service.py; analytics panel silently blank; user has no recovery path shown |
| 3 | `startup.stt_model_cache_miss` | MEDIUM | Recurring on 2026-05-22/23; users get silent multi-minute delay on first transcription |
| 4 | `stt.postprocess_drop` | MEDIUM | Silent loss of full transcript + retry; no user feedback at all |
| 5 | `rewriter.circuit_cascade` | LOW | Already gets one `rewriter.circuit_open` toast; cascade reduces user confusion but not blocking |
| 6 | `stt.gigaam_longform_unavailable` | LOW | Toast deduplication improvement; not a new failure mode |
