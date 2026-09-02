"""Молчаливая потеря чанков в MLX-адаптере GigaAM (03.09.2026).

Живой случай владельца 02.09.2026: 27 секунд речи превратились в 101 знак, и в
логе не было ни одной ошибки. Причина — цикл склейки:

    piece = self._infer_chunk(...)
    if piece:
        texts.append(piece)      # пустой кусок выбрасывался МОЛЧА

Все аварийные пути адаптера аккуратны и бросают исключение, но ПУСТОЙ ответ
модели на кусок ошибкой не считался. При чанке в 20 секунд это тихая потеря
почти половины диктовки, а итоговая строка про число кусков стоит на уровне
`debug` — в проде её не видно.

Тишина в куске при этом законна: чанкер режет по паузам, и кусок из одной
паузы обязан давать пустой текст. Поэтому пустой результат разбирается на два
случая: тихий кусок — норма, звучащий кусок без текста — потеря.
"""
import os
import sys
import unittest
from unittest.mock import patch

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.pipeline.stt_gigaam_mlx import GigaAMMLXAdapter, GigaAMMLXChunkLoss  # noqa: E402


def _speech(seconds: float, sample_rate: int = 16000) -> np.ndarray:
    """Звучащий сигнал: синус на уровне обычной речи."""
    t = np.linspace(0, seconds, int(seconds * sample_rate), endpoint=False)
    return (0.2 * np.sin(2 * np.pi * 180 * t)).astype(np.float32)


def _silence(seconds: float, sample_rate: int = 16000) -> np.ndarray:
    return np.zeros(int(seconds * sample_rate), dtype=np.float32)


class GigaAMMLXChunkLossTest(unittest.TestCase):
    def _adapter(self):
        adapter = GigaAMMLXAdapter(mode="v3_e2e_rnnt")
        # Модель не грузим: тест про склейку кусков, а не про инференс.
        adapter._get_model = lambda: (object(), object())  # type: ignore[method-assign]
        adapter._warmup = lambda *a, **k: None  # type: ignore[method-assign]
        return adapter

    def _run(self, adapter, audio, pieces):
        seq = list(pieces)
        with patch.object(GigaAMMLXAdapter, "_infer_chunk", side_effect=lambda *a, **k: seq.pop(0)), \
             patch.dict(sys.modules, {"gigaam_mlx": object()}):
            return adapter.transcribe(audio, sample_rate=16000)

    def test_all_chunks_recognised_gives_joined_text(self):
        adapter = self._adapter()
        audio = np.concatenate([_speech(20.0), _speech(20.0)])
        result = self._run(adapter, audio, ["первая часть", "вторая часть"])
        self.assertEqual(result["text"], "первая часть вторая часть")

    def test_silent_chunk_without_text_is_normal(self):
        """Кусок из одной паузы обязан давать пустой текст — это не потеря."""
        adapter = self._adapter()
        audio = np.concatenate([_speech(20.0), _silence(20.0)])
        result = self._run(adapter, audio, ["первая часть", ""])
        self.assertEqual(result["text"], "первая часть")

    def test_speech_chunk_without_text_raises_instead_of_truncating(self):
        """Звучащий кусок без текста — потеря; молчать о ней нельзя.

        Исключение — единственный однозначный сигнал каскаду перейти к другому
        движку. Вернуть половину текста как успех означало бы отдать владельцу
        обрезанную диктовку без единого признака беды, что и случилось 02.09.
        """
        adapter = self._adapter()
        audio = np.concatenate([_speech(20.0), _speech(20.0)])
        with self.assertRaises(GigaAMMLXChunkLoss) as ctx:
            self._run(adapter, audio, ["первая часть", ""])
        message = str(ctx.exception)
        self.assertIn("1", message, "в сообщении должно быть число потерянных кусков")

    def test_single_chunk_empty_on_speech_also_raises(self):
        """Короткая запись — один кусок; потеря его текста так же недопустима."""
        adapter = self._adapter()
        with self.assertRaises(GigaAMMLXChunkLoss):
            self._run(adapter, _speech(5.0), [""])

    def test_single_silent_chunk_returns_empty_text(self):
        """Полная тишина — законный пустой результат, а не отказ движка."""
        adapter = self._adapter()
        result = self._run(adapter, _silence(5.0), [""])
        self.assertEqual(result["text"], "")


if __name__ == "__main__":
    unittest.main()
