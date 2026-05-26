"""Детектор тишины и проб-запрос для Phase 3 Call Automation.

detect_silence_window — определяет непрерывную тишину >= duration_sec.
confirm_silence_with_probe — отправляет TTS-запрос и ждёт ответа 5 сек.
"""

from __future__ import annotations

import logging
import math
import subprocess
import threading
from typing import Any

import numpy as np

from core.silence_constants import SILENCE_THRESHOLD_DB

logger = logging.getLogger("KrabEar.Backend.CallSilenceProbe")

# Длительность probe-TTS в секундах (macOS say ~2 сек)
_TTS_PROBE_WAIT_SEC = 5.0
# Фраза для отправки при тишине
_PROBE_PHRASE_RU = "Вы ещё на линии?"
_PROBE_PHRASE_EN = "Are you still there?"


def _rms_db(audio: "np.ndarray[Any, np.dtype[np.float32]]") -> float:
    """Вычисляет RMS уровень аудио в дБ FS.

    Args:
        audio: массив float32 в диапазоне [-1, 1].

    Returns:
        float: уровень в дБ (0 dBFS = полный масштаб, отрицательные значения тише).
        Возвращает -96.0 для нулевого сигнала.
    """
    if audio.size == 0:
        return -96.0
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    if rms <= 0:
        return -96.0
    return 20.0 * math.log10(rms)


class CallSilenceProbe:
    """Детектор тишины и проб-зонд для автоматического завершения звонка."""

    def __init__(
        self,
        probe_phrase_ru: str = _PROBE_PHRASE_RU,
        probe_phrase_en: str = _PROBE_PHRASE_EN,
        probe_wait_sec: float = _TTS_PROBE_WAIT_SEC,
    ) -> None:
        self._probe_phrase_ru = probe_phrase_ru
        self._probe_phrase_en = probe_phrase_en
        self._probe_wait_sec = probe_wait_sec
        # Флаг: был ли получен ответ после probe
        self._response_received = threading.Event()

    def detect_silence_window(
        self,
        audio_buffer: "np.ndarray[Any, np.dtype[np.float32]]",
        sample_rate: int = 16000,
        threshold_db: float = SILENCE_THRESHOLD_DB,
        duration_sec: float = 10.0,
    ) -> bool:
        """Определяет, содержит ли буфер непрерывную тишину >= duration_sec.

        Разбивает буфер на окна по 100 мс и проверяет, что хвостовой участок
        длиной duration_sec весь ниже threshold_db.

        Args:
            audio_buffer: аудио PCM float32, моно или стерео (усредняем каналы).
            sample_rate: частота дискретизации в Гц.
            threshold_db: порог тишины в дБ FS (дефолт: -40 dB).
            duration_sec: минимальная длина окна тишины в секундах.

        Returns:
            bool: True если последние duration_sec буфера — тишина.
        """
        if audio_buffer is None or audio_buffer.size == 0:
            return False

        # Приводим к моно
        mono: "np.ndarray[Any, np.dtype[np.float32]]"
        if audio_buffer.ndim > 1:
            mono = audio_buffer.mean(axis=1).astype(np.float32)
        else:
            mono = audio_buffer.astype(np.float32)

        # Нужно минимум duration_sec сэмплов
        required_samples = int(duration_sec * sample_rate)
        if mono.size < required_samples:
            return False

        # Берём хвост нужной длины
        tail = mono[-required_samples:]

        # Разбиваем на окна 100 мс
        window_samples = max(1, int(0.1 * sample_rate))
        num_windows = len(tail) // window_samples

        if num_windows == 0:
            return _rms_db(tail) < threshold_db

        for i in range(num_windows):
            chunk = tail[i * window_samples:(i + 1) * window_samples]
            if _rms_db(chunk) >= threshold_db:
                return False

        # Остаток
        remainder = tail[num_windows * window_samples:]
        if remainder.size > 0 and _rms_db(remainder) >= threshold_db:
            return False

        logger.debug(
            "Обнаружена тишина: %.1f сек ниже %.1f dB",
            duration_sec,
            threshold_db,
        )
        return True

    def confirm_silence_with_probe(
        self,
        language: str = "ru",
        _say_fn: Any = None,
    ) -> bool:
        """Отправляет TTS-запрос и ждёт ответа probe_wait_sec секунд.

        Если ответ не получен — возвращает False (кандидат на завершение).

        Args:
            language: язык probe-фразы ('ru' или 'en').
            _say_fn: callable для инъекции в тестах (заменяет macOS say).
                     Должен принимать (phrase, language) → None.

        Returns:
            bool: True если ответ получен, False если тишина подтверждена.
        """
        phrase = (
            self._probe_phrase_ru
            if language == "ru"
            else self._probe_phrase_en
        )

        self._response_received.clear()

        if _say_fn is not None:
            try:
                _say_fn(phrase, language)
            except Exception as exc:
                logger.warning("Probe TTS (_say_fn) ошибка: %s", exc)
        else:
            self._speak_macos(phrase, language)

        # Ждём сигнала об ответе
        got_response = self._response_received.wait(timeout=self._probe_wait_sec)

        if got_response:
            logger.debug("Probe: ответ получен, звонок продолжается")
        else:
            logger.info(
                "Probe: нет ответа за %.1f сек — кандидат на завершение",
                self._probe_wait_sec,
            )
        return got_response

    def signal_response_received(self) -> None:
        """Сигналить что пользователь ответил (вызывается из обработчика STT)."""
        self._response_received.set()

    @staticmethod
    def _speak_macos(phrase: str, language: str) -> None:
        """Произносит фразу через macOS say в фоновом потоке."""
        voice = "Milena" if language == "ru" else "Samantha"

        def _run() -> None:
            try:
                subprocess.run(
                    ["say", "-v", voice, phrase],
                    timeout=10,
                    check=False,
                )
            except Exception as exc:
                logger.warning("macOS say ошибка: %s", exc)

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # IPC handler (used by CallAutoEnd via check_auto_end)
    # ------------------------------------------------------------------

    def check_silence(
        self,
        audio_buffer: "np.ndarray[Any, np.dtype[np.float32]]",
        sample_rate: int = 16000,
        threshold_db: float = SILENCE_THRESHOLD_DB,
        duration_sec: float = 10.0,
    ) -> dict[str, Any]:
        """Публичный метод для интеграции с CallAutoEnd.

        Returns:
            dict с ключами 'is_silent' и 'duration_sec'.
        """
        is_silent = self.detect_silence_window(
            audio_buffer,
            sample_rate=sample_rate,
            threshold_db=threshold_db,
            duration_sec=duration_sec,
        )
        return {
            "is_silent": is_silent,
            "threshold_db": threshold_db,
            "window_sec": duration_sec,
        }
