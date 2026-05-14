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
}
