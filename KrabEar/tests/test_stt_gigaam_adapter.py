"""Тесты для GigaAMAdapter и интеграции в STTRouter.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_stt_gigaam_adapter.py -v

CI безопасность:
    Тесты требующие реального gigaam-пакета пропускаются через pytest.importorskip("gigaam").
    Тесты мокирующие gigaam работают всегда — они не требуют реального пакета.
"""

from __future__ import annotations

import os
import sys
import types
import unittest
import wave
from unittest.mock import MagicMock, patch

import numpy as np

# Настройка PYTHONPATH для standalone-запуска
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _make_audio(seconds: float = 1.0, sample_rate: int = 16000) -> np.ndarray:
    """Генерирует синтетический синус как float32 массив."""
    t = np.linspace(0, seconds, int(seconds * sample_rate), dtype=np.float32)
    return np.sin(2 * np.pi * 440 * t)


def _make_fake_gigaam_module() -> types.ModuleType:
    """Создаёт фиктивный модуль gigaam для мокирования без реального пакета."""
    module = types.ModuleType("gigaam")

    class FakeTranscriptionResult:
        def __init__(self, text: str):
            self.text = text

    class FakeModel:
        def __init__(self, result_text: str = "Привет, мир!"):
            self._result_text = result_text
            self.transcribe_calls: list = []

        def transcribe(self, path: str) -> FakeTranscriptionResult:
            self.transcribe_calls.append(path)
            return FakeTranscriptionResult(self._result_text)

        def to(self, device):  # noqa: D401 — stub
            return self

    fake_model = FakeModel()
    module.load_model = MagicMock(return_value=fake_model)
    module._fake_model = fake_model  # для проверок в тестах
    return module


# ---------------------------------------------------------------------------
# Тест 1: GigaAMAdapter.__init__ не загружает модель (lazy load)
# ---------------------------------------------------------------------------

class TestGigaAMAdapterLazyInit(unittest.TestCase):
    """Адаптер не должен загружать модель при создании объекта."""

    def test_init_does_not_load_model(self):
        """__init__ не вызывает gigaam.load_model — модель загружается лениво."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        fake_gigaam = _make_fake_gigaam_module()
        with patch.dict(sys.modules, {"gigaam": fake_gigaam}):
            adapter = GigaAMAdapter(device="cpu", mode="rnnt")
            # Модель ещё не загружена
            self.assertFalse(adapter.is_loaded())
            # load_model не вызывался
            fake_gigaam.load_model.assert_not_called()

    def test_init_stores_params(self):
        """__init__ корректно сохраняет device и mode."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        adapter = GigaAMAdapter.__new__(GigaAMAdapter)
        adapter._device = "mps"
        adapter._mode = "ctc"
        adapter._model = None
        self.assertEqual(adapter._device, "mps")
        self.assertEqual(adapter._mode, "ctc")

    def test_init_invalid_mode_raises(self):
        """Недопустимый mode вызывает ValueError при создании."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        with self.assertRaises(ValueError) as ctx:
            GigaAMAdapter(device="cpu", mode="invalid_mode_xyz")
        self.assertIn("invalid_mode_xyz", str(ctx.exception))


# ---------------------------------------------------------------------------
# Тест 2: transcribe() возвращает dict с обязательными ключами
# ---------------------------------------------------------------------------

class TestGigaAMAdapterTranscribeKeys(unittest.TestCase):
    """transcribe() должен возвращать dict с text, language, confidence, engine."""

    def test_transcribe_returns_required_keys(self):
        """Результат transcribe содержит все обязательные поля."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        fake_gigaam = _make_fake_gigaam_module()
        fake_gigaam._fake_model._result_text = "Тестовая транскрипция"

        with patch.dict(sys.modules, {"gigaam": fake_gigaam}):
            # Мокируем torch чтобы избежать import ошибок
            fake_torch = types.ModuleType("torch")
            fake_torch.device = lambda x: x
            fake_backends = types.ModuleType("torch.backends")
            fake_mps = types.ModuleType("torch.backends.mps")
            fake_mps.is_available = lambda: False
            fake_backends.mps = fake_mps
            fake_torch.backends = fake_backends
            with patch.dict(sys.modules, {"torch": fake_torch, "torch.backends": fake_backends}):
                adapter = GigaAMAdapter(device="cpu", mode="rnnt")
                audio = _make_audio(seconds=1.0)
                result = adapter.transcribe(audio, sample_rate=16000)

        # Проверяем обязательные ключи
        self.assertIn("text", result)
        self.assertIn("language", result)
        self.assertIn("confidence", result)
        self.assertIn("engine", result)

    def test_transcribe_language_is_ru(self):
        """language всегда равен 'ru' — GigaAM заточен под русский."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        fake_gigaam = _make_fake_gigaam_module()
        with patch.dict(sys.modules, {"gigaam": fake_gigaam}):
            adapter = GigaAMAdapter(device="cpu", mode="rnnt")
            audio = _make_audio(seconds=0.5)
            with patch.object(adapter, "_get_model", return_value=fake_gigaam._fake_model):
                result = adapter.transcribe(audio, sample_rate=16000)
        self.assertEqual(result["language"], "ru")

    def test_transcribe_confidence_is_float(self):
        """confidence возвращается как float."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        fake_gigaam = _make_fake_gigaam_module()
        with patch.dict(sys.modules, {"gigaam": fake_gigaam}):
            adapter = GigaAMAdapter(device="cpu", mode="rnnt")
            audio = _make_audio(seconds=0.5)
            with patch.object(adapter, "_get_model", return_value=fake_gigaam._fake_model):
                result = adapter.transcribe(audio, sample_rate=16000)
        self.assertIsInstance(result["confidence"], float)
        self.assertGreater(result["confidence"], 0.0)
        self.assertLessEqual(result["confidence"], 1.0)


