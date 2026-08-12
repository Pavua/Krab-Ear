"""Слой транскрибации backend-сервиса Krab Ear.

Класс Transcriber является высокоуровневым интерфейсом для AudioEngine,
позволяя переключать профили качества и управлять контекстом (словарями).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, TYPE_CHECKING

from core.engine import AudioEngine
from core.mlx_lock import mlx_lock

if TYPE_CHECKING:
    from backend.llm_rewriter import LLMRewriter

logger = logging.getLogger("KrabEar.Backend.Transcriber")

# 2026-08-01: бюджет ожидания mlx_lock для BEST-EFFORT превью.
# Финальная транскрибация держит лок десятки секунд (живой замер: 26.98 s с
# ретраями), и неограниченный захват превращал вспомогательное превью в
# блокировщик основной записи. Значение меньше интервала превью (3 s): не
# достался лок — пропускаем итерацию, следующая попытка придёт своим чередом.
PREVIEW_MLX_LOCK_TIMEOUT_SEC = 1.0


class Transcriber:
    """Обёртка над AudioEngine для удобного вызова из API и IPC."""

    def __init__(
        self,
        engine: AudioEngine | None = None,
        llm_rewriter: Optional["LLMRewriter"] = None,
        settings_get: Optional[Callable[[str, Any], Any]] = None,
    ) -> None:
        """Инициализация.

        Args:
            engine: опциональный AudioEngine. Если None — создаётся новый с
                    инжекцией llm_rewriter и settings_get.
            llm_rewriter: D.10a LLM клиент для post-cleanup rewrite'а (прокидывается в AudioEngine).
            settings_get: callback для runtime toggle'ов (прокидывается в AudioEngine).
        """
        if engine is None:
            self.engine = AudioEngine(llm_rewriter=llm_rewriter, settings_get=settings_get)
        else:
            self.engine = engine
            if llm_rewriter is not None and engine._llm_rewriter is None:
                engine._llm_rewriter = llm_rewriter
            if settings_get is not None:
                engine._settings_get = settings_get

    def transcribe(
        self,
        audio_data: Any,
        quality_profile: str = "balanced",
        cleanup_profile: str = "soft",
        domain: str = "casual",
        extra_vocabulary: list[str] | None = None,
        lang_hint: str | None = None,
        history_context: list[Any] | None = None,
        stt_hotwords: list[str] | None = None,
        settings: dict | None = None,
        diarize: bool | None = None,
        skip_vad_prefilter: bool = False,
        silence_ranges: list[tuple[float, float]] | None = None,
        progress_callback: Any | None = None,
        context_free: bool = False,
        single_pass: bool = False,
    ) -> dict[str, Any]:
        """Транскрибирует аудио с учётом выбранного профиля и контекста.

        Args:
            lang_hint: ISO 639-1 код языка или None/"auto" для авто-определения whisper'ом.
            history_context: Последние HistoryItem'ы для построения initial_prompt в Whisper.
            stt_hotwords: Пользовательские термины для Glossary-префикса в initial_prompt.
            settings: Опциональный dict настроек текущего запроса (для проверки
                      diarization_enabled + HF_TOKEN). Если None — проверка не производится.
            diarize: Явное управление диаризацией для текущего вызова. None = использовать
                     глобальный settings.DIARIZATION_ENABLED. Если settings передан и
                     diarization_enabled=True, но HF_TOKEN отсутствует — переопределяется в False.
            silence_ranges: Диапазоны тишины (start_sec, end_sec) от RealtimeSilenceFilter.
                            Если указаны, обнуляет тихие участки аудио перед STT.
            context_free: 2026-08-12, живой инцидент утечки TRANSCRIBE_PROMPT в
                          live-субтитры чужого видео. True → engine.transcribe получает
                          пустой initial_prompt (ни инструкции, ни истории владельца,
                          ни hotwords) — см. core/engine.py. Отдельный от is_preview
                          флаг: is_preview дополнительно гейтит диаризацию/loop-детектор/
                          LLM-passes, которые live subs не должны терять.
            single_pass: 2026-08-12, живой инцидент 9.49с на окно live-субтитров.
                          True → engine.transcribe отключает confidence-driven multi-pass
                          retry и request-local fallback на Whisper после пустого GigaAM —
                          см. core/engine.py::AudioEngine.transcribe. По умолчанию False —
                          путь диктовки не меняется.
        """
        # Phase B.1 — guard: check HF_TOKEN before delegating to engine.
        # If diarization is requested (explicitly or via settings dict) but token
        # is missing, push diarization.no_token and override diarize=False so the
        # engine skips diarization gracefully. Defense in depth: engine.py still
        # logs its own warning inside _maybe_run_diarization.
        if settings is not None:
            _diarize_intent = diarize if diarize is not None else settings.get("diarization_enabled", False)
            if _diarize_intent:
                if not self._push_diarization_no_token_if_needed(settings):
                    diarize = False
        self.engine.set_quality_profile(quality_profile)
        return self.engine.transcribe(
            audio_data,
            cleanup_profile=cleanup_profile,
            is_preview=False,
            domain=domain,
            extra_vocabulary=extra_vocabulary,
            lang_hint=lang_hint,
            history_context=history_context,
            stt_hotwords=stt_hotwords,
            diarize=diarize,
            skip_vad_prefilter=skip_vad_prefilter,
            silence_ranges=silence_ranges,
            progress_callback=progress_callback,
            context_free=context_free,
            single_pass=single_pass,
        )

    def transcribe_preview(self, audio_data: Any, quality_profile: str = "balanced") -> dict[str, Any]:
        """Быстрая транскрибация для realtime-превью (всегда в balanced режиме).

        Оборачивается в mlx_lock() для атомарности (W1364 fix): переключение
        профиля и инференс обязаны быть неделимы относительно других MLX-вызовов.

        Захват лока — ОГРАНИЧЕН по времени (2026-08-01). Превью — best-effort:
        ждать GPU десятки секунд ради строки, которая к моменту готовности уже
        устареет, бессмысленно, а вреда от ожидания много. Живой инцидент:
        финальная транскрибация держала mlx_lock 26.98 s (ретраи по низкой
        уверенности через несколько моделей), воркер превью всё это время висел
        на захвате — и не мог отреагировать на остановку, потому что его
        проверки `_stop_event` стоят ДО и ПОСЛЕ вызова, а не внутри захвата.
        Итог: воркер не завершался ни за 1.5 s, ни за 30 s, аудиобуфер
        переполнялся, диктовка терялась. Вспомогательная функция блокировала
        основную.

        Не достался лок — возвращаем пустой текст с явным маркером: все три
        потребителя (realtime_partial, call_assist, meeting_session) уже умеют
        пустой результат и просто пропустят итерацию.
        """
        lock = mlx_lock()
        if not lock.acquire(timeout=PREVIEW_MLX_LOCK_TIMEOUT_SEC):
            logger.debug(
                "transcribe_preview: GPU занят дольше %.1f с — пропускаем итерацию",
                PREVIEW_MLX_LOCK_TIMEOUT_SEC,
            )
            return {"text": "", "skipped": "mlx_busy"}
        try:
            self.engine.set_quality_profile("balanced")
            return self.engine.transcribe(audio_data, cleanup_profile="soft", is_preview=True)
        finally:
            lock.release()

    def close(self) -> None:
        """Останавливает фоновые ресурсы обёрнутого engine (2026-08-04).

        Duck-typed: engine может быть fake-объектом без close() (тесты) — тогда
        это тихий no-op, не AttributeError. Never raises.
        """
        engine_close = getattr(self.engine, "close", None)
        if engine_close is None:
            return
        try:
            engine_close()
        except Exception:
            logger.warning("Transcriber.close: ошибка закрытия engine", exc_info=True)

    # ------------------------------------------------------------------
    # Phase B.1 — error_bus integration (late-injection, same as LLMRewriter)
    # ------------------------------------------------------------------

    def _push_diarization_no_token_if_needed(self, settings: dict) -> bool:
        """Check if diarization can proceed; push diarization.no_token if HF_TOKEN missing.

        Returns:
            True  — diarization is disabled OR token is present (safe to proceed).
            False — diarization is enabled but HF_TOKEN missing (error pushed, skip diarization).
        """
        if not settings.get("diarization_enabled", False):
            return True  # diarization off — no token needed

        import os
        token = (
            os.environ.get("HF_TOKEN")
            or os.environ.get("KRAB_EAR_HF_TOKEN")
            or ""
        )
        if token:
            return True  # token present — diarization can proceed

        error_bus = getattr(self, "_error_bus", None)
        if error_bus is not None:
            try:
                from backend.error_bus import KrabError
                from backend.error_codes import ERROR_REGISTRY
                from datetime import datetime, timezone
                entry = ERROR_REGISTRY["diarization.no_token"]
                err = KrabError(
                    severity=entry["severity"],
                    component="diarization",
                    code="diarization.no_token",
                    message_user=entry["user_msg_ru"],
                    message_debug="HF_TOKEN env not set; diarization skipped",
                    timestamp=datetime.now(timezone.utc),
                    context={},
                    actionable=entry["actionable"],
                    action_id=entry["action_id"],
                )
                error_bus.push(err)
            except Exception:
                logger.exception("error_bus.push diarization.no_token failed")
        return False
