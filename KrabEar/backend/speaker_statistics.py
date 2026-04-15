"""Анализ паттернов речи по спикерам диаризации Krab Ear.

SpeakerStatisticsAnalyzer — вычисляет per-speaker статистику из истории
транскрипций с данными диаризации: длительность речи, слова, темп,
распределение языков, появления, confidence и баланс спикеров.
"""

from __future__ import annotations

import logging
import re
from typing import Any

_log = logging.getLogger("KrabEar.Backend.SpeakerStatistics")

# Паттерн для токенизации слов: последовательность букв/цифр/апострофов.
_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


class SpeakerStatisticsAnalyzer:
    """Вычисляет per-speaker статистику по списку элементов истории.

    Принимает на вход список элементов истории (объекты HistoryItem или словари)
    и возвращает агрегированную статистику по каждому спикеру.
    """

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def analyze_speakers(
        self,
        items: list[Any],
        speaker_manager: Any = None,
    ) -> dict[str, Any]:
        """Анализирует паттерны речи по спикерам.

        Args:
            items: список объектов/словарей истории. Каждый элемент может иметь
                   поля ``diarization``, ``confidence``, ``source_lang``, ``ts``.
            speaker_manager: опциональный SpeakerManager для разрешения псевдонимов.
                             Должен иметь метод ``get_alias(speaker_id) -> str | None``.

        Returns:
            Словарь со статистикой спикеров::

                {
                    "speakers": {
                        "SPEAKER_00": {
                            "alias": "Паша",
                            "total_speaking_time_sec": 1200.0,
                            "total_words": 8500,
                            "avg_words_per_minute": 142.0,
                            "appearances": 45,
                            "avg_confidence": 0.89,
                            "languages": {"ru": 40, "es": 5},
                            "longest_turn_sec": 120.0,
                            "avg_turn_sec": 26.7,
                        }
                    },
                    "total_speakers": 3,
                    "most_active_speaker": "SPEAKER_00",
                    "speaker_balance": 0.65,
                }
        """
        # Промежуточное хранилище для агрегатов.
        # agg[speaker_id] = {
        #     "speaking_time_sec": float,
        #     "words": int,
        #     "turn_count": int,
        #     "turn_durations": list[float],
        #     "confidences": list[float],
        #     "languages": dict[str, int],
        # }
        agg: dict[str, dict[str, Any]] = {}

        for item in items:
            diar = self._get_field(item, "diarization")
            if not diar or not isinstance(diar, dict):
                continue
            if not diar.get("enabled"):
                continue

            turns = diar.get("speaker_turns", [])
            if not isinstance(turns, list):
                continue

            # Confidence записи целиком (может быть None).
            item_confidence = self._get_field(item, "confidence")

            # Язык записи (source_lang — lang detection результат).
            item_lang = (self._get_field(item, "source_lang") or "").strip().lower() or None

            for turn in turns:
                if not isinstance(turn, dict):
                    continue
                speaker = str(turn.get("speaker", "")).strip()
                if not speaker:
                    continue

                start = float(turn.get("start", 0.0))
                end = float(turn.get("end", start))
                duration = max(0.0, end - start)
                text = str(turn.get("text", "")).strip()
                words = len(_WORD_RE.findall(text))

                if speaker not in agg:
                    agg[speaker] = {
                        "speaking_time_sec": 0.0,
                        "words": 0,
                        "turn_count": 0,
                        "turn_durations": [],
                        "confidences": [],
                        "languages": {},
                    }

                entry = agg[speaker]
                entry["speaking_time_sec"] += duration
                entry["words"] += words
                entry["turn_count"] += 1
                if duration > 0:
                    entry["turn_durations"].append(duration)

                # Confidence: берём от записи целиком, если задан.
                if item_confidence is not None:
                    try:
                        entry["confidences"].append(float(item_confidence))
                    except (TypeError, ValueError):
                        pass

                # Язык: накапливаем счётчик из source_lang записи.
                if item_lang:
                    entry["languages"][item_lang] = entry["languages"].get(item_lang, 0) + 1

        if not agg:
            return {
                "speakers": {},
                "total_speakers": 0,
                "most_active_speaker": None,
                "speaker_balance": 1.0,
            }

        # Сборка итогового словаря спикеров.
        speakers_out: dict[str, Any] = {}
        for speaker_id, entry in agg.items():
            total_time = entry["speaking_time_sec"]
            total_words = entry["words"]
            turn_durations = entry["turn_durations"]
            confidences = entry["confidences"]

            avg_wpm = 0.0
            if total_time > 0:
                avg_wpm = round((total_words / total_time) * 60.0, 2)

            avg_conf = None
            if confidences:
                avg_conf = round(sum(confidences) / len(confidences), 4)

            longest_turn = max(turn_durations) if turn_durations else 0.0
            avg_turn = (
                round(sum(turn_durations) / len(turn_durations), 2)
                if turn_durations
                else 0.0
            )

            # Псевдоним из speaker_manager (если передан).
            alias: str | None = None
            if speaker_manager is not None:
                try:
                    alias = speaker_manager.get_alias(speaker_id)
                except Exception as exc:
                    _log.debug("Не удалось получить alias для %s: %s", speaker_id, exc)

            speakers_out[speaker_id] = {
                "alias": alias,
                "total_speaking_time_sec": round(total_time, 3),
                "total_words": total_words,
                "avg_words_per_minute": avg_wpm,
                "appearances": entry["turn_count"],
                "avg_confidence": avg_conf,
                "languages": dict(entry["languages"]),
                "longest_turn_sec": round(longest_turn, 3),
                "avg_turn_sec": avg_turn,
            }

        # Самый активный спикер — по суммарному времени речи.
        most_active = max(
            speakers_out,
            key=lambda sid: speakers_out[sid]["total_speaking_time_sec"],
        )

        balance = self._compute_balance(
            [v["total_speaking_time_sec"] for v in speakers_out.values()]
        )

        return {
            "speakers": speakers_out,
            "total_speakers": len(speakers_out),
            "most_active_speaker": most_active,
            "speaker_balance": balance,
        }

    # ------------------------------------------------------------------
    # IPC-обработчик
    # ------------------------------------------------------------------

    def handle_get_speaker_statistics(
        self,
        params: dict[str, Any],
        store: Any,
        speaker_manager: Any = None,
    ) -> dict[str, Any]:
        """IPC: get_speaker_statistics — per-speaker статистика из всей истории.

        Params:
            (нет обязательных параметров)

        Returns:
            Словарь speaker_statistics (см. ``analyze_speakers``).
        """
        try:
            with store._lock():
                items = store._load_active_items_unlocked()
        except Exception as exc:
            _log.warning("Не удалось загрузить историю для speaker_statistics: %s", exc)
            items = []
        return self.analyze_speakers(items, speaker_manager=speaker_manager)

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _get_field(item: Any, field: str) -> Any:
        """Читает поле из объекта или словаря."""
        if isinstance(item, dict):
            return item.get(field)
        return getattr(item, field, None)

    @staticmethod
    def _compute_balance(times: list[float]) -> float:
        """Вычисляет индекс баланса речи (1.0 = идеальный баланс).

        Использует нормализованную энтропию Шеннона: H / log(N),
        где N — количество спикеров. При одном спикере возвращает 0.0.
        При равных долях — 1.0.
        """
        import math

        n = len(times)
        if n <= 1:
            return 0.0

        total = sum(times)
        if total <= 0:
            return 1.0

        entropy = 0.0
        for t in times:
            if t > 0:
                p = t / total
                entropy -= p * math.log(p)

        max_entropy = math.log(n)
        if max_entropy <= 0:
            return 1.0

        return round(entropy / max_entropy, 4)