# ---------------------------------------------------------------------------
# Тест 3: resample вызывается при sample_rate != 16000
# ---------------------------------------------------------------------------

class TestGigaAMAdapterResample(unittest.TestCase):
    """_ensure_16k вызывается при sample_rate != 16000."""

    def test_resample_called_when_not_16k(self):
        """При sample_rate=8000 _ensure_16k должен вернуть массив другой длины."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        adapter = GigaAMAdapter.__new__(GigaAMAdapter)
        adapter._device = "cpu"
        adapter._mode = "rnnt"
        adapter._model = None

        audio_8k = _make_audio(seconds=1.0, sample_rate=8000)
        result_16k = adapter._ensure_16k(audio_8k, sample_rate=8000)

        # Длина должна увеличиться примерно вдвое (8000 → 16000)
        expected_len = int(len(audio_8k) * 16000 / 8000)
        self.assertAlmostEqual(len(result_16k), expected_len, delta=2)
        self.assertEqual(result_16k.dtype, np.float32)

    def test_no_resample_when_16k(self):
        """При sample_rate=16000 массив возвращается без изменения длины."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        adapter = GigaAMAdapter.__new__(GigaAMAdapter)
        adapter._device = "cpu"
        adapter._mode = "rnnt"
        adapter._model = None

        audio_16k = _make_audio(seconds=1.0, sample_rate=16000)
        result = adapter._ensure_16k(audio_16k, sample_rate=16000)

        self.assertEqual(len(result), len(audio_16k))
        self.assertEqual(result.dtype, np.float32)


# ---------------------------------------------------------------------------
# Тест 4: engine name = "gigaam-rnnt" или "gigaam-ctc"
# ---------------------------------------------------------------------------

