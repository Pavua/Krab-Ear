"""OpenWakeWordAdapter — адаптер openWakeWord для Krab Ear.

openWakeWord — open-source wake word detection (Apache 2.0, без email/signup).
GitHub: https://github.com/dscripka/openWakeWord

Встроенные модели: "alexa", "hey_mycroft", "hey_jarvis".
Кастомные модели ("Краб") требуют ~15 мин обучения через Jupyter notebook —
в данном PR не включены, только инфраструктура для их загрузки.

Установка (optional):
    pip install openwakeword

Адаптер использует lazy import: если библиотека не установлена,
логирует предупреждение и работает в stub-режиме.
"""

from __future__ import annotations

import base64
import logging
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

from backend.wake_word_preroll import PreRollBuffer
from backend.stt_keyword_spotter import (  # noqa: E402
    FRAME_SAMPLES as _STT_FRAME_SAMPLES,
    SttKeywordModel,
)
from core.audio_stream_guard import (  # noqa: E402
    DEFAULT_POLL_SEC,
    DEFAULT_STARVE_SEC,
    StreamStarved,
    wait_for_frames,
)

logger = logging.getLogger("KrabEar.Backend.OpenWakeWordAdapter")

# 🔴 Swift сравнивает reason ТОЧНОЙ строкой (WakeWordPoller.recordingInProgressReason),
# поэтому обе константы — контракт между процессами: менять только парно с
# native/KrabEarAgent/Sources/KrabEarAgent/WakeWordPoller.swift.
RECORDING_IN_PROGRESS_REASON = "recording in progress"
# W8 (2026-08-18, Fable-ревью дефект A): worker рекордера физически жив после
# stop()-таймаута, хотя is_recording уже False. Отдельная строка нужна, чтобы
# это состояние было ВИДНО (в логе агента и watchdog'у), а не сливалось с
# обычной диктовкой в вечный тихий ретрай.
RECORDER_WORKER_HUNG_REASON = "recorder worker hung"
# B17 (ревью Opus #2309, P2): оба движка выключены (off/off) — состояние, которое владелец меняет
# сам и без паузы поллера. Отдельная строка нужна, чтобы Swift НЕ жёг бюджет self-heal (иначе после
# трёх отказов wake word молчит и после включения движка). Менять парно с
# WakeWordPoller.enginesOffReason.
ENGINES_OFF_REASON = "wake engines off"

# Встроенные модели openWakeWord
_BUILTIN_MODELS: list[str] = [
    "alexa",
    "hey_mycroft",
    "hey_jarvis",
]

# Имя директории для пользовательских .onnx / .tflite моделей
_CUSTOM_MODELS_DIR = "wake_word_models"

# Режимы движков wake word (PR 2a, 2026-11-25): off — не работает, shadow — только счётчики (никого не будит,
# ни аудио, ни текст не сохраняются), live — будит. Ключи settings: wake_word_oww_mode / wake_word_stt_mode.
ENGINE_MODES = ("off", "shadow", "live")
ENGINE_OWW = "oww"
ENGINE_STT = "stt"
_SETTING_KEYS = {ENGINE_OWW: "wake_word_oww_mode", ENGINE_STT: "wake_word_stt_mode"}
_DEFAULT_MODES = {ENGINE_OWW: "live", ENGINE_STT: "off"}  # без настройки поведение прежнее: только openWakeWord
STT_MODEL_NAME = "krab_stt"
# Pre-roll после срабатывания (см. backend/wake_word_preroll.py): агент забирает звук «после слова» одним IPC-вызовом.
PREROLL_RING_SEC = 8.0      # сколько последних секунд звука держит кольцо сессии прослушивания
PREROLL_MAX_SEC = 6.0       # не больше этого отдаём агенту за раз
PREROLL_TTL_SEC = 15.0      # срок годности срабатывания: позже звук уже не «сразу после слова»
PREROLL_OWW_BACK_SEC = 0.5  # у openWakeWord точного конца слова нет: берём звук с небольшим запасом назад
_SHADOW_TS_KEEP = 500  # сколько последних меток времени срабатываний «в тени» держим для отчёта (только числа)

# Минимальный допустимый порог (F1 — covert mic tap guard)
_THRESHOLD_MIN: float = 0.05
_THRESHOLD_MAX: float = 1.0

# Таймаут загрузки модели OWW (секунды) — F4 download timeout guard
_MODEL_LOAD_TIMEOUT_SEC: float = 30.0

# Circuit breaker для повторных ошибок открытия микрофона (KRAB-EAR-BACKEND-1J):
# без этого Swift WakeWordPoller self-heal (restartMinGapSec=10s) бесконечно
# респавнит поток каждые ~10s, если sd.InputStream() падает синхронно
# (мик занят/недоступен) — 2376 событий Sentry за 7 часов.
_MAX_CONSECUTIVE_STREAM_FAILURES: int = 3
_STREAM_FAILURE_COOLDOWN_SEC: float = 60.0


class WakeEnginesOffError(RuntimeError):
    """Оба движка (openwakeword и STT-споттер) в режиме off — стартовать нечего, но это не сбой."""


class WakeWordDisabledError(RuntimeError):
    """Фича wake word выключена владельцем (wake_word_enabled=False).

    Отдельный тип, чтобы AudioReinitCoordinator._restore_listener отличал
    «восстанавливать нечего» (True) от настоящего сбоя старта (False).
    """


