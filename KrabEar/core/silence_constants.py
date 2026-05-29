"""Общие константы порога тишины для всех модулей Krab Ear (W1018).

Двухуровневая система порогов:

  SILENCE_THRESHOLD_DB_STRICT (-40 дБ)
      Агрессивный порог для аналитики (AudioQualityAnalyzer, get_speech_ratio).
      Офисный фоновый шум обычно ниже -40 дБ → надёжно детектируется как тишина.

  SILENCE_THRESHOLD_DB_PRESERVE_WHISPER (-55 дБ)
      Мягкий порог для STT-путей (SmartSilenceSkipper, RealtimeSilenceFilter).
      Сохраняет шёпот и тихую речь (~-45…-55 дБ) — не допускает их отсечения
      перед Whisper, что устраняет пустые транскрипты для тихой речи.

Backward-compatible aliases:
  SILENCE_THRESHOLD_DB  → SILENCE_THRESHOLD_DB_STRICT  (-40 dB)
  SILENCE_THRESHOLD_AMP → амплитудный эквивалент -40 dB (0.01)

Все модули ДОЛЖНЫ импортировать константы отсюда.
"""

from __future__ import annotations

# Строгий порог: -40 dBFS (аналитика, метрики)
SILENCE_THRESHOLD_DB_STRICT: float = -40.0

# Мягкий порог для STT: -55 dBFS — сохраняет шёпот
SILENCE_THRESHOLD_DB_PRESERVE_WHISPER: float = -55.0

# Backward-compatible alias
SILENCE_THRESHOLD_DB: float = SILENCE_THRESHOLD_DB_STRICT

# Эквивалентная амплитуда для STRICT: 10 ** (SILENCE_THRESHOLD_DB / 20) = 0.01.
# Вычисляется из SILENCE_THRESHOLD_DB_STRICT чтобы значение не расходилось при смене порога.
SILENCE_THRESHOLD_AMP: float = 10.0 ** (SILENCE_THRESHOLD_DB_STRICT / 20.0)

__all__ = [
    "SILENCE_THRESHOLD_DB",
    "SILENCE_THRESHOLD_AMP",
    "SILENCE_THRESHOLD_DB_STRICT",
    "SILENCE_THRESHOLD_DB_PRESERVE_WHISPER",
]
