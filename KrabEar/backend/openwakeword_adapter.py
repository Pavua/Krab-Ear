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

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("KrabEar.Backend.OpenWakeWordAdapter")

# Встроенные модели openWakeWord
_BUILTIN_MODELS: list[str] = [
    "alexa",
    "hey_mycroft",
    "hey_jarvis",
]

# Имя директории для пользовательских .onnx / .tflite моделей
_CUSTOM_MODELS_DIR = "wake_word_models"

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
    ) -> None:
        self._data_dir = Path(data_dir)
        # 2026-08-01 (F6): активная диктовка запрещает открывать второй тап.
        # Тот же колбэк, что у AudioReinitCoordinator и WakeWordWatchdog.
        self._is_recording = is_recording
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
        # 2026-07-15 (спека wake-word-watchdog): heartbeat живого захвата.
        # last_chunk_ts штампуется ТОЛЬКО ненулевыми чанками (живой микрофон
        # никогда не отдаёт секунды идеальных int16-нулей; шторм нулей 12-07 и
        # зависшее чтение 13-07 оба оставляют его stale). Всё под self._lock.
        self._last_chunk_ts: float | None = None
        self._listen_started_ts: float | None = None
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
        self._stream_failure_cooldown_until: float = 0.0

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

            if not self._oww_available:
                logger.warning(
                    "OpenWakeWordAdapter: openwakeword не установлен. "
                    "Установите: pip install openwakeword. "
                    "Работаем в stub-режиме."
                )
                raise RuntimeError(
                    "openwakeword не установлен. "
                    "Выполните: pip install openwakeword"
                )

            model_path = self._resolve_model_path(model_name)
            self._on_detected = on_detected
            self._active_model = model_name
            self._active_threshold = threshold
            self._stop_event.clear()
            self._last_detection = None  # свежая сессия — стейл-детекция не триггерит
            self._reset_session_state()
            self._generation += 1

            self._oww = self._load_model(model_name, model_path)
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
            # Спека §4.1: heartbeat сбрасывается и в start(), и в stop().
            # wedged здесь НЕ трогаем — флаг обязан пережить pause/resume
            # циклы поллера (wake_word_stop при паузе), его снимает только
            # watchdog по свежему чанку или start() новой сессии.
            self._last_chunk_ts = None
            self._listen_started_ts = None
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
            self._active_model = None
            self._active_threshold = None
            if thread is None or not thread.is_alive():
                self._thread = None
                return True
            self._stop_event.set()

        thread.join(timeout=timeout)
        exited = not thread.is_alive()
        if exited:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
            logger.info("OpenWakeWordAdapter: остановлен")
        else:
            with self._lock:
                # Ссылка намеренно остаётся в self._thread: start() увидит
                # живой клин и не создаст второй CFFI/PortAudio listener.
                # Обычный Python-finalize для такого треда небезопасен;
                # process-level policy в service.py завершит процесс без него.
                self._wedged = True
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

    def heartbeat(self) -> dict[str, float | None]:
        """Снапшот heartbeat'а для watchdog/status (спека 2026-07-15)."""
        with self._lock:
            return {
                "last_chunk_ts": self._last_chunk_ts,
                "listen_started_ts": self._listen_started_ts,
            }

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
        self._wedged = False

    def _record_detection(self, model_name: str, score: float) -> None:
        """Фиксирует последнюю детекцию для wake_word_status (IPC-поллинг)."""
        with self._lock:
            self._last_detection = {
                "model": model_name,
                "score": float(score),
                "ts": time.monotonic(),
            }

    def _privacy_blocked(self) -> bool:
        """True если privacy mode включён — держать микрофон wake word нельзя.

        Fail-open к False: сломанный settings-провайдер не должен «ронять»
        слушатель, за выключение отвечает и агент (setPrivacyMode → stop).
        """
        try:
            return bool(self._settings_get("privacy_mode_enabled", False))
        except Exception:
            return False

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
        if self._settings_get("privacy_mode_enabled", False):
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
        if self._is_recording is not None:
            try:
                _recording = bool(self._is_recording())
            except Exception:
                logger.warning(
                    "OpenWakeWordAdapter.handle_wake_word_start: "
                    "is_recording() упал — считаем, что запись идёт (fail-closed)",
                    exc_info=True,
                )
                _recording = True
            if _recording:
                # DEBUG, а не INFO: поллер стучится каждые ~10 секунд, и на
                # каждой диктовке INFO залил бы err.log (как это делает F5).
                logger.debug(
                    "OpenWakeWordAdapter.handle_wake_word_start: "
                    "отклонён — идёт запись"
                )
                return {"ok": False, "reason": "recording in progress"}

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
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def handle_wake_word_stop(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: остановить wake word detection."""
        self.stop()
        return {"ok": True}

    def handle_wake_word_status(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """IPC: статус адаптера + последняя детекция для поллинга агента.

        ВНИМАНИЕ: self._lock — обычный Lock; is_running()/active_model() сами
        берут его, поэтому вызываются ВНЕ with-блока (иначе deadlock).
        """
        with self._lock:
            last = dict(self._last_detection) if self._last_detection else None
        hb = self.heartbeat()
        return {
            "ok": True,
            "running": self.is_running(),
            "active_model": self.active_model(),
            "engine_available": self._oww_available,
            "last_detection": last,
            "last_chunk_ts": hb["last_chunk_ts"],
            "listen_started_ts": hb["listen_started_ts"],
            "wedged": self.is_wedged(),
        }

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
            self._active_model = None
            self._active_threshold = None
            self._last_detection = None
            self._last_chunk_ts = None
            self._listen_started_ts = None

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
                while not self._stop_event.is_set():
                    if self._privacy_blocked():
                        logger.info(
                            "OpenWakeWordAdapter: privacy mode включён — "
                            "слушатель остановлен"
                        )
                        break
                    audio_chunk, _ = stream.read(chunk_size)
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
                        if flat.any():
                            self._last_chunk_ts = time.monotonic()

                    if oww is None:
                        break

                    prediction = oww.predict(flat)
                    for mdl_name, score in prediction.items():
                        if score >= threshold:
                            self._record_detection(mdl_name, float(score))
                            if self._on_detected is not None:
                                self._on_detected(mdl_name, float(score))

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
            # Любой выход цикла (exception, privacy-break, oww-None,
            # generation-break, штатный stop) — сессия либо чистится, либо
            # generation-гард защищает поля новой сессии.
            self._cleanup_session_after_loop_exit(generation)