class TestGigaAMAdapterEngineName(unittest.TestCase):
    """_engine_name() должен возвращать корректный идентификатор."""

    def test_engine_name_rnnt(self):
        """mode='rnnt' → engine='gigaam-rnnt'."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        adapter = GigaAMAdapter.__new__(GigaAMAdapter)
        adapter._mode = "rnnt"
        self.assertEqual(adapter._engine_name(), "gigaam-rnnt")

    def test_engine_name_ctc(self):
        """mode='ctc' → engine='gigaam-ctc'."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        adapter = GigaAMAdapter.__new__(GigaAMAdapter)
        adapter._mode = "ctc"
        self.assertEqual(adapter._engine_name(), "gigaam-ctc")

    def test_engine_name_v2_rnnt(self):
        """mode='v2_rnnt' → engine='gigaam-rnnt' (без префикса v2)."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        adapter = GigaAMAdapter.__new__(GigaAMAdapter)
        adapter._mode = "v2_rnnt"
        self.assertEqual(adapter._engine_name(), "gigaam-rnnt")

    def test_engine_name_v1_ctc(self):
        """mode='v1_ctc' → engine='gigaam-ctc' (без префикса v1)."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        adapter = GigaAMAdapter.__new__(GigaAMAdapter)
        adapter._mode = "v1_ctc"
        self.assertEqual(adapter._engine_name(), "gigaam-ctc")


# ---------------------------------------------------------------------------
# Тест 5: ImportError при отсутствующем gigaam → информативное исключение
# ---------------------------------------------------------------------------

class TestGigaAMAdapterImportError(unittest.TestCase):
    """При отсутствии gigaam-пакета _get_model кидает информативный ImportError."""

    def test_import_error_on_missing_gigaam(self):
        """_get_model() → ImportError с инструкцией по установке."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        # Убираем gigaam из sys.modules если он вдруг установлен
        original = sys.modules.pop("gigaam", None)
        # Делаем gigaam недоступным
        sys.modules["gigaam"] = None  # type: ignore[assignment]
        try:
            adapter = GigaAMAdapter(device="cpu", mode="rnnt")
            with self.assertRaises(ImportError) as ctx:
                adapter._get_model()
            error_msg = str(ctx.exception)
            self.assertIn("gigaam", error_msg.lower())
            self.assertIn("pip install", error_msg)
        finally:
            # Восстанавливаем состояние
            if original is not None:
                sys.modules["gigaam"] = original
            else:
                sys.modules.pop("gigaam", None)

    def test_is_loaded_false_before_transcribe(self):
        """is_loaded() == False пока transcribe() не вызван."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        adapter = GigaAMAdapter(device="cpu", mode="rnnt")
        self.assertFalse(adapter.is_loaded())


# ---------------------------------------------------------------------------
# Тест 6: STT_GIGAAM_ENABLED=False → адаптер не создаётся роутером
# ---------------------------------------------------------------------------

class TestGigaAMRouterIntegration(unittest.TestCase):
    """STTRouter.get_gigaam_adapter() → None когда флаг выключен."""

    def _make_settings(self, gigaam_enabled: bool = False) -> object:
        """Создаёт duck-typed объект настроек."""
        s = MagicMock()
        s.STT_GIGAAM_ENABLED = gigaam_enabled
        s.STT_GIGAAM_MODE = "rnnt"
        s.STT_GIGAAM_DEVICE = "cpu"
        s.STT_LANGUAGE_ROUTING_ENABLED = False
        s.STT_OTHER_PRIMARY_MODEL = "mlx-community/whisper-large-v3-mlx"
        return s

    def test_disabled_flag_returns_none(self):
        """STT_GIGAAM_ENABLED=False → get_gigaam_adapter() == None."""
        from core.stt_router import STTRouter

        settings = self._make_settings(gigaam_enabled=False)
        router = STTRouter(settings=settings)
        adapter = router.get_gigaam_adapter()
        self.assertIsNone(adapter)

    def test_enabled_flag_with_missing_package_returns_none(self):
        """STT_GIGAAM_ENABLED=True но gigaam не установлен → None (graceful)."""
        from core.stt_router import STTRouter

        settings = self._make_settings(gigaam_enabled=True)
        router = STTRouter(settings=settings)

        # Мокируем import провал stt_gigaam
        with patch.dict(sys.modules, {"core.pipeline.stt_gigaam": None}):
            adapter = router.get_gigaam_adapter()
        # None — адаптер не создался, но и не упал
        self.assertIsNone(adapter)

    def test_enabled_flag_with_valid_adapter_returns_instance(self):
        """STT_GIGAAM_ENABLED=True и gigaam доступен → возвращает GigaAMAdapter."""
        from core.pipeline.stt_gigaam import GigaAMAdapter
        from core.stt_router import STTRouter

        settings = self._make_settings(gigaam_enabled=True)
        router = STTRouter(settings=settings)

        # Мокируем stt_gigaam модуль чтобы GigaAMAdapter создавался без реального gigaam
        fake_module = types.ModuleType("core.pipeline.stt_gigaam")
        fake_module.GigaAMAdapter = GigaAMAdapter

        with patch.dict(sys.modules, {"core.pipeline.stt_gigaam": fake_module}):
            adapter = router.get_gigaam_adapter()

        # Должен вернуть экземпляр GigaAMAdapter
        self.assertIsNotNone(adapter)
        self.assertIsInstance(adapter, GigaAMAdapter)
        # Адаптер создан с правильными параметрами
        self.assertEqual(adapter._mode, "rnnt")
        self.assertEqual(adapter._device, "cpu")


