"""LiveSubsService — потоковый STT + перевод для живых субтитров (Sprint 2B).

Принимает audio-чанки (base64 PCM 16 kHz mono), аккумулирует в буфере
и выполняет flush при накоплении ≥3 секунд или при is_final=True.
После flush: Whisper STT → translate → emit live_subs.result через EventBus.

F3 (2026-08-12, backpressure): `ingest()` НИКОГДА не выполняет STT инлайн —
живой инцидент показал, что синхронный flush в IPC-треде при 2x-темпе видео
вешал handle_request на 180с (backstop-таймаут) на КАЖДЫЙ чанк, сжигая
коннект-слоты и деградируя весь бэкенд. Вместо этого ingest() снимает
снапшот буфера под локом и кладёт его в слот размера 1 фонового воркера —
"последний выигрывает": субтитры эфемерны, свежее окно важнее отставшего.
Единственное исключение — is_final (пользователь остановил захват и ждёт
последний кусок): здесь ingest()/stop() синхронно ждут воркер, но с явным
таймаутом, а не бесконечно.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from typing import Any, Callable, TYPE_CHECKING

import numpy as np

from backend.event_bus import bus as event_bus
from contracts.live_subs_events import LiveSubsResult
from contracts.registry import EventType
from core.utils import is_likely_repetition_loop

if TYPE_CHECKING:
    from backend.transcriber import Transcriber
    from backend.translator import Translator

logger = logging.getLogger("KrabEar.Backend.LiveSubsService")

# Размер буфера (в секундах), при достижении которого происходит авто-flush.
_FLUSH_THRESHOLD_SEC = 3.0

# Абсолютный потолок буфера (в сэмплах) — 1 минута при 16 kHz.
# Защита от OOM: sample_rate приходит от клиента (handle_ingest) и контролируется
# атакующим. Огромный sample_rate делает buffer_sec≈0 навсегда → flush по
# порогу _FLUSH_THRESHOLD_SEC никогда не срабатывает → буфер растёт без границ.
# Этот потолок форсирует flush независимо от sample_rate. См. W1770 HIGH.
_MAX_BUFFER_SAMPLES = 16000 * 60

# Допустимый диапазон частоты дискретизации (Гц). Значения вне диапазона
# (включая отрицательные/огромные) клампятся в handle_ingest со структурным
# warning — нельзя доверять полю sample_rate из IPC-запроса.
_MIN_SAMPLE_RATE = 8000
_MAX_SAMPLE_RATE = 192000

# F3: сколько ждать фоновый воркер СИНХРОННО в is_final/stop() до того, как
# сдаться и вернуть flush_timeout. STT обычно занимает несколько секунд —
# таймаут даёт щедрый запас на загруженной машине, но остаётся далеко от
# 180с IPC-backstop'а, который уронил бэкенд в живом инциденте 2026-08-12.
_FINAL_FLUSH_TIMEOUT_SEC = 15.0

# F3: таймаут join() при остановке воркера (stop()/reset()/close()). daemon=True
# не требует join для завершения процесса — это best-effort, чтобы не оставить
# двух живых воркеров после быстрого рестарта сессии (см. _worker_loop).
_WORKER_JOIN_TIMEOUT_SEC = 2.0

# F3: период пробуждения воркера в холостом ожидании — ограничивает, насколько
# быстро stop_event будет замечен, даже если notify каким-то образом потерялся.
_WORKER_POLL_SEC = 1.0


class LiveSubsService:
    """Буферизация и обработка потоковых аудио-чанков для живых субтитров."""

    def __init__(
        self,
        transcriber: "Transcriber",
        translator: "Translator",
        settings_get: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self._transcriber = transcriber
        self._translator = translator
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda k, d: d)
        self._lock = threading.RLock()
        self._buffer: list[np.ndarray] = []
        self._buffer_samples: int = 0
        self._session_start: float = time.monotonic()

        # F3: фоновый flush-воркер + слот "последний выигрывает" (backpressure).
        # _worker_start_lock сериализует старт/стоп воркера (отдельно от
        # _worker_cond, который защищает pending/completed-состояние) —
        # исключает двойной спавн при конкурентных ingest() (double-checked
        # locking, тот же паттерн, что и lazy-load STT-адаптеров).
        self._worker_start_lock = threading.Lock()
        self._worker_cond = threading.Condition()
        self._worker_thread: threading.Thread | None = None
        # Экземпляр Event, захваченный ТЕКУЩИМ потоком воркера — см. _worker_loop
        # про то, почему это не общий на сервис флаг.
        self._worker_stop_event: threading.Event | None = None
        self._pending_window: dict[str, Any] | None = None
        self._pending_seq: int = 0
        self._completed_seq: int = 0
        self._completed_result: dict[str, Any] | None = None
        self._dropped_windows: int = 0
        # Лог дропа — один раз на эпизод (серию подряд идущих дропов), а не на
        # каждый: под нагрузкой это устроило бы тот же лог-шторм, что и сам
        # инцидент. Сбрасывается, когда сабмит застаёт слот пустым (воркер
        # догнал очередь).
        self._drop_episode_logged: bool = False

    # ── public API ────────────────────────────────────────────────────────────

    @property
    def dropped_windows(self) -> int:
        """Сколько окон дропнуто слотом воркера с момента создания сервиса (F3, наблюдаемость)."""
        with self._worker_cond:
            return self._dropped_windows

    def ingest(
        self,
        audio_bytes: bytes,
        sample_rate: int,
        target_lang: str,
        is_final: bool,
    ) -> dict[str, Any] | None:
        """Добавляет чанк в буфер; при необходимости планирует flush.

        F3: STT НИКОГДА не выполняется в этом треде. При достижении порога
        снапшот буфера снимается под локом и уходит в фоновый воркер через
        слот размера 1 — метод возвращается немедленно. Единственное
        исключение — is_final=True: вызывающий (пользователь остановил
        захват) ждёт последний кусок синхронно, но с явным таймаутом
        (_FINAL_FLUSH_TIMEOUT_SEC), а не бесконечно.

        Returns:
            None — буфер ещё не достиг порога, ИЛИ (только при is_final=True)
                финальный flush не успел завершиться за таймаут.
            dict — только при is_final=True и завершении воркера в течение
                таймаута: {"text", "translation", "start_ts", "end_ts",
                "language_detected"}.
        """
        # sample_rate приходит от клиента. WS /v1/stream вызывает ingest() НАПРЯМУЮ,
        # минуя handle_ingest-санитайзер, поэтому клампим ЗДЕСЬ — в общей точке обоих
        # путей (IPC + WS). Крошечный sample_rate иначе даёт resample_poly ~16000×
        # upsample в _process_window → попытка аллокации десятков GB → OOM/swap-thrash (W1771 HIGH).
        sample_rate = self._sanitize_sample_rate(sample_rate)
        audio_array = self._decode_audio(audio_bytes, sample_rate)
        # Под локом — ТОЛЬКО мутация буфера, решение о flush и снапшот+сброс.
        # Сам STT (несколько секунд) НИКОГДА не выполняется под этим локом —
        # ни здесь, ни где-либо ещё (F3): он живёт исключительно в фоновом
        # воркере, вне self._lock и вне IPC-тредов.
        with self._lock:
            self._buffer.append(audio_array)
            self._buffer_samples += len(audio_array)
            buffer_sec = self._buffer_samples / max(sample_rate, 1)
            # Потолок _MAX_BUFFER_SAMPLES форсирует flush даже если sample_rate
            # настолько велик, что buffer_sec никогда не достигнет порога (OOM-защита).
            over_cap = self._buffer_samples >= _MAX_BUFFER_SAMPLES
            should_flush = is_final or buffer_sec >= _FLUSH_THRESHOLD_SEC or over_cap
            if over_cap and not (is_final or buffer_sec >= _FLUSH_THRESHOLD_SEC):
                logger.warning(
                    "LiveSubsService: буфер достиг потолка — форсирую flush",
                    extra={
                        "buffer_samples": self._buffer_samples,
                        "max_buffer_samples": _MAX_BUFFER_SAMPLES,
                        "sample_rate": sample_rate,
                    },
                )
            if not should_flush:
                return None
            start_ts = self._session_start
            end_ts = time.monotonic()
            audio = np.concatenate(self._buffer).astype(np.float32)
            self._reset()

        # Лок отпущен — дальше только сабмит снапшота в фоновый воркер (F3).
        seq = self._submit_window(
            audio=audio,
            sample_rate=sample_rate,
            target_lang=target_lang,
            start_ts=start_ts,
            end_ts=end_ts,
        )
        if not is_final:
            # Немедленный возврат — обычный путь. Результат (если/когда воркер
            # его посчитает) уйдёт наружу только через EventBus (live_subs.result).
            return None

        result = self._await_completion(seq, timeout=_FINAL_FLUSH_TIMEOUT_SEC)
        if result is None:
            logger.warning(
                "LiveSubsService: is_final flush не успел за %.1fс — воркер занят/завис",
                _FINAL_FLUSH_TIMEOUT_SEC,
            )
        return result

    def stop(self) -> dict[str, Any]:
        """Flush оставшегося буфера, останавливает фоновый воркер и сбрасывает состояние.

        Если privacy_mode_enabled=True — буфер сбрасывается БЕЗ транскрипции
        и эмиссии событий (аудио, накопленное до переключения режима, не утекает).

        F3: финальный flush синхронный (пользователь ждёт последний кусок), но
        ограничен _FINAL_FLUSH_TIMEOUT_SEC — истёк таймаут → возвращаем
        {"status": "stopped", "flushed": False, "reason": "flush_timeout"},
        а не виснем на неопределённое время.
        """
        with self._lock:
            privacy_active = self._settings_get("privacy_mode_enabled", False)
            if privacy_active:
                self._reset()
                has_data = False
                audio = None
                start_ts = end_ts = 0.0
            else:
                has_data = bool(self._buffer)
                if has_data:
                    start_ts = self._session_start
                    end_ts = time.monotonic()
                    audio = np.concatenate(self._buffer).astype(np.float32)
                else:
                    audio = None
                    start_ts = end_ts = 0.0
                self._reset()

        if privacy_active:
            # Privacy fail-safe: сессия завершена под privacy_mode — не даём
            # уже засабмиченному ДО переключения режима окну (если такое есть)
            # доехать до STT/emit. Та же логика, что и в _process_window/reset().
            self._discard_pending_window()
            self._stop_worker()
            return {"status": "stopped", "flushed": False, "skipped": True,
                    "reason": "privacy_mode_active"}

        if not has_data:
            self._stop_worker()
            return {"status": "stopped", "flushed": False}

        seq = self._submit_window(
            audio=audio, sample_rate=16000, target_lang="off",
            start_ts=start_ts, end_ts=end_ts,
        )
        result = self._await_completion(seq, timeout=_FINAL_FLUSH_TIMEOUT_SEC)
        self._stop_worker()
        if result is None:
            logger.warning(
                "LiveSubsService: финальный flush в stop() не успел за %.1fс",
                _FINAL_FLUSH_TIMEOUT_SEC,
            )
            return {"status": "stopped", "flushed": False, "reason": "flush_timeout"}
        return {"status": "stopped", "flushed": True}

    def buffer_duration_sec(self, sample_rate: int = 16000) -> float:
        """Текущая длительность буфера в секундах."""
        with self._lock:
            return self._buffer_samples / max(sample_rate, 1)

    def wait_until_idle(self, timeout: float = 2.0) -> bool:
        """Блокирует, пока фоновый воркер не обработает все засабмиченные окна.

        F3: детерминированная точка синхронизации для тестов и диагностики —
        замена time.sleep() при проверке асинхронного flush. Возвращает True,
        если воркер догнал очередь (слот пуст, последнее засабмиченное окно
        обработано) до истечения timeout, иначе False.
        """
        deadline = time.monotonic() + timeout
        with self._worker_cond:
            while not (self._pending_window is None and self._completed_seq >= self._pending_seq):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._worker_cond.wait(timeout=remaining)
            return True

    def reset(self) -> None:
        """Очищает буфер БЕЗ транскрипции и эмиссии событий (под локом).

        Публичная точка для privacy-purge: handle_purge_all_data вызывает её,
        чтобы накопленное system-audio было стёрто немедленно, не пройдя через
        STT/EventBus. Отличается от stop(): никакого flush, никаких событий.

        F3: также выкидывает необработанное окно из слота воркера и
        останавливает воркер — иначе аудио, засабмиченное ДО purge, всё равно
        транскрибировалось бы и утекло через EventBus уже ПОСЛЕ того, как
        reset() отчитался о полной очистке.
        """
        with self._lock:
            self._reset()
        self._discard_pending_window()
        self._stop_worker()

    def close(self) -> None:
        """Graceful shutdown: останавливает фоновый flush-воркер без обработки хвоста.

        Идемпотентен — безопасно вызывать несколько раз. Тот же паттерн, что и
        другие daemon-треды сервиса (DiskSpaceMonitor/RecapScheduler/
        WakeWordWatchdog) в BackendService.close(): без явного join() поток мог
        бы залогировать уже после начала teardown интерпретатора в CI
        (feedback_backendservice_teardown_ci.md).
        """
        self._discard_pending_window()
        self._stop_worker()

    # ── IPC handlers ──────────────────────────────────────────────────────────

    def handle_ingest(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC handler: live_subs_ingest."""
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": True, "skipped": True, "reason": "privacy_mode_active"}

        audio_b64 = params.get("audio_chunk", "")
        target_lang = str(params.get("target_lang", "off"))
        sample_rate = self._sanitize_sample_rate(params.get("sample_rate", 16000))
        is_final = bool(params.get("is_final", False))

        try:
            audio_bytes = base64.b64decode(audio_b64)
        except Exception as exc:
            raise ValueError(f"audio_chunk: invalid base64: {exc}") from exc

        result = self.ingest(
            audio_bytes=audio_bytes,
            sample_rate=sample_rate,
            target_lang=target_lang,
            is_final=is_final,
        )

        buf_sec = self.buffer_duration_sec(sample_rate)
        if result is not None:
            return {
                "status": "flushed",
                "buffer_duration_sec": buf_sec,
                "text": result.get("text"),
                "translation": result.get("translation"),
            }
        if is_final:
            # F3: финальный flush был отправлен в воркер, но не успел завершиться
            # за таймаут — тишина лучше зависшего IPC-хендлера (живой инцидент
            # 2026-08-12: синхронный flush на КАЖДЫЙ чанк ронял весь бэкенд).
            # Явный статус вместо неотличимого от "accepted" молчания.
            return {
                "status": "stopped",
                "flushed": False,
                "reason": "flush_timeout",
                "buffer_duration_sec": buf_sec,
            }
        return {"status": "accepted", "buffer_duration_sec": buf_sec}

    def handle_stop(self, params: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        """IPC handler: live_subs_stop."""
        return self.stop()

    # ── internals: backpressure worker (F3) ──────────────────────────────────

    def _ensure_worker_started(self) -> None:
        """Лениво стартует фоновый flush-воркер (double-checked locking).

        Тот же паттерн, что и lazy-load у STT-адаптеров (_load_lock, см.
        CLAUDE.md): проверка без лока → лок → повторная проверка — иначе два
        конкурентных ingest() могут заспавнить два воркера одновременно.
        """
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        with self._worker_start_lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._worker_loop,
                args=(stop_event,),
                name="LiveSubsFlushWorker",
                daemon=True,
            )
            self._worker_stop_event = stop_event
            self._worker_thread = thread
            thread.start()

    def _worker_loop(self, stop_event: threading.Event) -> None:
        """Тело фонового воркера: STT/translate/emit выполняются здесь, полностью
        вне IPC-тредов и вне self._lock.

        stop_event — экземпляр, захваченный ИМЕННО этим потоком (а не общий на
        сервис флаг): при рестарте воркера (новая сессия после stop()/reset())
        новый поток получает СВОЙ собственный Event. Если бы флаг был общим —
        clear() для новой сессии тихо реанимировал бы старый, ещё не успевший
        выйти поток вместо того, чтобы дать ему корректно завершиться.
        """
        while not stop_event.is_set():
            with self._worker_cond:
                while self._pending_window is None and not stop_event.is_set():
                    self._worker_cond.wait(timeout=_WORKER_POLL_SEC)
                window = self._pending_window
                self._pending_window = None
            if window is None:
                continue
            try:
                result = self._process_window(window)
            except Exception:
                # Fail-safe (F3): исключение внутри STT/translate/emit не должно
                # убивать воркер молча — следующий ingest() иначе ждал бы полный
                # _FINAL_FLUSH_TIMEOUT_SEC впустую перед self-heal рестартом.
                logger.exception("LiveSubsService: фоновый flush упал с исключением")
                result = {"text": "", "translation": None}
            with self._worker_cond:
                self._completed_seq = window["seq"]
                self._completed_result = result
                self._worker_cond.notify_all()

    def _stop_worker(self) -> None:
        """Останавливает текущий воркер (если запущен) и джойнит с таймаутом.

        daemon=True не блокирует завершение процесса сам по себе, но не
        джойнить вовсе — значит рисковать двумя одновременно живыми воркерами
        после быстрого рестарта сессии (см. _worker_loop). join с таймаутом —
        best-effort: если воркер завис внутри transcribe() дольше
        _WORKER_JOIN_TIMEOUT_SEC, поток остаётся daemon'ом и завершится сам,
        когда (если) STT когда-нибудь вернётся — не блокируем вызывающего.
        """
        with self._worker_start_lock:
            thread = self._worker_thread
            stop_event = self._worker_stop_event
            if thread is None or stop_event is None:
                return
            stop_event.set()
            with self._worker_cond:
                self._worker_cond.notify_all()
            thread.join(timeout=_WORKER_JOIN_TIMEOUT_SEC)
            if thread.is_alive():
                logger.warning(
                    "LiveSubsService: воркер не завершился за %.1fс при остановке — "
                    "оставлен daemon-потоком",
                    _WORKER_JOIN_TIMEOUT_SEC,
                )
            self._worker_thread = None
            self._worker_stop_event = None

    def _submit_window(
        self,
        audio: np.ndarray,
        sample_rate: int,
        target_lang: str,
        start_ts: float,
        end_ts: float,
    ) -> int:
        """Кладёт снапшот в слот воркера (размер 1, последний выигрывает).

        Если слот уже занят необработанным окном — оно ДРОПАЕТСЯ: субтитры
        эфемерны, показать свежее окно правильнее, чем тащить отставшее.
        dropped_windows считает дропы для наблюдаемости; warning логируется
        один раз на эпизод (серию подряд идущих дропов), а не на каждый —
        иначе лог-шторм воспроизвёл бы тот же паттерн, что и живой инцидент.
        """
        self._ensure_worker_started()
        with self._worker_cond:
            self._pending_seq += 1
            seq = self._pending_seq
            if self._pending_window is not None:
                self._dropped_windows += 1
                if not self._drop_episode_logged:
                    logger.warning(
                        "LiveSubsService: воркер не успевает — окно дропнуто (backpressure)",
                        extra={"dropped_windows": self._dropped_windows},
                    )
                    self._drop_episode_logged = True
            else:
                self._drop_episode_logged = False
            self._pending_window = {
                "seq": seq,
                "audio": audio,
                "sample_rate": sample_rate,
                "target_lang": target_lang,
                "start_ts": start_ts,
                "end_ts": end_ts,
            }
            self._worker_cond.notify_all()
        return seq

    def _await_completion(self, seq: int, timeout: float) -> dict[str, Any] | None:
        """Блокирует до завершения окна с номером >= seq либо до истечения timeout.

        Используется ТОЛЬКО синхронными путями (is_final через ingest(), stop()).
        Обычный (не финальный) путь никогда не ждёт воркер — возвращается сразу.
        """
        deadline = time.monotonic() + timeout
        with self._worker_cond:
            while self._completed_seq < seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._worker_cond.wait(timeout=remaining)
            return self._completed_result

    def _discard_pending_window(self) -> None:
        """Выкидывает окно из слота БЕЗ обработки.

        Используется reset()/stop()-под-privacy/close() — окно, засабмиченное
        до явного сброса состояния, не должно доехать до STT/emit.
        """
        with self._worker_cond:
            if self._pending_window is not None:
                self._pending_window = None
                self._worker_cond.notify_all()

    # ── internals: flush pipeline ─────────────────────────────────────────────

    def _process_window(self, window: dict[str, Any]) -> dict[str, Any]:
        """Выполняет STT + translate по уже снятому окну и эмитит событие.

        F3: выполняется ИСКЛЮЧИТЕЛЬНО в фоновом воркере — снапшот буфера уже
        снят вызывающим (ingest()/stop()) под self._lock ДО сабмита в слот,
        этот метод self._buffer вообще не трогает.
        """
        # Privacy fail-safe (W1771 MED, сохранено из исходного _flush): даже
        # если per-chunk gate вызывающего проиграл гонку с privacy-toggle,
        # НИКОГДА не транскрибируем и не эмитим аудио, накопленное до
        # переключения режима. Окно теперь может ждать своей очереди в
        # воркере ДОЛЬШЕ, чем раньше висел один синхронный STT-вызов — тем
        # более веская причина перепроверять здесь, а не только на входе.
        if self._settings_get("privacy_mode_enabled", False):
            return {"text": "", "translation": None}

        # Defense-in-depth (W1771 HIGH): не доверяем несанитизированному
        # sample_rate даже здесь — window["sample_rate"] уже клампнут
        # ingest()/stop(), но повторный клампинг идемпотентен и защищает
        # любой будущий прямой вызов _process_window с битым rate.
        sample_rate = self._sanitize_sample_rate(window["sample_rate"])
        audio = window["audio"]
        target_lang = window["target_lang"]
        start_ts = window["start_ts"]
        end_ts = window["end_ts"]

        # Ресемплинг: Swift/SCStream отдаёт нативную частоту (обычно 48 kHz),
        # Whisper ожидает строго 16 kHz. Без ресемплинга Whisper воспринимает
        # audio pitch-shifted (×3 медленнее) → text="" → confidence=0.00.
        _WHISPER_SR = 16000
        if sample_rate != _WHISPER_SR and sample_rate > 0:
            try:
                from scipy.signal import resample_poly  # type: ignore[import]
                from math import gcd
                _g = gcd(sample_rate, _WHISPER_SR)
                audio = resample_poly(audio, _WHISPER_SR // _g, sample_rate // _g).astype(np.float32)
                logger.debug(
                    "LiveSubsService: resampled %d Hz → %d Hz (%d → %d samples)",
                    sample_rate, _WHISPER_SR, len(audio) * sample_rate // _WHISPER_SR, len(audio),
                )
            except Exception:
                logger.exception("LiveSubsService: ресемплинг не удался, STT получит raw %d Hz", sample_rate)

        # STT (skip_vad_prefilter=True для live_subs: VAD-модель тренирована на
        # mic input и speech_ratio=0.0 на компрессированном system-audio из YouTube
        # → STT никогда не вызывается. Для live субтитров VAD контрпродуктивен —
        # короткие чанки уже отфильтрованы на уровне Swift SystemAudioCapture.)
        # context_free=True (2026-08-12, живой инцидент): live subs флашит окна
        # каждые ~3s без initial_prompt — иначе TRANSCRIBE_PROMPT и история/
        # hotwords владельца утекают в субтитры чужого видео как "Сохраняй
        # смысл 0 тяги" (см. core/engine.py и docs/superpowers/specs/
        # 2026-08-12-live-subs-prompt-leakage-design.md).
        stt_result = self._transcriber.transcribe(
            audio, quality_profile="balanced", skip_vad_prefilter=True, context_free=True
        )
        text = stt_result.get("text", "").strip()
        language_detected = stt_result.get("language")

        # G2 (2026-08-12, живой инцидент — «Сохраняй смысл 0 тяги» x17 на экране
        # чужого YouTube-видео): зацикленное окно Whisper не должно доехать до
        # субтитров. Отличие от пути диктовки (engine.py:~1138, «не врём про
        # input»: пользователь видит реальный вывод + warning-тост и решает,
        # перезаписать ли фразу) — чужое видео перезаписать нельзя, поэтому для
        # live subs правильная реакция — дроп, а не показ мусора. Переиспользуем
        # dropped_windows (F3) — тот же счётчик наблюдаемости для обеих причин
        # дропа (backpressure и repetition-loop), новый заводить не нужно.
        if text:
            _is_loop, _loop_reason = is_likely_repetition_loop(text)
            if _is_loop:
                with self._worker_cond:
                    self._dropped_windows += 1
                    _dropped_total = self._dropped_windows
                # W1770 MED: не логируем сам текст/бигу/предложение из _loop_reason
                # (потенциальный фрагмент чужой речи) — только тип эвристики.
                logger.warning(
                    "LiveSubsService: зацикленное окно дропнуто (repetition loop)",
                    extra={
                        "loop_heuristic": _loop_reason.split(":", 1)[0],
                        "dropped_windows": _dropped_total,
                        "text_len": len(text),
                    },
                )
                return {
                    "text": "",
                    "translation": None,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "language_detected": language_detected,
                }

        # Translate
        translation: str | None = None
        if text and target_lang and target_lang not in ("off", "none", ""):
            try:
                tr = self._translator.translate(
                    text=text,
                    mode=target_lang,
                    network_mode="offline_default",
                )
                translation = tr.text or None
            except Exception:
                logger.exception("LiveSubsService: ошибка перевода")

        # Emit event
        event_payload = LiveSubsResult(
            text=text,
            translation=translation,
            start_ts=start_ts,
            end_ts=end_ts,
            language_detected=language_detected,
        )
        event_bus.emit_typed(EventType.LIVE_SUBS_RESULT, event_payload)
        # W1770 MED: НИКОГДА не логируем сам текст транскрипта/перевода (PII).
        # Только метаданные — гарантия metadata-only логирования.
        if text:
            logger.info(
                "LiveSubsService: flush OK",
                extra={
                    "text_len": len(text),
                    "lang": language_detected,
                    "translation_len": len(translation) if translation else 0,
                },
            )
        else:
            logger.info(
                "LiveSubsService: flush EMPTY (Whisper вернул пустой текст)",
                extra={"lang": language_detected},
            )

        return {
            "text": text,
            "translation": translation,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "language_detected": language_detected,
        }

    def _reset(self) -> None:
        """Сбрасывает буфер и метки времени."""
        self._buffer = []
        self._buffer_samples = 0
        self._session_start = time.monotonic()

    @staticmethod
    def _sanitize_sample_rate(raw: Any) -> int:
        """Валидирует и клампит sample_rate из IPC-запроса в безопасный диапазон.

        sample_rate приходит от клиента и нельзя ему доверять (W1770 HIGH):
        огромное значение ломает flush-gate по buffer_sec → OOM; нулевое/
        отрицательное приводит к делению на ноль / некорректному ресемплингу.
        Значения вне [_MIN_SAMPLE_RATE, _MAX_SAMPLE_RATE] клампятся со структурным
        warning; нечисловой ввод → дефолт 16000.
        """
        try:
            sr = int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "LiveSubsService: нечисловой sample_rate — дефолт 16000",
                extra={"raw_sample_rate": repr(raw)},
            )
            return 16000
        if sr < _MIN_SAMPLE_RATE or sr > _MAX_SAMPLE_RATE:
            clamped = min(max(sr, _MIN_SAMPLE_RATE), _MAX_SAMPLE_RATE)
            logger.warning(
                "LiveSubsService: sample_rate вне диапазона — клампинг",
                extra={
                    "requested_sample_rate": sr,
                    "clamped_sample_rate": clamped,
                    "min_sample_rate": _MIN_SAMPLE_RATE,
                    "max_sample_rate": _MAX_SAMPLE_RATE,
                },
            )
            return clamped
        return sr

    @staticmethod
    def _decode_audio(audio_bytes: bytes, sample_rate: int) -> np.ndarray:
        """Декодирует сырые PCM int16 байты в float32 [-1, 1]."""
        if len(audio_bytes) == 0:
            return np.zeros(0, dtype=np.float32)
        # Ожидаем 16-bit PCM (2 байта на сэмпл)
        if len(audio_bytes) % 2 != 0:
            audio_bytes = audio_bytes[: len(audio_bytes) - 1]
        pcm = np.frombuffer(audio_bytes, dtype=np.int16)
        return pcm.astype(np.float32) / 32768.0