class OpenWakeWordAdapter:
    """Адаптер openWakeWord для Krab Ear.

    Поддерживает:
    - Встроенные модели openWakeWord: alexa, hey_mycroft, hey_jarvis.
    - Пользовательские .onnx/.tflite модели из {data_dir}/wake_word_models/.
    - Graceful stub-режим если openwakeword не установлен.
    """

    def __init__(
        self,
        data_dir: str | Path,
        settings_get: Optional[Callable[[str, Any], Any]] = None,
        is_recording: Optional[Callable[[], bool]] = None,
        is_start_blocked: Optional[Callable[[], bool]] = None,
        read_privacy_revision: Optional[Callable[[], str | None]] = None,
        stt_transcribe: Optional[Callable[[Any], str]] = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        # Распознаватель для STT-споттера (backend передаёт ТОЛЬКО уже загруженный в процессе GigaAM — второй
        # копии модели адаптер не создаёт). None — STT-движок недоступен, режим stt трактуется как off.
        self._stt_transcribe = stt_transcribe
        self._stt: SttKeywordModel | None = None
        self._oww_loading = False
        self._oww_load_retry_at = 0.0
        self._chunk_size = _STT_FRAME_SAMPLES  # размер чанка активной сессии (STT-споттер требует ровно 1280)
        self._shutting_down = False  # backend закрывается: модель споттера не выгружаем (процесс всё равно завершится)
        self._engine_stats: dict[str, dict[str, Any]] = {
            e: {"fires_live": 0, "fires_shadow": 0, "shadow_ts": deque(maxlen=_SHADOW_TS_KEEP), "last_fire_ts": None}
            for e in (ENGINE_OWW, ENGINE_STT)
        }
        # 2026-08-01 (F6): активная диктовка запрещает открывать второй тап.
        # stop()/_listen_loop читают голый is_recording (wedged под записью
        # нельзя — 2026-08-03). handle_wake_word_start дополнительно смотрит
        # is_start_blocked (W7): worker рекордера ещё жив после stop().
        self._is_recording = is_recording
        self._is_start_blocked = is_start_blocked
        self._read_privacy_revision = read_privacy_revision
        self._custom_dir = self._data_dir / _CUSTOM_MODELS_DIR
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._oww: Any = None  # openwakeword.Model instance
        self._on_detected: Callable[[str, float], None] | None = None
        self._active_model: str | None = None
        # 2026-07-12: threshold of the active listening session (mirrors
        # _active_model) — lets AudioSelfHealer restore the exact configured
        # threshold after a stop()/start() reinit cycle instead of silently
        # falling back to the start() default.
        self._active_threshold: float | None = None
        # Последняя детекция для IPC-поллинга агента (wake_word_status).
        # Монотонный ts — агент дебаунсит по росту, wall-clock не нужен.
        self._last_detection: dict[str, Any] | None = None
        self._detection_seq: int = 0  # id срабатывания: связывает status.last_detection и wake_word_take_preroll (B2)
        # Pre-roll: ссылка на кольцо ТОЙ сессии, что сработала, якорь (абсолютная позиция), время и ревизия политики.
        self._preroll_lock = threading.Lock()
        self._preroll_fire: dict[str, Any] | None = None
        self._preroll_ended = False  # вооружённое срабатывание пропало с выходом цикла (агент опоздал)
        # 2026-07-15 (спека wake-word-watchdog): heartbeat живого захвата.
        # last_chunk_ts штампуется ТОЛЬКО ненулевыми чанками (живой микрофон
        # никогда не отдаёт секунды идеальных int16-нулей; шторм нулей 12-07 и
        # зависшее чтение 13-07 оба оставляют его stale). Всё под self._lock.
        self._last_chunk_ts: float | None = None
        self._listen_started_ts: float | None = None
        # Диагностика A3: отдельно различаем открытие stream, read() без
        # ответа и успешно прочитанные (в том числе полностью нулевые) кадры.
        # Ни аудиоданные, ни имя устройства здесь не сохраняются.
        self._stream_opened: bool = False
        self._last_read_started_ts: float | None = None
        self._last_read_completed_ts: float | None = None
        self._last_any_chunk_ts: float | None = None
        # Поколение сессии: отвисший «зомби»-тред старой сессии видит чужое
        # поколение и выходит, не публикуя heartbeat/детекции чужой сессии
        # (один bounded read до проверки — принятый остаточный риск, спека §6).
        self._generation: int = 0
        # Выставляется watchdog'ом, когда мягкое лечение невозможно/не помогло.
        self._wedged: bool = False
        # Chip Finding 5: монотонный счётчик публичных stop() — координатор
        # снапшотит его после СВОЕГО stop() в танце и перепроверяет перед
        # restore; внешний stop (toggle-off/pause) во время танца отменяет
        # восстановление слушателя.
        self._stop_epoch: int = 0
        # 2026-07-15 (Fix A, ревью Task 4): окно обслуживания координатора.
        # Пока True — start() отказывает (и IPC wake_word_start вернёт
        # ok:false): между adapter.stop() и sd._terminate() чужой старт
        # спавнил бы тред, под которым исполнится Pa_Terminate (crash-класс).
        self._maintenance: bool = False
        self._oww_available = self._check_lib_available()
        # F2: callable to read runtime settings (e.g. privacy_mode_enabled)
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda k, d: d)
        # KRAB-EAR-BACKEND-1J: circuit breaker state for repeated immediate
        # sd.InputStream() open failures — protected by self._lock.
        self._consecutive_stream_failures: int = 0
        # Guarded read (спека 2026-08-23 §4.3). 🔴 Жизненный цикл задан явно:
        # поля ПЕРЕЖИВАЮТ _cleanup_session_after_loop_exit (иначе причина
        # выхода умрёт вместе с сессией и watchdog примет мёртвый стрим за
        # «чистую паузу»), но НЕ переживают публичный stop() (иначе
        # выключенный владельцем слушатель дозреет до wedged и получит
        # рестарт backend на пустом месте).
        self._starved_since: float | None = None
        self._consecutive_starve_exits: int = 0
        self._stream_failure_cooldown_until: float = 0.0
        # Инъекция для тестов: замер длительности join в stop() (R5).
        self._clock = time.monotonic

    # ------------------------------------------------------------------
    # Проверка наличия библиотеки
    # ------------------------------------------------------------------

    def _check_lib_available(self) -> bool:
        try:
            import importlib
            spec = importlib.util.find_spec("openwakeword")
            return spec is not None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def list_models(self) -> list[dict[str, Any]]:
        """Возвращает список доступных моделей: built-in + пользовательские.

        Returns:
            Список dict с полями: name (str), source ("builtin" | "custom"),
            path (str | None).
        """
        models: list[dict[str, Any]] = [
            {"name": m, "source": "builtin", "path": None}
            for m in _BUILTIN_MODELS
        ]

        # Сканируем директорию пользовательских моделей
        if self._custom_dir.exists():
            for f in sorted(self._custom_dir.iterdir()):
                if f.suffix.lower() in (".onnx", ".tflite") and f.is_file():
                    models.append({
                        "name": f.stem,
                        "source": "custom",
                        "path": str(f),
                    })

        return models

    def is_available(self) -> bool:
        """True если openwakeword установлен и может быть использован."""
        return self._oww_available

    def start(
        self,
        model_name: str,
        on_detected: Callable[[str, float], None],
        threshold: float = 0.5,
        chunk_size: int = 1280,
        sample_rate: int = 16000,
    ) -> None:
        """Запускает фоновый поток прослушивания.

        Args:
            model_name: Имя встроенной модели или stem пользовательского .onnx.
            on_detected: Callback (model_name, score) при обнаружении wake word.
            threshold: Порог уверенности [0.0, 1.0], по умолчанию 0.5.
            chunk_size: Размер аудио-чанка в сэмплах (int16).
            sample_rate: Частота дискретизации в Гц.

        Raises:
            RuntimeError: Если openwakeword не установлен.
            ValueError: Если модель не найдена.
        """
        with self._lock:
            # F5b (2026-09-16, sibling asymmetry F5): IPC-гейт
            # handle_wake_word_start — не единственный вход. Прямые вызовы
            # start() (AudioReinitCoordinator._restore_listener) обходили
            # проверку и открывали микрофон при wake_word_enabled=False.
            # Тот же fail-open default True, что в F5: отсутствие ключа =
            # «агент ещё не синхронизировал», не «выключено».
            if self._settings_get("wake_word_enabled", True) is False:
                logger.info(
                    "OpenWakeWordAdapter.start: отклонён — wake_word_enabled=False"
                )
                raise WakeWordDisabledError("wake word disabled in settings")
            if self._maintenance:
                raise RuntimeError(
                    "wake word занят обслуживанием аудио-стека (reinit) — "
                    "повторите позже"
                )
            now = time.monotonic()
            if now < self._stream_failure_cooldown_until:
                remaining = self._stream_failure_cooldown_until - now
                logger.warning(
                    "OpenWakeWordAdapter: старт заблокирован — %d подряд ошибок "
                    "открытия микрофона, охлаждение ещё %.0fs",
                    self._consecutive_stream_failures,
                    remaining,
                )
                raise RuntimeError(
                    "Микрофон недоступен (повторные ошибки открытия потока), "
                    "повторите позже"
                )

            if self._thread is not None and self._thread.is_alive():
                # Если stop() уже взвёл событие, но PortAudio не вернул
                # управление, это не активная сессия, а CFFI-клин. Ложный
                # success здесь раньше позволял поллеру считать новый listener
                # запущенным, а обнулённая ссылка — плодить новые zombie-треды.
                if self._stop_event.is_set() or self._active_model is None:
                    raise RuntimeError(
                        "предыдущий поток wake word завис внутри PortAudio; "
                        "требуется перезапуск backend"
                    )
                logger.warning(
                    "OpenWakeWordAdapter: уже запущен (модель %r), сначала stop()",
                    self._active_model,
                )
                return

            oww_mode = self.engine_mode(ENGINE_OWW)
            stt_wanted = (
                self._stt_transcribe is not None
                and self.engine_mode(ENGINE_STT) != "off"
                and chunk_size == _STT_FRAME_SAMPLES
            )
            if oww_mode != "off" and not self._oww_available and not stt_wanted:
                logger.warning(
                    "OpenWakeWordAdapter: openwakeword не установлен. "
                    "Установите: pip install openwakeword. "
                    "Работаем в stub-режиме."
                )
                raise RuntimeError(
                    "openwakeword не установлен. "
                    "Выполните: pip install openwakeword"
                )
            if oww_mode == "off" and not stt_wanted:
                # DEBUG: поллер стучится каждые ~10 с, INFO заливал бы лог (как и в ветке «идёт запись»).
                logger.debug("OpenWakeWordAdapter.start: все движки wake word выключены (режим off)")
                raise WakeEnginesOffError("все движки wake word выключены в настройках")
            use_oww = oww_mode != "off" and self._oww_available

            model_path = self._resolve_model_path(model_name) if use_oww else None
            self._on_detected = on_detected
            self._active_model = model_name
            self._active_threshold = threshold
            self._stop_event.clear()
            self._last_detection = None  # свежая сессия — стейл-детекция не триггерит
            self._reset_session_state()
            self._generation += 1

            self._oww = self._load_model(model_name, model_path) if use_oww else None
            self._chunk_size = chunk_size
            self._close_stt_locked()
            if stt_wanted:
                self._stt = SttKeywordModel(self._stt_transcribe, name=STT_MODEL_NAME)  # type: ignore[arg-type]
                self._start_stt_warmup()
            else:
                self._release_stt_model()
            self._thread = threading.Thread(
                target=self._listen_loop,
                kwargs={
                    "threshold": threshold,
                    "chunk_size": chunk_size,
                    "sample_rate": sample_rate,
                    "generation": self._generation,
                },
                daemon=True,
                name="OpenWakeWordListener",
            )
            self._thread.start()
            logger.info(
                "OpenWakeWordAdapter: запущен (model=%r, threshold=%.2f)",
                model_name,
                threshold,
            )

    def stop(self, timeout: float = 3.0) -> bool:
        """Останавливает поток прослушивания; при остановке «насовсем» (wake word выключен/privacy) выгружает
        модель STT-споттера. Пауза под запись модель не выгружает."""
        final = self._is_final_stop() and not self._shutting_down
        exited = self._stop_impl(timeout)
        if final:
            self._release_stt_model_async()
        return exited

    def _stop_impl(self, timeout: float = 3.0) -> bool:
        """Останавливает поток прослушивания.

        Returns:
            True — тред вышел (или не был запущен); False — тред НЕ вышел за
            timeout (застрял внутри PortAudio-вызова). Вызывающий обязан
            считать False сигналом «мягкий reinit небезопасен» (спека
            2026-07-15, вариант клина 13-07).
        """
        with self._lock:
            self._stop_epoch += 1
            self._last_detection = None
            # Остановка отзывает ещё не забранное срабатывание СИНХРОННО (раньше — только в finally цикла, то есть
            # после закрытия потока; при зависшем join звук оставался доступен). Порядок локов self._lock → _preroll_lock.
            with self._preroll_lock:
                if self._preroll_fire is not None:
                    self._preroll_fire = None
                    self._preroll_ended = True
            # Спека §4.1: heartbeat сбрасывается и в start(), и в stop().
            # wedged здесь НЕ трогаем — флаг обязан пережить pause/resume
            # циклы поллера (wake_word_stop при паузе), его снимает только
            # watchdog по свежему чанку или start() новой сессии.
            self._last_chunk_ts = None
            self._listen_started_ts = None
            self._stream_opened = False
            self._last_read_started_ts = None
            self._last_read_completed_ts = None
            self._last_any_chunk_ts = None
            # 🔴 Симметрично _last_detection: остановка ПО ПРОСЬБЕ владельца
            # (privacy, выключение фичи, пауза поллера) закрывает и лестницу
            # голодания — иначе застывший флаг эскалирует выключённую фичу.
            self._starved_since = None
            self._consecutive_starve_exits = 0
            thread = self._thread
            # Сессионные поля чистим ВСЕГДА, включая мёртвый/отсутствующий
            # тред: тред мог умереть сам (exception-путь _listen_loop не
            # чистит _active_model), и старый early-return без очистки
            # оставлял сигнатуру «мёртвой сессии» (running=False, model≠None)
            # на легитимно выключенном слушателе → ложная dead_session-
            # эскалация watchdog'а (re-review Task 4). Целевой случай
            # dead_session (упавший restore) не затронут — там stop() после
            # провала никто не зовёт.
            self._oww = None
            self._close_stt_locked()
            self._active_model = None
            self._active_threshold = None
            if thread is None or not thread.is_alive():
                self._thread = None
                return True
            self._stop_event.set()

        join_started = self._clock()
        thread.join(timeout=timeout)
        join_elapsed = self._clock() - join_started
        exited = not thread.is_alive()
        # R5 (инцидент 05.11): join, растянувшийся намного дольше timeout, —
        # признак останова ВСЕГО процесса, а не клина слушателя.
        process_stalled = join_elapsed > 2.0 * timeout + 1.0
        if exited:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
            logger.info("OpenWakeWordAdapter: остановлен")
        else:
            # 2026-08-03: под АКТИВНОЙ записью невыход за таймаут — ожидаемая
            # медлительность, а НЕ клин. Живой инцидент: агент паузит wake word
            # на время диктовки (wake_word_stop → сюда), поток не успевает выйти
            # за 3 с, адаптер помечал себя wedged, агент видел флаг и УБИВАЛ
            # backend ПОСРЕДИ записи. То есть сама пауза под запись и порождала
            # ложный сигнал зависания — запись не доезжала ни до вставки, ни до
            # истории. Ссылку на поток и возврат False сохраняем: start() всё
            # равно не создаст второй listener, а лечение отложится до момента,
            # когда запись кончится и сигнал можно будет оценить честно.
            _recording = False
            if self._is_recording is not None:
                try:
                    _recording = bool(self._is_recording())
                except Exception:
                    # Fail-open в сторону ЛЕЧЕНИЯ: не знаем состояние — считаем,
                    # что записи нет, и оставляем watchdog'у право вылечить
                    # настоящий клин (обратное решение молча отключило бы
                    # самолечение навсегда).
                    logger.warning(
                        "OpenWakeWordAdapter.stop: is_recording() упал — "
                        "трактуем как «записи нет»",
                        exc_info=True,
                    )
                    _recording = False
            with self._lock:
                # Ссылка намеренно остаётся в self._thread: start() увидит
                # живой клин и не создаст второй CFFI/PortAudio listener.
                # Обычный Python-finalize для такого треда небезопасен;
                # process-level policy в service.py завершит процесс без него.
                if not _recording and not process_stalled:
                    self._wedged = True
            if process_stalled:
                logger.warning(
                    "OpenWakeWordAdapter: join слушателя длился %.1fs при timeout %.1fs — "
                    "останов процесса целиком, wedged НЕ выставляем",
                    join_elapsed, timeout,
                )
            elif _recording:
                logger.info(
                    "OpenWakeWordAdapter: тред слушателя не вышел за %.1fs, "
                    "но идёт запись — wedged НЕ выставляем (легитимная пауза)",
                    timeout,
                )
            else:
                logger.error(
                    "OpenWakeWordAdapter: тред слушателя не вышел за %.1fs — "
                    "вероятно завис внутри PortAudio (класс инцидента 13-07)",
                    timeout,
                )
        return exited

    def is_running(self) -> bool:
        """True если поток прослушивания активен."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def active_model(self) -> str | None:
        """Имя активной модели или None."""
        with self._lock:
            return self._active_model

    def active_threshold(self) -> float | None:
        """Порог уверенности активной сессии прослушивания или None, если
        сейчас не запущен (2026-07-12, см. AudioSelfHealer)."""
        with self._lock:
            return self._active_threshold

    def heartbeat(self) -> dict[str, Any]:
        """Снапшот heartbeat'а для watchdog/status (спека 2026-07-15)."""
        with self._lock:
            return {
                "last_chunk_ts": self._last_chunk_ts,
                "listen_started_ts": self._listen_started_ts,
                "stream_opened": self._stream_opened,
                "last_read_started_ts": self._last_read_started_ts,
                "last_read_completed_ts": self._last_read_completed_ts,
                "last_any_chunk_ts": self._last_any_chunk_ts,
                # Голодание — НЕ «чистая пауза»: watchdog обязан различать
                # (иначе эпизод сбрасывается и wedged недостижим).
                "starvation_active": self._starved_since is not None,
                "consecutive_starve_exits": self._consecutive_starve_exits,
            }

    def listener_stack(self, max_frames: int = 12) -> str | None:
        """Стек живого потока слушателя — след для события wedged (None, если треда нет).

        Только чтение ``sys._current_frames()`` (+ кэш linecache), без локов адаптера, поэтому
        безопасно звать из сторожа даже когда слушатель завис.
        """
        thread = self._thread
        if thread is None or not thread.is_alive():
            return None  # мёртвый тред: ident мог быть переиспользован чужим
        ident = thread.ident
        if ident is None:
            return None
        frame = sys._current_frames().get(ident)
        if frame is None:
            return None
        import os
        import traceback

        text = "".join(traceback.format_stack(frame, limit=max_frames)).rstrip()
        # Стек уходит в Sentry: домашний каталог не светим.
        return text.replace(os.path.expanduser("~"), "~")

    def set_wedged(self, value: bool) -> None:
        with self._lock:
            self._wedged = bool(value)

    def is_wedged(self) -> bool:
        with self._lock:
            return self._wedged

    def begin_maintenance(self) -> None:
        """Координатор помечает опасное окно танца (stop → Pa_Terminate)."""
        with self._lock:
            self._maintenance = True

    def end_maintenance(self) -> None:
        with self._lock:
            self._maintenance = False

    def stop_epoch(self) -> int:
        """Монотонный счётчик публичных stop() (chip Finding 5) — растёт и на
        no-op stop'ах (слушатель уже остановлен): во время танца координатора
        слушатель как раз остановлен, и именно такой stop сигналит toggle-off."""
        with self._lock:
            return self._stop_epoch

    def _reset_session_state(self) -> None:
        """Чистое состояние новой сессии. Вызывать ТОЛЬКО под self._lock
        (start()) или в тестах без конкуренции."""
        self._last_chunk_ts = None
        self._listen_started_ts = None
        self._stream_opened = False
        self._last_read_started_ts = None
        self._last_read_completed_ts = None
        self._last_any_chunk_ts = None
        self._wedged = False
        self._preroll_fire = None  # срабатывание прошлой сессии не переживает перезапуск прослушивания
        self._preroll_ended = False

    def _create_stt_runtime(self, generation: int) -> SttKeywordModel | None:
        """Создаёт споттер ИЗ потока слушателя, когда режим stt переключили off→не-off уже после старта сессии
        (без этого переключатель в панели ничего не делал бы до рестарта адаптера). Поколение сверяется под локом."""
        with self._lock:
            if self._generation != generation or self._stop_event.is_set():
                return None
            if self._stt is None and self._stt_transcribe is not None:
                self._stt = SttKeywordModel(self._stt_transcribe, name=STT_MODEL_NAME)  # type: ignore[arg-type]
                created = True
            else:
                created = False
            stt = self._stt
        if created:
            self._start_stt_warmup()
        return stt

    def _maybe_load_oww_async(self, generation: int, model_name: str | None) -> None:
        """Модель openWakeWord при режиме oww=off на старте не загружена; включили на лету — грузим В ФОНЕ (загрузка до
        30 с не должна задерживать чтение микрофона), готовая модель подставляется под локом, если сессия та же."""
        if model_name is None or not self._oww_available:
            return
        with self._lock:
            if (self._oww_loading or self._generation != generation or time.monotonic() < self._oww_load_retry_at
                    or self._stop_event.is_set() or self._shutting_down):
                return  # остановка/закрытие между снимком и вызовом: поток загрузки не нужен (и опасен при close())
            self._oww_loading = True

        def _load() -> None:
            model = None
            try:
                model = self._load_model(model_name, self._resolve_model_path(model_name))
            except Exception as exc:  # noqa: BLE001 — не загрузилась: повтор не чаще раза в минуту, слушатель живёт
                logger.warning("OpenWakeWordAdapter: модель openWakeWord не загрузилась (%s)", type(exc).__name__)
            finally:
                with self._lock:
                    self._oww_loading = False
                    if model is None:
                        self._oww_load_retry_at = time.monotonic() + 60.0
                    elif self._generation == generation and not self._stop_event.is_set():
                        self._oww = model

        threading.Thread(target=_load, name="OwwRuntimeLoad", daemon=True).start()

    def _start_stt_warmup(self) -> None:
        """Загрузка и прогрев модели споттера в фоне (в первую реплику не копим 0,9 с компиляции ядер)."""
        warmup = getattr(self._stt_transcribe, "warmup", None)
        if callable(warmup):
            threading.Thread(target=warmup, name="SttSpotterWarmup", daemon=True).start()

    def _release_stt_model_async(self) -> None:
        """Выгрузка модели споттера в отдельном потоке: не под self._lock и не в потоке слушателя (release берёт
        mlx_lock с ожиданием до 0,25 с)."""
        # Нечего выгружать (модель не загружалась — режим stt никогда не включали) — поток не создаём: stop() входит в
        # путь close() backend, где новые потоки нежелательны (граница rest_stageb_boundary), а выгрузка не нужна.
        is_loaded = getattr(self._stt_transcribe, "is_loaded", None)
        is_loading = getattr(self._stt_transcribe, "is_loading", None)
        try:
            # Идущая загрузка — тоже «есть что выгружать» (B18): иначе остановка «насовсем» посреди прогрева пропускала
            # выгрузку, и по окончании загрузки 1,1 ГБ оставались в памяти при выключенном wake word.
            if callable(is_loaded) and not is_loaded() and not (callable(is_loading) and is_loading()):
                return
            threading.Thread(target=self._release_stt_model, name="SttSpotterRelease", daemon=True).start()
        except Exception:  # noqa: BLE001 — stop()/close() не имеют права падать из-за выгрузки модели споттера
            logger.warning("OpenWakeWordAdapter: не удалось запустить выгрузку модели STT-споттера", exc_info=True)

    def mark_shutdown(self) -> None:
        """Вызывается ``BackendService.close()`` ПЕРЕД ``stop()``: на закрытии процесса выгрузка модели не нужна (память
        освободит выход), а daemon-поток с mlx_lock/mx.clear_cache во время finalize интерпретатора — тот же класс
        фатала stderr-lock, что описан в CLAUDE.md (daemon-потоки BackendService)."""
        self._shutting_down = True

    def _is_final_stop(self) -> bool:
        """Остановка «насовсем» (wake word выключен или включён privacy), а не пауза под запись: после паузы адаптер
        стартует снова, и выгрузка заставила бы грузить 1,1 ГБ после КАЖДОЙ диктовки."""
        try:
            if self._settings_get("wake_word_enabled", True) is False:
                return True
        except Exception:
            return False
        return self._privacy_blocked()

    def _release_stt_model(self) -> None:
        """Режим stt выключен — модель споттера выгружается из памяти (если она вообще была загружена)."""
        release = getattr(self._stt_transcribe, "release", None)
        if callable(release):
            try:
                release()
            except Exception:
                logger.warning("OpenWakeWordAdapter: выгрузка модели STT-споттера не удалась", exc_info=True)

    def _close_stt_locked(self) -> None:
        """Закрывает STT-споттер (воркер-поток). close() не трогает сегментатор и поэтому безопасен из другого потока;
        predict()/reset() споттера вызываются ТОЛЬКО из потока слушателя."""
        stt, self._stt = self._stt, None
        if stt is not None:
            try:
                stt.close(timeout=0.5)
            except Exception:
                logger.warning("OpenWakeWordAdapter: закрытие STT-споттера не удалось", exc_info=True)

    def engine_mode(self, engine: str) -> str:
        """Текущий режим движка из настроек (читается на КАЖДОМ чанке: переключатель в панели действует без рестарта).
        Мусор и сбой чтения настроек = безопасный дефолт (oww live как раньше, stt off)."""
        try:
            value = self._settings_get(_SETTING_KEYS[engine], _DEFAULT_MODES[engine])
        except Exception:
            return _DEFAULT_MODES[engine]
        return value if value in ENGINE_MODES else _DEFAULT_MODES[engine]

    def _note_fire(self, engine: str, mode: str) -> None:
        with self._lock:
            stats = self._engine_stats[engine]
            now = time.monotonic()
            stats["last_fire_ts"] = now
            if mode == "live":
                stats["fires_live"] += 1
            else:
                stats["fires_shadow"] += 1
                stats["shadow_ts"].append(now)

    def engine_report(self) -> dict[str, Any]:
        """Числа по движкам для сравнения (без аудио и текста): режимы, срабатывания, health STT-распознавания."""
        with self._lock:
            stt = self._stt
            oww_obj = self._oww
            out: dict[str, Any] = {}
            for engine, stats in self._engine_stats.items():
                out[engine] = {
                    "fires_live": stats["fires_live"],
                    "fires_shadow": stats["fires_shadow"],
                    "last_fire_ts": stats["last_fire_ts"],
                    "predict_errors": stats.get("predict_errors", 0),
                }
        for engine in (ENGINE_OWW, ENGINE_STT):
            out[engine]["mode"] = self.engine_mode(engine)
        out[ENGINE_STT]["available"] = self._stt_transcribe is not None
        out[ENGINE_STT]["active"] = stt is not None
        out[ENGINE_OWW]["active"] = oww_obj is not None
        out[ENGINE_OWW]["loading"] = bool(self._oww_loading)
        out[ENGINE_OWW]["load_error"] = self._oww_load_error(out[ENGINE_OWW]["mode"], oww_obj)
        out[ENGINE_STT]["load_error"] = self._stt_load_error(out[ENGINE_STT]["mode"], stt)
        is_loaded = getattr(self._stt_transcribe, "is_loaded", None)
        out[ENGINE_STT]["loading"] = bool(
            stt is not None and callable(is_loaded) and not is_loaded() and out[ENGINE_STT]["load_error"] is None
        )
        if stt is not None:
            out[ENGINE_STT]["spotter"] = stt.stats()
            out[ENGINE_STT]["transcribing_for_sec"] = round(stt.busy_for(), 2)
            out[ENGINE_STT]["unavailable_reason"] = stt.last_unavailable_reason()
        return out

    def _oww_load_error(self, mode: str, oww_obj: Any) -> str | None:
        """Код причины, по которой openWakeWord не активен при включённом режиме (без текста): панель отличает
        «грузится» (штатно, до 30 с) от «библиотеки нет» и «сбой загрузки, повтор через минуту»."""
        if mode == "off" or oww_obj is not None:
            return None
        if not self._oww_available:
            return "not_installed"
        if not self._oww_loading and time.monotonic() < self._oww_load_retry_at:
            return "load_failed"
        return None

    def _stt_load_error(self, mode: str, stt: Any) -> str | None:
        """Код причины, по которой STT-споттер не работает при включённом режиме (без текста)."""
        if mode == "off":
            return None
        if self._stt_transcribe is None:
            return "no_transcriber"
        if stt is None:
            return "chunk_size" if self._chunk_size != _STT_FRAME_SAMPLES else None
        last_error = getattr(self._stt_transcribe, "last_error", None)
        if stt.last_unavailable_reason() == "model_load_failed" or (callable(last_error) and last_error() == "model_load_failed"):
            return "load_failed"
        return None

    def _record_detection(
        self, model_name: str, score: float, *,
        capture_revision: str | None = None, generation: int | None = None, detection_id: int | None = None,
    ) -> bool:
        """Публикует детекцию только для текущей ревизии политики захвата и живой сессии.

        Остановка (``stop()``) отзывает детекцию синхронно, но поток, уже стоящий на этом локе, иначе опубликовал бы
        её ПОСЛЕ остановки: агент увидел бы чужое срабатывание. Поэтому под тем же локом проверяем флаг остановки и,
        если передано, поколение сессии (зомби старого цикла не пишет в новую).
        """
        with self._lock:
            if self._stop_event.is_set() or (generation is not None and self._generation != generation):
                return False
            revision = self._privacy_revision()
            if revision is None or (capture_revision is not None and revision != capture_revision):
                return False
            if detection_id is None:  # прямой вызов без арминга pre-roll (тесты, будущие потребители): свой id
                self._detection_seq += 1
                detection_id = self._detection_seq
            self._last_detection = {
                "_privacy_revision": revision,
                "id": detection_id,
                "model": model_name,
                "score": float(score),
                "ts": time.monotonic(),
            }
            return True

    def _guarded_read_enabled(self) -> bool:
        """Killswitch guarded read (спека §9): False → прежний блокирующий путь."""
        try:
            return bool(self._settings_get("audio_guarded_read_enabled", True))
        except Exception:
            return True

    def _read_poll_sec(self) -> float:
        try:
            return float(self._settings_get("audio_read_poll_sec", DEFAULT_POLL_SEC))
        except Exception:
            return DEFAULT_POLL_SEC

    def _stream_starve_sec(self) -> float:
        try:
            return float(
                self._settings_get("audio_stream_starve_sec", DEFAULT_STARVE_SEC)
            )
        except Exception:
            return DEFAULT_STARVE_SEC

    def _privacy_revision(self) -> str | None:
        """Ревизия разрешённого захвата. Ошибка чтения закрывает микрофон."""
        try:
            if self._read_privacy_revision is not None:
                return self._read_privacy_revision()
            # Совместимость с адаптерами без версионированного провайдера.
            allowed = self._settings_get("privacy_mode_enabled", False) is False
            return "unversioned" if allowed else None
        except Exception:
            logger.warning("OpenWakeWordAdapter: privacy settings недоступны — микрофон заблокирован")
            return None

    def _privacy_blocked(self) -> bool:
        return self._privacy_revision() is None

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_wake_word_list_models(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """IPC: список доступных wake word моделей."""
        models = self.list_models()
        return {
            "ok": True,
            "models": models,
            "engine_available": self._oww_available,
            "custom_models_dir": str(self._custom_dir),
        }

    def handle_wake_word_start(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: запустить wake word detection.

        Параметры: model (str), threshold (float, optional).

        Security guards (W1205):
          F2 — возвращает skipped если privacy_mode_enabled.
          F1 — порог < 0 отклоняется; < 0.05 зажимается к 0.05; > 1.0 зажимается к 1.0.
          F5 — возвращает skipped если wake_word_enabled=False (2026-07-29).
        """
        # F2: privacy mode guard — do not open mic tap in privacy mode
        if self._privacy_blocked():
            logger.info(
                "OpenWakeWordAdapter.handle_wake_word_start: "
                "отклонён — privacy_mode_enabled=True"
            )
            return {"ok": False, "reason": "cannot activate wake-word in privacy mode"}

        # F5: настройка выключения обязана РЕАЛЬНО выключать (2026-07-29).
        # До этого гейта `wake_word_enabled` не читалась backend'ом вообще, и
        # settings.json показывал False, пока микрофон слушался — выключить
        # фичу через настройки было физически невозможно. Backend владеет
        # микрофоном, поэтому гейт стоит здесь: устаревший или сломанный агент
        # не должен уметь открыть тап вопреки настройке.
        #
        # Дефолт — РАЗРЕШЕНО: отсутствие ключа означает «агент ещё не
        # синхронизировал своё значение», а не «выключено». Fail-closed здесь
        # тихо сломал бы работающий wake word у всех, у кого ключа ещё нет.
        if self._settings_get("wake_word_enabled", True) is False:
            logger.info(
                "OpenWakeWordAdapter.handle_wake_word_start: "
                "отклонён — wake_word_enabled=False"
            )
            return {"ok": False, "reason": "wake word disabled in settings"}

        # F6: активная диктовка запрещает открывать ВТОРОЙ входной тап
        # (живой инцидент 2026-08-01). Два конкурирующих CoreAudio-тапа на одном
        # устройстве вешают worker'а AudioRecorder насмерть -> recorder_timeout,
        # запись теряется целиком. Гейт стоит здесь, а не в агенте: backend
        # владеет микрофоном и один знает про активную запись — тот же принцип,
        # что у F5 выше. Источник вызова — self-heal WakeWordPoller, который
        # шлёт start каждые ~10 секунд и про запись не знает.
        #
        # FAIL-CLOSED: сбой колбэка трактуем как «идёт запись». Цена ошибки
        # несимметрична — ложный отказ лишь не разбудит по слову (обратимо),
        # ложное разрешение стоит пользователю всей диктовки. Это отличие от
        # WakeWordWatchdog, где тот же колбэк fail-open: там ошибка в другую
        # сторону отключила бы сторожа, здесь — сломала бы основную функцию.
        # Сбой логируем WARNING: тихий fail-closed убил бы wake word навсегда.
        #
        # W7 (2026-08-17): is_start_blocked закрывает окно после stop(), когда
        # is_recording уже False, а worker рекордера ещё держит CoreAudio
        # (join / recorder_timeout). Reason тот же, что F6 — поллер не жжёт
        # бюджет self-heal.
        _busy_cb = (
            self._is_start_blocked
            if self._is_start_blocked is not None
            else self._is_recording
        )
        if _busy_cb is not None:
            try:
                _recording = bool(_busy_cb())
            except Exception:
                logger.warning(
                    "OpenWakeWordAdapter.handle_wake_word_start: "
                    "is_recording()/is_start_blocked() упал — считаем, что "
                    "запись идёт (fail-closed)",
                    exc_info=True,
                )
                _recording = True
            if _recording:
                # W8: отделяем настоящую диктовку от зависшего worker'а. Голый
                # is_recording честен для первой; комбинированный гейт —
                # единственный, кто знает про вторую (is_worker_thread_alive).
                _reason = RECORDING_IN_PROGRESS_REASON
                if self._is_start_blocked is not None and self._is_recording is not None:
                    try:
                        if not bool(self._is_recording()):
                            _reason = RECORDER_WORKER_HUNG_REASON
                    except Exception:
                        # Не знаем — оставляем прежнюю строку: она заведомо
                        # безопасна для Swift (транзиентная ветка, бюджет цел).
                        logger.warning(
                            "OpenWakeWordAdapter.handle_wake_word_start: "
                            "is_recording() упал при уточнении причины",
                            exc_info=True,
                        )
                # DEBUG, а не INFO: поллер стучится каждые ~10 секунд, и на
                # каждой диктовке INFO залил бы err.log (как это делает F5).
                logger.debug(
                    "OpenWakeWordAdapter.handle_wake_word_start: "
                    "отклонён — %s", _reason,
                )
                return {"ok": False, "reason": _reason}

        model_name = str(params.get("model", "hey_jarvis"))

        # F1: threshold validation and clamping
        try:
            raw_threshold = float(params.get("threshold", 0.5))
        except (TypeError, ValueError):
            return {"ok": False, "error": "threshold должен быть числом"}

        if raw_threshold < 0:
            return {"ok": False, "error": "threshold не может быть отрицательным"}

        threshold = max(_THRESHOLD_MIN, min(_THRESHOLD_MAX, raw_threshold))
        if threshold != raw_threshold:
            logger.warning(
                "OpenWakeWordAdapter: threshold %.4f зажат к %.4f",
                raw_threshold,
                threshold,
            )

        def _on_detected(name: str, score: float) -> None:
            logger.info(
                "Wake word обнаружен: model=%r score=%.3f", name, score
            )

        try:
            self.start(model_name, _on_detected, threshold=threshold)
            return {"ok": True, "model": model_name, "threshold": threshold}
        except WakeEnginesOffError:
            # ТОЛЬКО reason (как в ветке записи): Swift-поллер берёт `error` раньше `reason`, и при наличии
            # обоих полей why != enginesOffReason — бюджет self-heal сгорал бы (ревью Opus, #2309).
            return {"ok": False, "reason": ENGINES_OFF_REASON}
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def handle_wake_word_stop(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: остановить wake word detection."""
        self.stop()
        return {"ok": True}

    def handle_wake_word_take_preroll(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: ОДНОРАЗОВО отдать агенту звук «после слова» последнего живого срабатывания (pcm int16 mono, base64).

        Агент вызывает сразу при детекции (до ``wake_word_stop``): пока слушатель жив, кольцо не сброшено. Недоступно
        (``available: false`` + код причины, без аудио): нет свежего срабатывания, истёк TTL, сменилась ревизия политики
        захвата. Звук не пишется на диск и не логируется."""
        requested = (params or {}).get("detection_id")
        with self._preroll_lock:
            current = self._preroll_fire
            if requested is not None and current is not None and current.get("id") != requested:
                # Агент пришёл за звуком ДРУГОГО срабатывания (его слот уже вытеснен новым или id не наш): чужое
                # аудио не отдаём и новый слот не расходуем — его заберёт тот, кто увидел его id (ревью B2).
                return {"ok": True, "available": False, "reason": "superseded"}
            fire, self._preroll_fire = self._preroll_fire, None
            ended, self._preroll_ended = self._preroll_ended, False
        if fire is None:
            return {"ok": True, "available": False, "reason": "session_ended" if ended else "no_detection"}
        ring: PreRollBuffer = fire["ring"]
        reason: str | None = None
        if time.monotonic() - fire["ts"] > PREROLL_TTL_SEC:
            reason = "expired"
        else:
            revision = self._privacy_revision()
            if revision is None or revision != fire["rev"]:
                reason = "privacy"
        if reason is not None:
            # Кольцо принадлежит живой сессии прослушивания: после истёкшего take там мог уже ждать звук следующего
            # срабатывания — не трогаем. Чистим только при смене политики приватности (звук снят под старой).
            if reason == "privacy":
                ring.clear()
            return {"ok": True, "available": False, "reason": reason}
        rate = int(fire["rate"])
        pcm = ring.slice_from(fire["anchor"], int(PREROLL_MAX_SEC * rate))
        return {
            "ok": True,
            "available": bool(pcm),
            "sample_rate": rate,
            "format": "pcm_s16le_mono_b64",
            "samples": len(pcm) // 2,
            "pcm_b64": base64.b64encode(pcm).decode("ascii"),
            "age_ms": int((time.monotonic() - fire["ts"]) * 1000),
        }

    def _drop_preroll(self, ring: PreRollBuffer) -> None:
        """Выход цикла: кольцо этой сессии и срабатывание, ссылающееся на него, забываются (ничего не висит в памяти)."""
        with self._preroll_lock:
            if self._preroll_fire is not None and self._preroll_fire["ring"] is ring:
                self._preroll_fire = None
                self._preroll_ended = True
        ring.clear()

    def handle_wake_word_status(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """IPC: статус адаптера + последняя детекция для поллинга агента.

        ВНИМАНИЕ: self._lock — обычный Lock; is_running()/active_model() сами
        берут его, поэтому вызываются ВНЕ with-блока (иначе deadlock).
        """
        hb = self.heartbeat()
        status = {
            "ok": True,
            "running": self.is_running(),
            "active_model": self.active_model(),
            "engine_available": self._oww_available,
            "last_chunk_ts": hb["last_chunk_ts"],
            "listen_started_ts": hb["listen_started_ts"],
            "wedged": self.is_wedged(),
        }
        with self._lock:
            last = dict(self._last_detection) if self._last_detection else None
        if last is not None and last.pop("_privacy_revision") != self._privacy_revision():
            last = None
        status["last_detection"] = last
        status["engines"] = self.engine_report()
        return status

    # ------------------------------------------------------------------
    # Внутренние
    # ------------------------------------------------------------------

    def _resolve_model_path(self, model_name: str) -> str | None:
        """Возвращает путь к файлу модели или None для встроенных."""
        if model_name in _BUILTIN_MODELS:
            return None  # openWakeWord загрузит по имени автоматически

        # Поиск в директории пользовательских моделей
        if self._custom_dir.exists():
            for ext in (".onnx", ".tflite"):
                candidate = self._custom_dir / (model_name + ext)
                if candidate.exists():
                    return str(candidate)

        raise ValueError(
            f"Модель {model_name!r} не найдена. "
            f"Встроенные: {_BUILTIN_MODELS}. "
            f"Пользовательские: {self._custom_dir}"
        )

    def _load_model(self, model_name: str, model_path: str | None) -> Any:
        """Загружает openwakeword.Model.

        Security guards (W1205):
          F3 — отклоняет symlink-пути и пути вне data_dir.
          F4 — таймаут загрузки модели (_MODEL_LOAD_TIMEOUT_SEC) для HF download.
        """
        try:
            from openwakeword.model import Model as OWWModel  # type: ignore[import]
        except ImportError:
            raise RuntimeError("openwakeword не установлен")

        if model_path is not None:
            # F3: symlink and path-escape check
            path = Path(model_path)
            if path.is_symlink():
                raise ValueError(
                    f"Путь к модели является symlink и отклонён: {model_path}"
                )
            try:
                resolved = path.resolve()
                data_dir_resolved = self._data_dir.resolve()
                resolved.relative_to(data_dir_resolved)
            except ValueError:
                raise ValueError(
                    f"Путь к модели выходит за пределы data_dir: {model_path}"
                )

            # F4: wrap load in a thread with timeout to guard against slow HF downloads
            logger.info(
                "OpenWakeWordAdapter: загрузка кастомной модели %r (таймаут %.0fs)",
                model_path,
                _MODEL_LOAD_TIMEOUT_SEC,
            )
            result: list[Any] = []
            exc_holder: list[BaseException] = []

            def _load() -> None:
                try:
                    result.append(OWWModel(wakeword_models=[model_path]))
                except Exception as e:  # noqa: BLE001
                    exc_holder.append(e)

            t = threading.Thread(target=_load, daemon=True)
            t.start()
            t.join(timeout=_MODEL_LOAD_TIMEOUT_SEC)
            if t.is_alive():
                raise RuntimeError(
                    f"Загрузка модели {model_name!r} превысила таймаут "
                    f"{_MODEL_LOAD_TIMEOUT_SEC:.0f}s"
                )
            if exc_holder:
                raise exc_holder[0]
            logger.info("OpenWakeWordAdapter: кастомная модель %r загружена", model_path)
            return result[0]

        # Встроенная модель — openWakeWord скачает при первом запуске
        logger.info(
            "OpenWakeWordAdapter: загрузка встроенной модели %r (таймаут %.0fs, "
            "возможна загрузка с HF)",
            model_name,
            _MODEL_LOAD_TIMEOUT_SEC,
        )
        result = []
        exc_holder = []

        def _load_builtin() -> None:
            try:
                result.append(OWWModel(wakeword_models=[model_name]))
            except Exception as e:  # noqa: BLE001
                exc_holder.append(e)

        t = threading.Thread(target=_load_builtin, daemon=True)
        t.start()
        t.join(timeout=_MODEL_LOAD_TIMEOUT_SEC)
        if t.is_alive():
            raise RuntimeError(
                f"Загрузка встроенной модели {model_name!r} превысила таймаут "
                f"{_MODEL_LOAD_TIMEOUT_SEC:.0f}s (возможно зависание HF download)"
            )
        if exc_holder:
            raise exc_holder[0]
        logger.info("OpenWakeWordAdapter: встроенная модель %r загружена", model_name)
        return result[0]

    def _cleanup_session_after_loop_exit(self, generation: int) -> None:
        """Смерть цикла (exception/ImportError/privacy-break) раньше оставляла
        `_active_model` выставленным — для watchdog'а это сигнатура «мёртвой
        сессии» (chip по Finding 3 Fable-гейта волны watchdog): класс
        мгновенных падений старта, который до волны тихо гасился
        circuit-breaker'ом, получал бы kickstart вместо cooldown; backend-side
        privacy-break — тем более. Чистим generation-guarded: зомби-тред
        старого поколения не смеет затирать поля НОВОЙ сессии (start() бампит
        поколение под этим же локом). Штатный stop()-выход даёт здесь no-op
        (поля уже занулены), wedged — домен watchdog'а, не трогаем."""
        with self._lock:
            if self._generation != generation:
                return
            self._oww = None
            self._close_stt_locked()
            self._active_model = None
            self._active_threshold = None
            self._last_detection = None
            self._last_chunk_ts = None
            self._listen_started_ts = None
            self._stream_opened = False
            self._last_read_started_ts = None
            self._last_read_completed_ts = None
            self._last_any_chunk_ts = None

    def _listen_loop(
        self,
        threshold: float,
        chunk_size: int,
        sample_rate: int,
        generation: int,
    ) -> None:
        """Фоновый поток: читает аудио с микрофона и передаёт в openWakeWord."""
        with self._lock:
            self._listen_started_ts = time.monotonic()
        ring = PreRollBuffer(int(PREROLL_RING_SEC * sample_rate))  # pre-roll: звук только этой сессии, только в памяти
        try:
            import sounddevice as sd  # type: ignore[import]
        except ImportError:
            logger.error(
                "OpenWakeWordAdapter: sounddevice не установлен"
            )
            self._cleanup_session_after_loop_exit(generation)
            return

        logger.debug(
            "OpenWakeWordAdapter._listen_loop: старт "
            "(chunk=%d, rate=%d, threshold=%.2f)",
            chunk_size,
            sample_rate,
            threshold,
        )

        # 2026-08-01 (F6, mid-flight re-check): гейт handle_wake_word_start
        # проверяет запись в НАЧАЛЕ старта, а тап открывается здесь — уже после
        # синхронной загрузки модели. Окно между ними не микросекунда: кэша нет,
        # каждый start() строит OWWModel заново. Лог инцидента даёт его размер —
        # 02:35:21 (гейт пройден) → 02:35:37 (тап открыт) = 16 секунд. Запись,
        # начатая ВНУТРИ окна, встречала уже разрешённый старт и получала второй
        # тап на то же устройство → worker AudioRecorder висел насмерть.
        # Штатная пауза поллера тут не спасает: wake_word_stop ждёт на _lock,
        # который держит загрузка модели.
        # FAIL-CLOSED, симметрично гейту: не знаем состояние — не открываем.
        # Поллер ретрайнет своим циклом (~10 с), когда запись кончится.
        if self._stop_event.is_set():
            logger.debug(
                "OpenWakeWordAdapter._listen_loop: остановка запрошена до "
                "открытия тапа — выходим"
            )
            self._cleanup_session_after_loop_exit(generation)
            return
        if self._is_recording is not None:
            try:
                _recording = bool(self._is_recording())
            except Exception:
                logger.warning(
                    "OpenWakeWordAdapter._listen_loop: is_recording() упал — "
                    "тап не открываем (fail-closed)",
                    exc_info=True,
                )
                _recording = True
            if _recording:
                logger.debug(
                    "OpenWakeWordAdapter._listen_loop: запись началась во время "
                    "загрузки модели — тап не открываем"
                )
                self._cleanup_session_after_loop_exit(generation)
                return

        capture_revision = self._privacy_revision()
        if capture_revision is None:
            self._cleanup_session_after_loop_exit(generation)
            return
        stt_was_off = False

        # Смена ревизии завершает сессию целиком: ring buffer и модель могли
        # накопить PCM за время краткого ON→OFF. Повторный start создаёт оба заново.
        def privacy_changed() -> bool:
            return self._privacy_revision() != capture_revision

        try:
            with sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                blocksize=chunk_size,
            ) as stream:
                with self._lock:
                    self._consecutive_stream_failures = 0
                    self._stream_failure_cooldown_until = 0.0
                    if self._generation == generation:
                        self._stream_opened = True
                while not self._stop_event.is_set():
                    if self._guarded_read_enabled():
                        try:
                            if not wait_for_frames(
                                stream,
                                chunk_size,
                                stop_event=self._stop_event,
                                poll_sec=self._read_poll_sec(),
                                starve_sec=self._stream_starve_sec(),
                                is_recording=self._is_recording,
                                cancel_wait=privacy_changed,
                            ):
                                break  # попросили остановиться
                            # Кадры пришли → стрим жив. Лестницу голодания
                            # закрываем ЗДЕСЬ, а не по ненулевому чанку:
                            # абсолютная тишина (все нули) — валидное аудио,
                            # и привязка сброса к flat.any() оставила бы
                            # starvation_active висеть на работающем стриме.
                            with self._lock:
                                if self._starved_since is not None or self._consecutive_starve_exits:
                                    self._starved_since = None
                                    self._consecutive_starve_exits = 0
                        except StreamStarved as exc:
                            # Тред НЕ лечит себя сам: перезапуск сессии —
                            # домен поллера/координатора, wedged — домен
                            # watchdog'а. Наше дело — выйти и оставить след.
                            with self._lock:
                                self._starved_since = time.monotonic()
                                self._consecutive_starve_exits += 1
                                streak = self._consecutive_starve_exits
                            logger.warning(
                                "OpenWakeWordAdapter: стрим не отдаёт кадры (%s); "
                                "выходим из цикла, подряд голоданий=%d",
                                exc, streak,
                            )
                            break
                    # Политика могла измениться во время ожидания кадров.
                    if privacy_changed():
                        break
                    with self._lock:
                        if self._generation == generation:
                            self._last_read_started_ts = time.monotonic()
                    try:
                        audio_chunk, _ = stream.read(chunk_size)
                    finally:
                        # read() может бросить, а __exit__ ниже — зависнуть:
                        # это НЕ pending read. Штампуем оба исхода до close.
                        with self._lock:
                            if self._generation == generation:
                                self._last_read_completed_ts = time.monotonic()
                    if privacy_changed():
                        break
                    # openwakeword.Model.predict() требует numpy.ndarray —
                    # НЕ list (см. KRAB-EAR-BACKEND-1C/1D). sounddevice уже
                    # возвращает ndarray, поэтому просто flatten() без .tolist().
                    flat = audio_chunk.flatten()

                    with self._lock:
                        if self._generation != generation:
                            logger.info(
                                "OpenWakeWordAdapter: сессия устарела "
                                "(generation %d != %d) — зомби-тред выходит",
                                generation, self._generation,
                            )
                            break
                        oww = self._oww
                        stt = self._stt
                        active_model = self._active_model
                        self._last_any_chunk_ts = time.monotonic()
                        if flat.any():
                            self._last_chunk_ts = self._last_any_chunk_ts
                    # Оба движка выключили на лету (off/off): слушать нечему. Раньше цикл держал открытый тап и
                    # продолжал копить звук в pre-roll-кольце до ручной остановки (ревью B17): закрываем микрофон
                    # ДО добавления чанка в кольцо. Новый start() при off/off и так отказывает.
                    if self.engine_mode(ENGINE_OWW) == "off" and self.engine_mode(ENGINE_STT) == "off":
                        logger.info("OpenWakeWordAdapter: оба движка выключены — освобождаем микрофон")
                        break
                    try:
                        ring_total = ring.append(flat)
                    except Exception:  # noqa: BLE001 — pre-roll best-effort: сбой кольца не должен глушить wake word
                        ring_total = ring.total

                    # Режим включили ПОСЛЕ старта сессии (off→shadow/live): объект движка создаём на лету.
                    if (stt is None and self._stt_transcribe is not None and chunk_size == _STT_FRAME_SAMPLES
                            and self.engine_mode(ENGINE_STT) != "off"):
                        stt = self._create_stt_runtime(generation)
                    if oww is None and self.engine_mode(ENGINE_OWW) != "off":
                        self._maybe_load_oww_async(generation, active_model)

                    if (oww is None and stt is None and not self._oww_loading) or privacy_changed():
                        break
                    if oww is None and stt is None:
                        continue  # модель openWakeWord ещё грузится в фоне: микрофон читаем дальше

                    # Один тап микрофона, два движка. Режим читается на каждом чанке (off/shadow/live):
                    # shadow — только счётчики и метки времени, никого не будит и ничего не сохраняет.
                    detections: list[tuple[str, str, float, str]] = []  # (engine, имя, score, режим)
                    backs: dict[str, int | None] = {}  # engine → сколько сэмплов назад начинать pre-roll
                    if oww is not None:
                        mode = self.engine_mode(ENGINE_OWW)
                        if mode != "off":
                            for mdl_name, score in oww.predict(flat).items():
                                if score >= threshold:
                                    detections.append((ENGINE_OWW, mdl_name, float(score), mode))
                                    backs[ENGINE_OWW] = int(PREROLL_OWW_BACK_SEC * sample_rate)
                    if stt is not None:
                        mode = self.engine_mode(ENGINE_STT)
                        if mode != "off":
                            if stt_was_off:
                                stt.reset()  # включили на лету: сегментатор не должен помнить старый поток
                                self._start_stt_warmup()  # модель могла быть выгружена при выключении
                                stt_was_off = False
                            try:
                                stt_scores = stt.predict(flat)
                            except Exception as exc:  # noqa: BLE001 — сбой споттера не должен глушить openWakeWord
                                stt_scores = {}
                                with self._lock:
                                    self._engine_stats[ENGINE_STT]["predict_errors"] = (
                                        self._engine_stats[ENGINE_STT].get("predict_errors", 0) + 1
                                    )
                                logger.warning("OpenWakeWordAdapter: STT-споттер predict() упал (%s)", type(exc).__name__)
                            for mdl_name, score in stt_scores.items():
                                if score >= 0.5:
                                    detections.append((ENGINE_STT, mdl_name, float(score), mode))
                                    take_back = getattr(stt, "fire_preroll_back", None)
                                    backs[ENGINE_STT] = take_back() if callable(take_back) else None
                        elif not stt_was_off:
                            stt_was_off = True
                            self._release_stt_model_async()  # выключили на лету: 1,1 ГБ не держим до рестарта
                    if privacy_changed():
                        break
                    live_published = False  # оба движка в одном чанке: будим один раз, второй только считаем
                    for engine, mdl_name, score, mode in detections:
                        self._note_fire(engine, mode)
                        if mode != "live" or live_published:
                            continue
                        # pre-roll вооружаем ДО публикации детекции: агент, увидев её в wake_word_status, сразу забирает
                        # звук и не должен получить «no_detection» из-за не успевшего отработать потока (ревью Codex).
                        # Отказ публикации (смена политики) → return → finally сбросит вооружённое срабатывание.
                        back = backs.get(engine)
                        with self._lock:
                            if self._generation != generation:
                                return  # зомби старого поколения не армирует чужую сессию
                            if self._stop_event.is_set():
                                return  # stop() уже отозвал срабатывание: не вооружаем и не публикуем после остановки
                            self._detection_seq += 1
                            detection_id = self._detection_seq
                            with self._preroll_lock:
                                self._preroll_ended = False
                                self._preroll_fire = {
                                    "ring": ring, "rate": sample_rate, "ts": time.monotonic(), "rev": capture_revision,
                                    "anchor": max(0, ring_total - (back if back is not None else 0)),
                                    "id": detection_id,
                                }
                        if not self._record_detection(
                            mdl_name, score, capture_revision=capture_revision, generation=generation,
                            detection_id=detection_id,
                        ):
                            return
                        live_published = True
                        if self._on_detected is not None:
                            if privacy_changed():
                                return
                            self._on_detected(mdl_name, score)

        except Exception:
            logger.exception("OpenWakeWordAdapter._listen_loop: ошибка")
            with self._lock:
                self._consecutive_stream_failures += 1
                if self._consecutive_stream_failures >= _MAX_CONSECUTIVE_STREAM_FAILURES:
                    self._stream_failure_cooldown_until = (
                        time.monotonic() + _STREAM_FAILURE_COOLDOWN_SEC
                    )
                    logger.warning(
                        "OpenWakeWordAdapter: %d подряд ошибок открытия микрофона — "
                        "охлаждение %.0fs перед следующей попыткой",
                        self._consecutive_stream_failures,
                        _STREAM_FAILURE_COOLDOWN_SEC,
                    )
        finally:
            self._drop_preroll(ring)  # pre-roll: кольцо и срабатывание, ссылающееся на него, исчезают с сессией
            # Любой выход цикла (exception, privacy-break, oww-None,
            # generation-break, штатный stop) — сессия либо чистится, либо
            # generation-гард защищает поля новой сессии.
            self._cleanup_session_after_loop_exit(generation)
