"""Single source of truth for error code definitions.

Adding a new code:
1. Add entry here with required keys.
2. Add a regression test in test_error_codes.py.
3. Wire `error_bus.push(KrabError(code="...", ...))` at the call site.
4. If actionable, add a real handler in error_actions.py.
"""
from typing import TypedDict


class _Entry(TypedDict):
    user_msg_ru: str
    actionable: bool
    action_id: str | None
    action_label: str
    severity: str
    dedupe_seconds: int


ERROR_REGISTRY: dict[str, _Entry] = {
    # ── Layer: paste ─────────────────────────────────────────────
    "paste.ax_denied": {
        "user_msg_ru": "Не смог вставить — текст в clipboard, нажми Cmd+V",
        "actionable": True,
        "action_id": "open_privacy_settings",
        "action_label": "Открыть Privacy Settings",
        "severity": "error",
        "dedupe_seconds": 60,
    },
    "paste.app_unsupported": {
        "user_msg_ru": "Эта программа не поддерживает paste — текст в clipboard",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "info",
        "dedupe_seconds": 30,
    },

    # ── Layer: rewriter ──────────────────────────────────────────
    "rewriter.timeout": {
        "user_msg_ru": "Rewriter недоступен — raw text вставлен",
        "actionable": True,
        "action_id": "disable_rewriter",
        "action_label": "Выключить rewriter",
        "severity": "warn",
        "dedupe_seconds": 60,
    },
    "rewriter.connection_error": {
        "user_msg_ru": "LM Studio не отвечает — raw text вставлен",
        "actionable": True,
        "action_id": "disable_rewriter",
        "action_label": "Выключить rewriter",
        "severity": "warn",
        "dedupe_seconds": 60,
    },
    "rewriter.circuit_open": {
        "user_msg_ru": "Rewriter временно отключён после нескольких ошибок",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 300,
    },
    "rewriter.unavailable": {
        "user_msg_ru": "LM Studio недоступен (active probe)",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "info",
        "dedupe_seconds": 300,
    },
    # Added 2026-05-04 after observing real production failure modes for
    # gemma-4-e4b-it-mlx (Vision+Tool capable) — model emits <tool_call>
    # JSON instead of plain text editor output due to its tool-calling
    # fine-tune triggered by numbered-rule SYSTEM_PROMPT. mlx_lm's
    # stream_generate also raises UnboundLocalError mid-stream. HTTP
    # returns 200 with empty content + tool_calls field; existing guards
    # catch it but were silent before B.1.
    "rewriter.tool_calls_emitted": {
        "user_msg_ru": "Rewriter вернул tool_calls вместо текста — raw text вставлен",
        "actionable": True,
        "action_id": "disable_rewriter",
        "action_label": "Выключить rewriter",
        "severity": "warn",
        "dedupe_seconds": 60,
    },
    "rewriter.empty_response": {
        "user_msg_ru": "Rewriter вернул пустой ответ — raw text вставлен",
        "actionable": True,
        "action_id": "disable_rewriter",
        "action_label": "Выключить rewriter",
        "severity": "warn",
        "dedupe_seconds": 60,
    },
    "rewriter.parse_error": {
        "user_msg_ru": "Rewriter вернул некорректный JSON — raw text вставлен",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 120,
    },
    "rewriter.model_evicted": {
        "user_msg_ru": "LM Studio доступен, но модель выгружена из памяти",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "info",
        "dedupe_seconds": 600,
    },
    "rewriter.channel_error": {
        "user_msg_ru": "LM Studio: Channel Error — модель крашится при инференсе. Раскачка circuit breaker.",
        "actionable": True,
        "action_id": "switch_to_stable_rewriter",
        "action_label": "Переключить на qwen3-4b-abliterated",
        "severity": "warn",
        "dedupe_seconds": 30,
    },
    "rewriter.fallback_used": {
        "user_msg_ru": "Основная модель сбоит — переключились на резервную",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "info",
        "dedupe_seconds": 300,
    },

    # ── Layer: stt ───────────────────────────────────────────────
    "stt.load_fail": {
        "user_msg_ru": "Не загрузилась STT модель — переключаюсь на balanced",
        "actionable": True,
        "action_id": "switch_to_balanced_profile",
        "action_label": "Переключить на balanced",
        "severity": "error",
        "dedupe_seconds": 30,
    },
    "stt.empty_text": {
        "user_msg_ru": "Тишина — ничего не распознано",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "info",
        "dedupe_seconds": 5,
    },
    # Phase C.4 (2026-05-04) — Whisper repetition-loop hallucination.
    # Observed live: «Атакса хвостимда» × 2, «согласен да» × 70+.
    # Detected by is_likely_repetition_loop() in core/utils.py.
    "stt.repetition_loop": {
        "user_msg_ru": "Whisper зациклился — переговори фразу",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 60,
    },

    # ── Layer: diarization ───────────────────────────────────────
    "diarization.no_token": {
        "user_msg_ru": "Diarization недоступна — нужен HF token",
        "actionable": True,
        "action_id": "open_hf_token_setting",
        "action_label": "Указать токен",
        "severity": "warn",
        "dedupe_seconds": 600,
    },
    "diarization.pipeline_fail": {
        "user_msg_ru": "Diarization упала — записано как один спикер",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 60,
    },

    # ── Layer: translation ───────────────────────────────────────
    "translation.timeout": {
        "user_msg_ru": "Перевод недоступен — оригинал сохранён",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 60,
    },

    # ── Layer: mlx ───────────────────────────────────────────────
    "mlx.oom": {
        "user_msg_ru": "Не хватило памяти — выгрузи LM Studio или другие MLX-приложения",
        "actionable": True,
        "action_id": "kill_lm_studio_via_telegram",
        "action_label": "Выгрузить через Telegram",
        "severity": "critical",
        "dedupe_seconds": 5,
    },

    # ── Layer: history ───────────────────────────────────────────
    "history.write_fail": {
        "user_msg_ru": "Не удалось сохранить транскрипт — данные в tmpfile",
        "actionable": True,
        "action_id": "retry_history_save",
        "action_label": "Повторить сохранение",
        "severity": "critical",
        "dedupe_seconds": 10,
    },

    # W1749: purge_all_data partial failure — one or more secondary cleanup steps
    # (compact, transcript .md deletion, chains, archive, bookmarks, call_sessions,
    #  semantic_search) raised an exception.  The primary history tombstone step still
    #  completed, but some data may remain on disk.  Toast so the user knows and can
    #  manually inspect the data directory.  Severity=error (not critical) because
    #  the main history store is cleared; secondary residue is typically low-sensitivity.
    "history.purge_incomplete": {
        "user_msg_ru": "Очистка данных завершена частично — некоторые файлы могут остаться на диске. Проверьте каталог данных.",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "error",
        "dedupe_seconds": 30,
    },

    # Crypto-audit (2026-06-20): шифрование истории включено, но encrypt_line
    # упал → запись ушла В ОТКРЫТОМ ВИДЕ. Данные НЕ потеряны, но защита не
    # сработала — пользователь ДОЛЖЕН знать (иначе молчаливая security-регрессия).
    "history.encrypt_fail": {
        "user_msg_ru": "Шифрование записи не сработало — транскрипт сохранён в открытом виде.",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "error",
        "dedupe_seconds": 30,
    },

    # ── Layer: vocabulary ────────────────────────────────────────
    "vocabulary.load_fail": {
        "user_msg_ru": "Не загрузился словарь — STT работает без bias",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 600,
    },

    # ── Layer: ipc ───────────────────────────────────────────────
    "ipc.reconnect": {
        "user_msg_ru": "Связь с backend восстановлена",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "info",
        "dedupe_seconds": 60,
    },

    # ── Layer: hotkey ────────────────────────────────────────────
    "hotkey.conflict": {
        "user_msg_ru": "Right Option занят другим приложением",
        "actionable": True,
        "action_id": "open_hotkey_settings",
        "action_label": "Сменить hotkey",
        "severity": "warn",
        "dedupe_seconds": 300,
    },

    # LM Studio v0.3.x+ enforces Bearer token auth on /v1/* endpoints.
    # Without a token, all requests get 401 → silently degrade to raw text.
    "rewriter.unauthorized": {
        "user_msg_ru": (
            "LM Studio требует API token. "
            "Открой Settings → LM Studio API Key, или отключи auth в LM Studio Server Settings."
        ),
        "actionable": True,
        "action_id": "open_lm_studio_settings",
        "action_label": "Открыть настройки LM Studio",
        "severity": "error",
        "dedupe_seconds": 60,
    },

    # ── Wave 50: codes from routine review findings (Wave 42 routine audit) ──
    # Backend-log-scanner found 18 warmup WARNING/3 days but no dedicated
    # error code → emit as WARN-tier to make recovery actionable.
    "rewriter.warmup_failed": {
        "user_msg_ru": (
            "Rewriter не прогрелся при старте — LM Studio ещё загружается. "
            "Будет повторная попытка через минуту."
        ),
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 300,
    },

    # ── Layer: stt (additions) ───────────────────────────────────
    # MLXWatchdog timeout (BACKEND-E/F). Wave 43 raised timeout 60→120s,
    # but cold-load still occasionally exceeds. Dedicated code → toast.
    "stt.mlx_timeout": {
        "user_msg_ru": (
            "Распознавание превысило таймаут MLX. Whisper модель долго грузится — "
            "следующая попытка пройдёт быстрее."
        ),
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 120,
    },
    # GigaAM padding mismatch on long audio (>60s with specific dim). Routine
    # backend-log-scanner caught this 2026-05-10. Indicates audio chunking gap.
    "stt.padding_mismatch": {
        "user_msg_ru": (
            "GigaAM не справился с длинной записью — переключился на Whisper fallback."
        ),
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 120,
    },

    # ── Layer: diarization (additions) ───────────────────────────
    # pyannote VAD model is HF-gated (manual accept required). Without it,
    # GigaAM longform breaks. Memory: blocker_pyannote_gated_2026-04-26.md.
    "diarization.vad_gated": {
        "user_msg_ru": (
            "pyannote VAD требует ручного accept на Hugging Face — открыть страницу модели?"
        ),
        "actionable": True,
        "action_id": "open_pyannote_hf_page",
        "action_label": "Открыть HF (accept terms)",
        "severity": "warn",
        "dedupe_seconds": 3600,
    },

    # ── Layer: agent (NEW) ───────────────────────────────────────
    # Wave 42 smoke-diagnostic flagged two-binary drift (Krab Ear.app vs
    # native/runtime/KrabEarAgent diverged UUIDs). Daily watcher routine
    # in Wave 47 alerts on this — emit code so UI shows recovery hint.
    "agent.binary_drift": {
        "user_msg_ru": (
            "Bundle и runtime бинари рассинхронизированы — запустите "
            "`make release` чтобы синхронизировать (Wave 43 fix)."
        ),
        "actionable": True,
        "action_id": "open_terminal_make_release",
        "action_label": "Открыть terminal",
        "severity": "warn",
        "dedupe_seconds": 86400,  # once per day
    },

    # ── Wave 60: production findings ─────────────────────────────

    # rewriter.warmup_timeout — LM Studio warmup probe returned Timeout
    # during warmup_probe(). Distinct from rewriter.warmup_failed (generic
    # fail) — this specifically indicates a network-level timeout (LM Studio
    # alive but model JIT cold-load stalled). Observed as chronic warmup
    # log entries. Action: open LM Studio to check model state.
    "rewriter.warmup_timeout": {
        "user_msg_ru": (
            "Rewriter warmup не завершился по таймауту — LM Studio жив но модель "
            "ещё грузится. Откройте LM Studio для проверки."
        ),
        "actionable": True,
        "action_id": "open_lm_studio_settings",
        "action_label": "Открыть LM Studio",
        "severity": "warn",
        "dedupe_seconds": 120,
    },

    # disk.low_space — DiskSpaceMonitor detected free space below threshold.
    # Severity matches threshold level: warn for DISK_WARNING_GB,
    # critical for DISK_CRITICAL_GB. Action: open Logs dir so user can delete.
    "disk.low_space": {
        "user_msg_ru": (
            "Мало свободного места на диске — освободите место "
            "или удалите старые записи."
        ),
        "actionable": True,
        "action_id": "open_logs",
        "action_label": "Открыть папку логов",
        "severity": "warn",
        "dedupe_seconds": 300,
    },

    # disk.critical — DiskSpaceMonitor detected free space below DISK_CRITICAL_GB.
    # Separate from disk.low_space (warn): this fires only on critical threshold,
    # always at severity=critical. Dedupe 600s to avoid alert storm on slow crawl.
    # Wave 490 / W860 F1: wired in _push_disk_critical_error(); was missing from
    # registry causing KrabError fallback to empty user_msg_ru string.
    "disk.critical": {
        "user_msg_ru": (
            "КРИТИЧНО: меньше 1 GB на диске — срочно освободите место "
            "или удалите старые записи."
        ),
        "actionable": True,
        "action_id": "open_logs",
        "action_label": "Открыть папку логов",
        "severity": "critical",
        "dedupe_seconds": 600,
    },

    # audio.buffer_overflow — recorder.py detected sounddevice buffer overflow
    # (overflowed=True from stream.read). Indicates system load causing audio
    # chunks to be dropped. No action possible; dedupe 5s to avoid spam.
    "audio.buffer_overflow": {
        "user_msg_ru": (
            "Аудиобуфер переполнен — возможны пропуски в записи "
            "(высокая нагрузка на систему)."
        ),
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 5,
    },

    # stt.oom_model_evicted — MemoryError or OOM OSError when loading STT
    # model (separate from mlx.oom which is inference-time). This fires on
    # model init failure in the fallback chain. No recovery action; the
    # chain already falls back to next model.
    "stt.oom_model_evicted": {
        "user_msg_ru": (
            "STT модель выгружена из-за нехватки памяти — "
            "переключился на запасную модель."
        ),
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "error",
        "dedupe_seconds": 60,
    },

    # stt.gigaam_worker_timeout — GigaAM subprocess worker did not respond
    # within the timeout window; _timeout_kill() sent SIGTERM. Worker will
    # be respawned on next transcription attempt. No user action needed.
    "stt.gigaam_worker_timeout": {
        "user_msg_ru": (
            "GigaAM воркер не ответил вовремя — перезапущу на следующей записи."
        ),
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 30,
    },

    # ── Wave 64: 5 new codes from backend log analysis 2026-05-14/16 ─────

    # stt.gigaam.ffmpeg_missing — REST server startup or audio_converter.py
    # raises RuntimeError when ffmpeg binary not found in PATH. 100+ occurrences
    # historically. Dedupe 3600s (once per hour — it's a persistent env issue).
    "stt.gigaam.ffmpeg_missing": {
        "user_msg_ru": (
            "ffmpeg не найден в PATH — REST STT отключён. "
            "Установите: brew install ffmpeg"
        ),
        "actionable": True,
        "action_id": "open_logs",
        "action_label": "Открыть логи",
        "severity": "error",
        "dedupe_seconds": 3600,
    },

    # mlx.metal_assertion_failure — Metal GPU command-buffer assertion error:
    # 'IOGPUMetalCommandBuffer validate failed assertion' or
    # 'commit command buffer with uncommitted encoder'. Recovery is automatic
    # (MLX subprocess restart). No user action needed.
    "mlx.metal_assertion_failure": {
        "user_msg_ru": "Metal GPU ошибка в MLX — автоматически перезапускаю...",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "error",
        "dedupe_seconds": 60,
    },

    # mlx.semaphore_leak — multiprocessing resource_tracker warns about N
    # leaked semaphore objects at subprocess shutdown (GigaAM worker or
    # other MLX subprocess). Cosmetic: OS reclaims them. Dedupe 1800s.
    "mlx.semaphore_leak": {
        "user_msg_ru": "MLX воркер оставил незакрытые семафоры (не критично)",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 1800,
    },

    # stt.empty_audio_warning — numpy RuntimeWarning: 'Mean of empty slice'
    # or 'invalid value encountered in divide' during audio quality metrics
    # computation on a zero-length audio frame. No action; automatic.
    "stt.empty_audio_warning": {
        "user_msg_ru": "Пустой аудиофрагмент — метрики качества пропущены",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 600,
    },

    # system.malloc_env_leak — MALLOC_STACK_LOGGING env var leaked from parent
    # process to subprocess. macOS logs 'can't turn off malloc stack logging
    # because it was not enabled'. Purely cosmetic; dedupe 3600s.
    "system.malloc_env_leak": {
        "user_msg_ru": "MALLOC_STACK_LOGGING просочился в subprocess (не критично)",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "info",
        "dedupe_seconds": 3600,
    },

    # ── Wave 61: final missing codes ─────────────────────────────

    # vgw.reconnect — VGWebSocketClient disconnected from Voice Gateway and
    # is entering exponential-backoff reconnect loop. Dedupe 120s to prevent
    # spam during backoff (max backoff interval is 10s, so a single disconnect
    # can fire many log lines before reconnecting).
    "vgw.reconnect": {
        "user_msg_ru": "Голосовой шлюз отключился — переподключаемся...",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 120,
    },

    # stt.diarization_skipped — diarization was requested but unavailable at
    # inference time: either WhisperX pipeline raised (engine.py:2234) or
    # pyannote pipeline failed to initialise (engine.py:2715 — already covered
    # by diarization.pipeline_fail). This code covers the WhisperX failure path
    # which previously had no dedicated code. Dedupe 600s — one toast per session.
    "stt.diarization_skipped": {
        "user_msg_ru": "Спикеры не определены — диаризация недоступна",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "info",
        "dedupe_seconds": 600,
    },

    # rewriter.lm_studio_500 — LM Studio returned HTTP 500 with an HTML body
    # (not the specific mlx_lm token-bug handled by rewriter.mlx_token_bug).
    # Indicates LM Studio internal crash / OOM / model load failure.
    # Action: open LM Studio so user can restart the server.
    "rewriter.lm_studio_500": {
        "user_msg_ru": "LM Studio вернул HTTP 500 — попробуй перезапустить сервер",
        "actionable": True,
        "action_id": "open_lm_studio_settings",
        "action_label": "Открыть LM Studio",
        "severity": "error",
        "dedupe_seconds": 60,
    },

    # Added Wave 77 — 3 production-critical codes from Wave 151 log audit
    # (stt_gigaam.py:589 × 3829, service.py:1093 × 2779, engine.py:1046 × 68).

    # stt.gigaam_worker_crashed — _GigaAMSubprocessSession.transcribe() called while
    # is_loaded()==False (worker exited / OOM / crash). 3829 occurrences per production
    # log. Dedupe 300s — high-frequency, one toast per crash window.
    "stt.gigaam_worker_crashed": {
        "user_msg_ru": "Распознавание речи GigaAM прервано — переключение на Whisper",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "error",
        "dedupe_seconds": 300,
    },

    # ipc.rate_limit_exceeded — IPCThrottle token-bucket rejected a method call.
    # 2779 occurrences. Sentry breadcrumb only (do not flood Sentry event stream).
    # Dedupe 60s — short window, reset fast when burst subsides.
    "ipc.rate_limit_exceeded": {
        "user_msg_ru": "Превышен лимит запросов IPC",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 60,
    },

    # stt.critical_recognition_error — broad except in AudioEngine.transcribe()
    # catches unexpected crash during STT (68 occurrences). Critical severity —
    # always generates a Sentry event. Dedupe 180s to avoid cascades.
    "stt.critical_recognition_error": {
        "user_msg_ru": "Критическая ошибка распознавания речи — обратитесь к разработчику",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "critical",
        "dedupe_seconds": 180,
    },

    # ── Wave 78 (Wave 205): 5 production-discovered codes ────────────────────

    # stt.gigaam_hf_cache_miss — GigaAM longform path requires pyannote/segmentation-3.0
    # model from HuggingFace cache. When the model is not cached and network is
    # unavailable (offline_strict mode) or gated (token missing), GigaAM transcription
    # fails and falls back to Whisper. 306 production hits. Dedupe 600s — one toast
    # per session to avoid spam on repeated recordings.
    "stt.gigaam_hf_cache_miss": {
        "user_msg_ru": "GigaAM: модель pyannote не в кеше — переключение на Whisper",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 600,
    },

    # rewriter.model_unloaded — LM Studio returned HTTP 422 or 400 with body containing
    # "Model has not started loading" or "model is not loaded". Indicates the user
    # selected a model in LM Studio settings but did not load it. Action: open LM Studio
    # so user can start the model. 36 production hits.
    "rewriter.model_unloaded": {
        "user_msg_ru": "LM Studio: модель не загружена. Запусти её в LM Studio.",
        "actionable": True,
        "action_id": "open_lm_studio_settings",
        "action_label": "Открыть LM Studio",
        "severity": "error",
        "dedupe_seconds": 120,
    },

    # rewriter.output_ratio_fallback — LLM output length ratio guard triggered:
    # output was <35% or >300% of input length, indicating hallucination or chatbot
    # behaviour. Original text used as fallback. Info-level breadcrumb only —
    # the fallback is transparent to the user. 38 production hits.
    "rewriter.output_ratio_fallback": {
        "user_msg_ru": "Rewriter: соотношение длин вышло за границы — исходный текст сохранён",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "info",
        "dedupe_seconds": 30,
    },

    # stt.mlx_watchdog_hang — MLXWatchdog detected that mlx_whisper.transcribe()
    # did not finish within the configured timeout (MLX_TRANSCRIBE_TIMEOUT_SEC).
    # Metal GPU is likely stuck. Backend remains alive (fallback chain continues).
    # Critical severity so it surfaces immediately; dedupe 60s — one toast per hang.
    # 5 production hits (rare, high-impact). No direct action available from UI —
    # restart_backend action would require backend to be running to process it.
    "stt.mlx_watchdog_hang": {
        "user_msg_ru": "MLX watchdog: Metal GPU завис — переключаемся на fallback",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "critical",
        "dedupe_seconds": 60,
    },

    # stt.transcribe_failed — transcriber.transcribe() raised an unexpected exception
    # in _stop_recording_phase_c (MLX timeout, GPU hang, NaN input, OOM, etc.).
    # The audio buffer is persisted to data_dir/failed_recordings/<id>.wav for manual
    # recovery. Severity=error so it surfaces immediately in the Swift ErrorToastView.
    # Not directly actionable from UI — user must manually re-import the saved WAV.
    # Dedupe 10s to group rapid-fire crashes during a session.
    "stt.transcribe_failed": {
        "user_msg_ru": "STT: ошибка транскрипции — аудио сохранено для восстановления",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "error",
        "dedupe_seconds": 10,
    },

    # ipc.audio_device_poll_flood — list_audio_inputs / get_audio_devices called
    # more than 10 times per second. Indicates a polling loop bug in Swift UI
    # (e.g. audio device picker refreshing on every keystroke). Breadcrumb only —
    # no user-facing toast, but surfaces in Sentry crash context. Aggressive dedupe
    # 60s to suppress flood. 417 production hits.
    "ipc.audio_device_poll_flood": {
        "user_msg_ru": "IPC: слишком частые запросы списка аудиоустройств (>10/с)",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 60,
    },

    # ── Wave 82 / W905 F2: startup + process codes ───────────────────────────

    # system.proc_cmdline_permission — psutil.process_iter() raised PermissionError
    # or SystemError when reading process cmdline on macOS Sequoia (KERN_PROCARGS2
    # blocked for sandboxed processes). Causes silent failure of memory analytics.
    "system.proc_cmdline_permission": {
        "user_msg_ru": (
            "Не удалось прочитать список процессов (Sequoia блокирует KERN_PROCARGS2). "
            "Аналитика памяти недоступна."
        ),
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "error",
        "dedupe_seconds": 3600,
    },

    # startup.stt_model_cache_miss — Whisper HF model not found in local cache.
    # First transcription will stall for several minutes while downloading.
    # Recurring on 2026-05-22/23. Dedupe 86400s (1 day) — one toast per startup cycle.
    "startup.stt_model_cache_miss": {
        "user_msg_ru": (
            "Модель Whisper отсутствует в кэше — первая транскрибация задержится на минуты."
        ),
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 86400,
    },

    # ── Wave 306: LM Studio Metal GPU stream context lost ─────────────────────

    # rewriter.lm_studio_stream_gpu_lost — LM Studio returned HTTP 500 with
    # "Stream(gpu, N) in current thread" in the JSON error body. This is a
    # transient Metal/MLX internal error (GPU stream context detached from the
    # inference thread). The rewriter retries once after a 2s sleep before
    # recording a circuit failure. Severity=warn (recoverable, circuit breaker
    # handles escalation). Dedupe 180s — one toast per cluster of 12 hits.
    # 12 production hits on 2026-05-18 21:41-21:49 during gemma-4-26b-a4b session.
    "rewriter.lm_studio_stream_gpu_lost": {
        "user_msg_ru": "LM Studio: потерян Metal GPU stream — повтор через 2s",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 180,
    },

    # ── W1614 F1 HIGH: audio.max_duration_reached missing from registry ──────
    # recorder.py:257 pushes this code when the max-duration limit is hit and
    # the recording is auto-stopped. Previously the code was unknown to the
    # registry, causing ErrorBus to emit a toast with an empty user_msg_ru and
    # no dedupe window. Severity=warn (expected, user-triggered boundary),
    # actionable=False (no UI action needed — recording is already stopped).
    # Dedupe 60s: one toast per recording limit hit.
    "audio.max_duration_reached": {
        "user_msg_ru": "Запись остановлена: достигнут лимит длительности",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 60,
    },

    # ── W1231 F2 HIGH: two live callsites with unregistered codes ─────────────

    # rewriter.mlx_token_bug — mlx_lm 0.31.3 HTTP 500 UnboundLocalError on
    # 'token' persisted after one retry.  Distinct from rewriter.lm_studio_500
    # (generic 500 path).  Original text returned as fallback.  Dedupe 600s
    # so a stuck mlx_lm instance produces at most one toast per 10 min.
    "rewriter.mlx_token_bug": {
        "user_msg_ru": "LLM rewriter: внутренняя ошибка токенизации MLX. Использован оригинальный текст.",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 600,
    },

    # rewriter.gpu_stream_error — LM Studio returned HTTP 400 with
    # "There is no Stream(gpu, N) in current thread" body (Wave 171 / BACKEND-J).
    # Metal CommandStream corrupted by concurrent MLX/GigaAM GPU pressure.
    # Previously fell through to rewriter.timeout — now a distinct code so
    # Sentry can track Metal-pressure incidents separately.  Original text
    # returned as fallback.  Severity=warn (circuit breaker handles escalation).
    "rewriter.gpu_stream_error": {
        "user_msg_ru": "LLM rewriter: ошибка Metal GPU stream. Использован оригинальный текст.",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 600,
    },

    # audio.stack_wedged — AudioSelfHealer (backend/audio_selfheal.py) escalation.
    # Root cause: prod incident 2026-07-12 — a long-lived backend ended up with
    # PortAudio streams that opened without error but returned all-zero frames
    # for 9 days (dictation always "empty audio", wake word silent). The
    # self-heal already tried a soft reinit (sd._terminate()/_initialize()) once
    # and the very next recording came back empty again, so this fires instead
    # of reinit-looping. No automated action — the recommendation is a full
    # backend restart. Dedupe 300s: at most one toast per 5 min while wedged.
    "audio.stack_wedged": {
        "user_msg_ru": (
            "Аудио-стек завис (микрофон отдаёт тишину) — перезапустите Krab Ear."
        ),
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "error",
        "dedupe_seconds": 300,
    },
}
