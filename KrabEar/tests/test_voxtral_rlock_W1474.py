"""Тесты double-checked locking для _load_voxtral_model (W1472 F1 HIGH).

Проверяем что конкурентные вызовы _load_voxtral_model() не приводят к двойной
загрузке модели (~2-3 GB) при одновременных IPC-запросах. Паттерн аналогичен
TestPyannoteDoubleCheckedLock / TestWhisperXDoubleCheckedLock (W1235).

Все тяжёлые зависимости (mistral-inference, huggingface_hub) мокаются —
реальная загрузка модели не требуется.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_engine_no_gigaam() -> "AudioEngine":  # noqa: F821
    """Создаёт AudioEngine без GigaAM warmup-потока."""
    from core.engine import AudioEngine
    return AudioEngine(skip_gigaam_warmup=True)


_VOXTRAL_REPO = "mistralai/Voxtral-Mini-4B-Realtime-2502"


class TestVoxtralModelInitState(unittest.TestCase):
    """Проверяем что _voxtral_model/_voxtral_load_error/_voxtral_load_lock
    инициализируются в __init__ (W1472 F1 — ранее поля отсутствовали)."""

    def test_voxtral_model_init_state(self) -> None:
        """AudioEngine.__init__ должен задавать три поля Voxtral-стейта."""
        engine = _make_engine_no_gigaam()
        # Поля должны существовать (не через getattr-fallback)
        self.assertIn("_voxtral_model", engine.__dict__,
                      "_voxtral_model должен быть в __dict__, а не через getattr")
        self.assertIn("_voxtral_load_error", engine.__dict__,
                      "_voxtral_load_error должен быть в __dict__")
        self.assertIn("_voxtral_load_lock", engine.__dict__,
                      "_voxtral_load_lock должен быть в __dict__")

    def test_voxtral_model_init_none(self) -> None:
        """_voxtral_model и _voxtral_load_error должны быть None после __init__."""
        engine = _make_engine_no_gigaam()
        self.assertIsNone(engine._voxtral_model)
        self.assertIsNone(engine._voxtral_load_error)

    def test_voxtral_lock_is_rlock(self) -> None:
        """_voxtral_load_lock должен быть threading.RLock."""
        engine = _make_engine_no_gigaam()
        # threading.RLock() возвращает _RLock внутренний тип — проверяем через acquire/release
        lock = engine._voxtral_load_lock
        self.assertTrue(hasattr(lock, "acquire"), "_voxtral_load_lock должен быть lock-объектом")
        self.assertTrue(hasattr(lock, "release"))
        # RLock поддерживает реентерабельный захват из одного потока
        acquired = lock.acquire(blocking=False)
        self.assertTrue(acquired, "Новый RLock должен быть свободен")
        reacquired = lock.acquire(blocking=False)  # реентерабельность
        self.assertTrue(reacquired, "RLock должен допускать повторный захват из того же потока")
        lock.release()
        lock.release()


class TestVoxtralLoaderPatternMatchesSenseVoice(unittest.TestCase):
    """Паттерн _load_voxtral_model должен соответствовать SenseVoice/WhisperX (W1235)."""

    def test_voxtral_loader_fast_path_returns_cached_model(self) -> None:
        """Если _voxtral_model уже установлен — загрузка не вызывается."""
        engine = _make_engine_no_gigaam()
        fake_model = (MagicMock(name="voxtral_model"), MagicMock(name="voxtral_tokenizer"))
        engine._voxtral_model = fake_model

        with patch("core.engine._voxtral_available", True), \
                patch("core.engine.settings") as mock_settings:
            mock_settings.VOXTRAL_MODEL = _VOXTRAL_REPO
            result = engine._load_voxtral_model()

        self.assertIs(result, fake_model, "Fast-path должен вернуть cached model без перезагрузки")

    def test_voxtral_loader_raises_on_cached_error(self) -> None:
        """Если _voxtral_load_error установлен — повторный вызов возбуждает RuntimeError."""
        engine = _make_engine_no_gigaam()
        engine._voxtral_load_error = "Тест: модель недоступна"

        with patch("core.engine._voxtral_available", True):
            with self.assertRaises(RuntimeError) as ctx:
                engine._load_voxtral_model()
        self.assertIn("модель недоступна", str(ctx.exception))

    def test_voxtral_loader_sets_error_when_unavailable(self) -> None:
        """_voxtral_available=False → _voxtral_load_error устанавливается и raises."""
        engine = _make_engine_no_gigaam()

        with patch("core.engine._voxtral_available", False):
            with self.assertRaises(RuntimeError) as ctx:
                engine._load_voxtral_model()

        self.assertIsNotNone(engine._voxtral_load_error)
        self.assertIn("mistral-inference", engine._voxtral_load_error)
        self.assertIn("mistral-inference", str(ctx.exception))

    def test_voxtral_loader_sets_error_on_load_failure(self) -> None:
        """Исключение во время загрузки → _voxtral_load_error устанавливается."""
        engine = _make_engine_no_gigaam()

        fake_hf_hub = MagicMock()
        fake_hf_hub.snapshot_download.side_effect = RuntimeError("network timeout")

        with patch("core.engine._voxtral_available", True), \
                patch("core.engine.settings") as mock_settings, \
                patch("core.engine._VOXTRAL_REPO_ALLOWLIST", {_VOXTRAL_REPO}), \
                patch.dict("sys.modules", {"huggingface_hub": fake_hf_hub}):

            mock_settings.VOXTRAL_MODEL = _VOXTRAL_REPO
            with self.assertRaises(RuntimeError):
                engine._load_voxtral_model()

        self.assertIsNotNone(engine._voxtral_load_error)
        self.assertIn("Voxtral", engine._voxtral_load_error)


class TestVoxtralConcurrentLoadsDoubleCheckedLock(unittest.TestCase):
    """Проверяем что 10 конкурентных потоков вызывают загрузку ровно 1 раз."""

    def test_voxtral_concurrent_loads_double_checked_lock(self) -> None:
        """10 потоков одновременно вызывают _load_voxtral_model.

        Ожидаем: snapshot_download вызван ровно 1 раз (не 10).
        Все 10 потоков получают одинаковый объект модели (одна и та же пара).
        """
        engine = _make_engine_no_gigaam()

        load_call_count = 0
        call_count_lock = threading.Lock()
        fake_model_obj = MagicMock(name="voxtral_transformer")
        fake_tokenizer_obj = MagicMock(name="voxtral_tokenizer")

        def counting_snapshot_download(repo_id: str) -> str:
            nonlocal load_call_count
            with call_count_lock:
                load_call_count += 1
            time.sleep(0.03)  # имитация медленной загрузки из HuggingFace
            return "/tmp/fake_voxtral_path"

        fake_tokenizer_cls = MagicMock()
        fake_tokenizer_cls.from_file.return_value = fake_tokenizer_obj
        fake_transformer_cls = MagicMock()
        fake_transformer_cls.from_folder.return_value = fake_model_obj

        results: list = []
        errors: list = []
        results_lock = threading.Lock()

        def load_in_thread():
            try:
                m = engine._load_voxtral_model()
                with results_lock:
                    results.append(m)
            except Exception as exc:  # pragma: no cover
                with results_lock:
                    errors.append(exc)

        fake_hf_hub = MagicMock()
        fake_hf_hub.snapshot_download.side_effect = counting_snapshot_download

        n_threads = 10
        with patch("core.engine._voxtral_available", True), \
                patch("core.engine._VOXTRAL_REPO_ALLOWLIST", {_VOXTRAL_REPO}), \
                patch("core.engine.settings") as mock_settings, \
                patch("core.engine._VoxtralTokenizer", fake_tokenizer_cls), \
                patch("core.engine._VoxtralTransformer", fake_transformer_cls), \
                patch.dict("sys.modules", {"huggingface_hub": fake_hf_hub}):
            mock_settings.VOXTRAL_MODEL = _VOXTRAL_REPO

            threads = [
                threading.Thread(target=load_in_thread, name=f"voxtral-t{i}")
                for i in range(n_threads)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=8.0)

        self.assertFalse(errors, f"Потоки завершились с ошибками: {errors}")
        self.assertEqual(
            load_call_count, 1,
            f"snapshot_download вызван {load_call_count} раз вместо 1 при {n_threads} потоках — "
            "double-checked lock сломан"
        )
        self.assertEqual(len(results), n_threads,
                         f"Не все {n_threads} потоков получили результат: {len(results)}")
        # Все потоки получают идентичную пару (тот же объект из engine._voxtral_model)
        first = results[0]
        for r in results:
            self.assertIs(r, first,
                          "Все потоки должны получить один и тот же объект (model, tokenizer)")


if __name__ == "__main__":
    unittest.main()
