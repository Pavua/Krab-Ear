"""Тесты GigaAMMLXAdapter (W-A волны gigaam-mlx-diar).

Все тесты работают БЕЗ реальной библиотеки gigaam_mlx (py3.12 ubuntu-parity):
в sys.modules подставляется фейковый модуль, teardown его снимает.
Ключевые инварианты из спека:
  * mlx-лок берётся по-чанково и не удерживается между чанками;
  * загрузка модели происходит ВНЕ лока;
  * все инференс-вызовы идут в ОДНОМ персистентном потоке (MLX платит
    прогрев графа per-thread) с таймаут-защитой; таймаут пересоздаёт поток;
  * первый transcribe делает один warmup-прогон;
  * lazy-import: конструктор не требует gigaam_mlx.
"""
import sys
import threading
import time
import types
import unittest
from unittest import mock

import numpy as np

from core.audio_chunker import AudioChunk
from core.mlx_subprocess import MLXTimeoutError
import core.pipeline.stt_gigaam_mlx as mod
from core.pipeline.stt_gigaam_mlx import GigaAMMLXAdapter


def _make_fake_gigaam_mlx(events, text_per_call="привет, мир."):
    """Фейковый модуль gigaam_mlx, пишущий события в общий список."""
    fake = types.ModuleType("gigaam_mlx")

    def load_model(model_type="ctc"):
        events.append(("load_model", model_type))
        return object(), object()

    def transcribe(model, tokenizer, path):
        events.append(("transcribe", path, threading.get_ident()))
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

    def _transcribes(self):
        return [e for e in self.events if e[0] == "transcribe"]

    def lock_depth_at(self, idx):
        depth = 0
        for ev in self.events[:idx + 1]:
            if ev[0] == "mlx_lock_enter":
                depth += 1
            elif ev[0] == "mlx_lock_exit":
                depth -= 1
        return depth

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

    def test_warmup_runs_once(self):
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(1))
        adapter.transcribe(self._audio())
        self.assertEqual(len(self._transcribes()), 2, "warmup + 1 чанк")
        adapter.transcribe(self._audio())
        self.assertEqual(len(self._transcribes()), 3, "повтор без warmup")

    def test_lock_taken_per_call_and_released_between(self):
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(3))
        adapter.transcribe(self._audio(seconds=3.0))
        enters = [e for e in self.events if e[0] == "mlx_lock_enter"]
        exits = [e for e in self.events if e[0] == "mlx_lock_exit"]
        self.assertEqual(len(enters), 4, "warmup + 3 чанка, лок на каждый")
        self.assertEqual(len(exits), 4)
        # Глубина в момент каждого входа == 1: лок не удерживался между чанками.
        self.assertTrue(all(depth == 1 for _, depth in enters), enters)
        # Каждый инференс-вызов происходит при захваченном локе.
        for i, ev in enumerate(self.events):
            if ev[0] == "transcribe":
                self.assertGreater(self.lock_depth_at(i), 0,
                                   "инференс обязан идти под mlx_lock")

    def test_model_loaded_outside_lock(self):
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(2))
        adapter.transcribe(self._audio(seconds=2.0))
        load_idx = next(i for i, e in enumerate(self.events) if e[0] == "load_model")
        self.assertEqual(self.lock_depth_at(load_idx), 0,
                         "загрузка модели обязана происходить вне mlx_lock")

    def test_inter_lock_is_outer(self):
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(1))
        adapter.transcribe(self._audio())
        seq = [e[0] for e in self.events
               if e[0] in ("inter_lock_enter", "mlx_lock_enter")]
        self.assertEqual(seq[:2], ["inter_lock_enter", "mlx_lock_enter"],
                         "порядок: межпроцессный flock снаружи, RLock внутри")

    def test_all_inference_in_one_persistent_thread(self):
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(3))
        adapter.transcribe(self._audio(seconds=3.0))
        idents = {e[2] for e in self._transcribes()}
        self.assertEqual(len(idents), 1,
                         "MLX платит прогрев per-thread: поток должен быть один")
        self.assertNotIn(threading.get_ident(), idents,
                         "инференс идёт не в вызывающем потоке (таймаут-защита)")

    def test_timeout_raises_and_recreates_executor(self):
        fake = sys.modules["gigaam_mlx"]
        orig = fake.transcribe

        def slow(model, tokenizer, path):
            time.sleep(0.5)
            return orig(model, tokenizer, path)

        fake.transcribe = slow
        adapter = GigaAMMLXAdapter(
            mode="rnnt", chunker=_FakeChunker(1), watchdog_timeout_sec=0.05,
        )
        with self.assertRaises(MLXTimeoutError):
            adapter.transcribe(self._audio())
        # После таймаута — чистый executor, быстрый вызов работает.
        fake.transcribe = orig
        adapter._watchdog_timeout_sec = 5.0
        result = adapter.transcribe(self._audio())
        self.assertEqual(result["text"], "привет, мир.")

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

    def test_temp_wavs_cleaned(self):
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(1))
        adapter.transcribe(self._audio())
        paths = [e[1] for e in self._transcribes()]
        self.assertEqual(len(paths), 2, "warmup + чанк")
        import os
        for p in paths:
            self.assertTrue(p.endswith(".wav"))
            self.assertFalse(os.path.exists(p), "temp wav должен удаляться")

    def test_chunks_joined_in_order(self):
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(3))
        adapter.transcribe(self._audio())  # warmup здесь, до подмены
        texts = iter(["Первый.", "Второй.", "Третий."])
        fake = sys.modules["gigaam_mlx"]
        orig = fake.transcribe

        def numbered(model, tokenizer, path):
            orig(model, tokenizer, path)
            return next(texts)

        fake.transcribe = numbered
        result = adapter.transcribe(self._audio(seconds=3.0))
        self.assertEqual(result["text"], "Первый. Второй. Третий.")

    def test_empty_text_returns_empty_without_raise(self):
        sys.modules["gigaam_mlx"] = _make_fake_gigaam_mlx(self.events, text_per_call="")
        adapter = GigaAMMLXAdapter(mode="rnnt", chunker=_FakeChunker(1))
        result = adapter.transcribe(self._audio())
        self.assertEqual(result["text"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
