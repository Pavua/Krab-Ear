"""Тесты GigaAMMLXAdapter (W-A волны gigaam-mlx-diar).

Все тесты работают БЕЗ реальной библиотеки gigaam_mlx (py3.12 ubuntu-parity):
в sys.modules подставляется фейковый модуль, teardown его снимает.
Ключевые инварианты из спека:
  * mlx-лок берётся по-чанково и не удерживается между чанками;
  * загрузка модели происходит ВНЕ лока;
  * каждый инференс-вызов идёт через MLX watchdog;
  * lazy-import: конструктор не требует gigaam_mlx.
"""
import sys
import types
import unittest
from unittest import mock

import numpy as np

from core.audio_chunker import AudioChunk
from core.pipeline import stt_gigaam_mlx as mod
from core.pipeline.stt_gigaam_mlx import GigaAMMLXAdapter


def _make_fake_gigaam_mlx(events, text_per_call="привет, мир."):
    """Фейковый модуль gigaam_mlx, пишущий события в общий список."""
    fake = types.ModuleType("gigaam_mlx")

    def load_model(model_type="ctc"):
        events.append(("load_model", model_type))
        return object(), object()

    def transcribe(model, tokenizer, path):
        events.append(("transcribe", path))
        return text_per_call

    fake.load_model = load_model
    fake.transcribe = transcribe
    return fake


class _FakeLockCtx:
    """Подменный mlx_lock/inter_lock: считает входы-выходы и пишет события."""

    def __init__(self, events, name):
        self._events = events
        self._name = name
        self.depth = 0

    def __enter__(self):
        self.depth += 1
        self._events.append((f"{self._name}_enter", self.depth))
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        self._events.append((f"{self._name}_exit", self.depth))
        return False