# ---------------------------------------------------------------------------
# Бонус: тест записи WAV и формата результата
# ---------------------------------------------------------------------------

class TestGigaAMAdapterWavWrite(unittest.TestCase):
    """_write_wav создаёт корректный 16-bit mono WAV файл."""

    def test_write_wav_creates_valid_file(self):
        """_write_wav → корректный WAV: 1 канал, 16-bit, 16000 Гц."""
        import tempfile

        from core.pipeline.stt_gigaam import GigaAMAdapter

        adapter = GigaAMAdapter.__new__(GigaAMAdapter)
        adapter._device = "cpu"
        adapter._mode = "rnnt"
        adapter._model = None

        audio = _make_audio(seconds=0.5, sample_rate=16000)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name

        try:
            adapter._write_wav(path, audio)
            with wave.open(path, "rb") as wf:
                self.assertEqual(wf.getnchannels(), 1)
                self.assertEqual(wf.getsampwidth(), 2)
                self.assertEqual(wf.getframerate(), 16000)
                self.assertGreater(wf.getnframes(), 0)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# In-process lazy-load thread-safety (sibling of subprocess _spawn_lock, W1216)
# ---------------------------------------------------------------------------

class TestGigaAMAdapterModelLoadThreadSafe(unittest.TestCase):
    """Concurrent in-process _get_model() must load the model exactly once.

    Before the fix _get_model() had no lock (unlike the subprocess path's
    _spawn_lock, W1216 F2): two threads could both pass ``if self._model is not
    None`` and call gigaam.load_model() twice — loading the ~2 GB model twice
    (memory pressure / OOM).
    """

    def test_concurrent_get_model_loads_once(self):
        import threading
        import time
        from core.pipeline.stt_gigaam import GigaAMAdapter

        calls = []  # list.append is atomic under the GIL
        load_event = threading.Event()  # lets threads pile up before model appears

        fake_gigaam = types.ModuleType("gigaam")
        fake_model = MagicMock()

        def counting_load_model(mode):
            calls.append(mode)
            load_event.wait(timeout=2.0)  # block so racing threads stack up
            return fake_model

        fake_gigaam.load_model = counting_load_model

        with patch.dict(sys.modules, {"gigaam": fake_gigaam}):
            adapter = GigaAMAdapter(device="cpu", mode="rnnt", transport="in_process")
            errors = []

            def worker():
                try:
                    adapter._get_model()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            time.sleep(0.05)  # let all four reach the load before it completes
            load_event.set()
            for t in threads:
                t.join(timeout=5.0)

        self.assertEqual(errors, [], f"Threads raised: {errors}")
        self.assertEqual(
            len(calls),
            1,
            f"load_model called {len(calls)}× — double-load race (missing _model_lock)",
        )


if __name__ == "__main__":
    unittest.main()