class _FakeChunker:
    """Возвращает заранее заданное число чанков."""

    def __init__(self, n_chunks):
        self._n = n_chunks

    def chunk(self, audio, sample_rate, max_chunk_sec=30.0):
        assert max_chunk_sec == 20.0, "склейка обязана резать по 20 c (hard limit 25 c)"
        size = max(1, len(audio) // self._n)
        return [
            AudioChunk(
                audio=audio[i * size:(i + 1) * size] if i < self._n - 1 else audio[i * size:],
                start_sec=float(i),
                end_sec=float(i + 1),
                index=i,
            )
            for i in range(self._n)
        ]


class _FakeWatchdog:
    def __init__(self, events):
        self._events = events

    def run_with_timeout(self, fn, timeout_sec, model_name):
        self._events.append(("watchdog", model_name, timeout_sec))
        return fn()


class TestGigaAMMLXAdapter(unittest.TestCase):
    def setUp(self):
        self.events = []
        self._saved_module = sys.modules.get("gigaam_mlx")
        sys.modules["gigaam_mlx"] = _make_fake_gigaam_mlx(self.events)
        self.lock_ctx = _FakeLockCtx(self.events, "mlx_lock")
        self.inter_ctx = _FakeLockCtx(self.events, "inter_lock")
        self._patches = [
            mock.patch.object(mod, "mlx_lock", lambda: self.lock_ctx),
            mock.patch.object(mod, "mlx_inter_process_lock", lambda: self.inter_ctx),
            mock.patch.object(mod, "get_watchdog", lambda: _FakeWatchdog(self.events)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        # Возвращаем sys.modules в исходное состояние (без order-dependence).
        if self._saved_module is None:
            sys.modules.pop("gigaam_mlx", None)
        else:
            sys.modules["gigaam_mlx"] = self._saved_module

    def _audio(self, seconds=1.0):
        return np.zeros(int(16000 * seconds), dtype=np.float32)

    # ------------------------------------------------------------------

    def test_result_contract(self):
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(1))
        result = adapter.transcribe(self._audio())
        self.assertEqual(result["text"], "привет, мир.")
        self.assertEqual(result["language"], "ru")
        self.assertEqual(result["engine"], "gigaam-mlx-rnnt")
        self.assertTrue(result["native_punctuation"])
        self.assertIsInstance(result["confidence"], float)

    def test_mode_mapping_and_engine_name(self):
        adapter = GigaAMMLXAdapter(mode="v3_e2e_ctc", chunker=_FakeChunker(1))
        result = adapter.transcribe(self._audio())
        load_calls = [e for e in self.events if e[0] == "load_model"]
        self.assertEqual(load_calls, [("load_model", "ctc")])
        self.assertEqual(result["engine"], "gigaam-mlx-ctc")

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            GigaAMMLXAdapter(mode="whisper")

    def test_lock_taken_per_chunk_and_released_between(self):
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(3))
        adapter.transcribe(self._audio(seconds=3.0))
        enters = [e for e in self.events if e[0] == "mlx_lock_enter"]
        exits = [e for e in self.events if e[0] == "mlx_lock_exit"]
        self.assertEqual(len(enters), 3, "лок должен браться на каждый чанк")
        self.assertEqual(len(exits), 3)
        # Глубина в момент каждого входа == 1: лок не удерживался между чанками.
        self.assertTrue(all(depth == 1 for _, depth in enters), enters)
        # Каждый transcribe-вызов происходит при захваченном локе.
        for i, ev in enumerate(self.events):
            if ev[0] == "transcribe":
                self.assertGreater(
                    self.lock_ctx_depth_at(i), 0,
                    "инференс обязан идти под mlx_lock")

    def lock_ctx_depth_at(self, idx):
        depth = 0
        for ev in self.events[:idx + 1]:
            if ev[0] == "mlx_lock_enter":
                depth += 1
            elif ev[0] == "mlx_lock_exit":
                depth -= 1
        return depth

    def test_model_loaded_outside_lock(self):
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(2))
        adapter.transcribe(self._audio(seconds=2.0))
        load_idx = next(i for i, e in enumerate(self.events) if e[0] == "load_model")
        self.assertEqual(
            self.lock_ctx_depth_at(load_idx), 0,
            "загрузка модели обязана происходить вне mlx_lock")

    def test_inter_lock_is_outer(self):
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(1))
        adapter.transcribe(self._audio())
        seq = [e[0] for e in self.events
               if e[0] in ("inter_lock_enter", "mlx_lock_enter")]
        self.assertEqual(seq[:2], ["inter_lock_enter", "mlx_lock_enter"],
                         "порядок: межпроцессный flock снаружи, RLock внутри")

    def test_every_inference_under_watchdog(self):
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(3))
        adapter.transcribe(self._audio(seconds=3.0))
        watchdog_calls = [e for e in self.events if e[0] == "watchdog"]
        transcribe_calls = [e for e in self.events if e[0] == "transcribe"]
        self.assertEqual(len(watchdog_calls), len(transcribe_calls))
        self.assertEqual(len(watchdog_calls), 3)

    def test_lazy_import_constructor_without_library(self):
        sys.modules.pop("gigaam_mlx", None)
        adapter = GigaAMMLXAdapter(mode="rnnt")  # не должен импортировать
        self.assertFalse(adapter.is_loaded())

    def test_close_unloads_and_reload_works(self):
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(1))
        adapter.transcribe(self._audio())
        self.assertTrue(adapter.is_loaded())
        adapter.close()
        self.assertFalse(adapter.is_loaded())
        adapter.transcribe(self._audio())
        load_calls = [e for e in self.events if e[0] == "load_model"]
        self.assertEqual(len(load_calls), 2, "после close() модель перезагружается")

    def test_temp_wav_passed_and_cleaned(self):
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(1))
        adapter.transcribe(self._audio())
        paths = [e[1] for e in self.events if e[0] == "transcribe"]
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith(".wav"))
        import os
        self.assertFalse(os.path.exists(paths[0]), "temp wav должен удаляться")

    def test_chunks_joined_in_order(self):
        texts = iter(["Первый.", "Второй.", "Третий."])
        fake = sys.modules["gigaam_mlx"]
        orig = fake.transcribe

        def numbered(model, tokenizer, path):
            orig(model, tokenizer, path)
            return next(texts)

        fake.transcribe = numbered
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(3))
        result = adapter.transcribe(self._audio(seconds=3.0))
        self.assertEqual(result["text"], "Первый. Второй. Третий.")

    def test_empty_text_returns_empty_without_raise(self):
        sys.modules["gigaam_mlx"] = _make_fake_gigaam_mlx(self.events, text_per_call="")
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(1))
        result = adapter.transcribe(self._audio())
        self.assertEqual(result["text"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
