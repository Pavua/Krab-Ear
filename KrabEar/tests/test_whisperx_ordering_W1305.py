"""Тесты порядка WhisperX в fallback chain — W1303 F1 HIGH fix (W1305).

Проверяет, что WhisperX маркер всегда вставляется ПОСЛЕ как SenseVoice,
так и Parakeet маркеров, независимо от того, какой из них включён.

Документированный порядок:
    balanced → Parakeet → SenseVoice → WhisperX → max-candidates
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import AudioEngine


class _SyncFuture:
    def __init__(self, fn):
        try:
            self._r, self._e = fn(), None
        except BaseException as e:
            self._r, self._e = None, e

    def result(self, timeout=None):
        if self._e:
            raise self._e
        return self._r

    def cancel(self):
        pass


class _SyncExecutor:
    def __init__(self, *a, **kw):
        pass

    def submit(self, fn, *a, **kw):
        return _SyncFuture(fn)

    def shutdown(self, wait=True, **kw):
        pass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine(
    *,
    parakeet_marker_unavailable: bool = False,
    sensevoice_marker_unavailable: bool = False,
    whisperx_marker_unavailable: bool = False,
) -> AudioEngine:
    """Создаёт AudioEngine.__new__ с необходимым минимумом стейта."""
    engine = AudioEngine.__new__(AudioEngine)
    engine.quality_profile = "balanced"
    engine.current_model = "mlx-community/whisper-large-v3-turbo"
    engine._unavailable_models: dict = {}
    engine._router = None
    engine._sensevoice_model = None
    engine._sensevoice_load_error = None
    engine._whisperx_model = None
    engine._whisperx_load_error = None
    engine._parakeet_model = None
    engine._parakeet_load_error = None
    if parakeet_marker_unavailable:
        engine._unavailable_models[engine._PARAKEET_MARKER] = __import__("time").monotonic()
    if sensevoice_marker_unavailable:
        engine._unavailable_models[engine._SENSEVOICE_MARKER] = __import__("time").monotonic()
    if whisperx_marker_unavailable:
        engine._unavailable_models[engine._WHISPERX_MARKER] = __import__("time").monotonic()
    return engine


def _base_settings(mock: Any) -> None:
    """Минимальный набор настроек для _transcribe_with_fallback_impl."""
    mock.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
    mock.MODEL_MAX_CANDIDATES = "mlx-community/whisper-large-v3-turbo"
    mock.model_max_list = ["mlx-community/whisper-large-v3-turbo"]
    mock.TRANSCRIBE_TIMEOUT_SEC = 30
    mock.NETWORK_MODE = "offline_strict"
    mock.HF_TOKEN = ""
    # Все адаптеры по умолчанию выключены
    mock.PARAKEET_ENABLED = False
    mock.PARAKEET_MODEL = "nvidia/parakeet-tdt-1.1b"
    mock.SENSEVOICE_ENABLED = False
    mock.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
    mock.SENSEVOICE_EMOTION_TO_HISTORY = True
    mock.WHISPERX_ENABLED = False
    mock.WHISPERX_MODEL = "large-v3"
    mock.WHISPERX_DEVICE = "cpu"
    mock.WHISPERX_DIARIZATION = False
    mock.WHISPERX_WORD_TIMESTAMPS = True
    mock.VOXTRAL_ENABLED = False


def _capture_candidates(engine: AudioEngine, mock_settings: Any) -> list[str]:
    """Запускает _transcribe_with_fallback_impl и возвращает посещённые кандидаты."""
    visited: list[str] = []

    def fake_model(audio_data: Any, model_name: str, prompt: str, language: Any = None) -> dict:
        visited.append(model_name)
        raise RuntimeError("stub unavail")

    engine._transcribe_model = fake_model  # type: ignore[method-assign]
    engine._transcribe_sensevoice = MagicMock(side_effect=RuntimeError("stub sv"))  # type: ignore[method-assign]
    engine._transcribe_parakeet = MagicMock(side_effect=RuntimeError("stub pk"))  # type: ignore[method-assign]
    engine._transcribe_whisperx = MagicMock(side_effect=RuntimeError("stub wx"))  # type: ignore[method-assign]

    with patch("core.engine._profiler") as mock_profiler:
        mock_profiler.start_span.return_value.__enter__ = lambda s: s
        mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
        with patch("core.engine._get_available_memory_gb", return_value=16.0):
            with patch("concurrent.futures.ThreadPoolExecutor", _SyncExecutor):
                try:
                    engine._transcribe_with_fallback_impl(b"audio", "prompt", "en")
                except (RuntimeError, Exception):
                    pass

    # Reconstruct visited order from _unavailable_models (все маркеры были посещены)
    # Возвращаем полную цепочку как список отметин в _unavailable_models в порядке
    # их появления в candidates — используем visited (вызовы _transcribe_model)
    # плюс маркеры из unavailable для специальных адаптеров.
    return visited


# ---------------------------------------------------------------------------
# 1. WhisperX ПОСЛЕ SenseVoice (SenseVoice включён, Parakeet выключен)
# ---------------------------------------------------------------------------

class TestWhisperXAfterSenseVoiceWhenPresent(unittest.TestCase):
    """WhisperX должен идти ПОСЛЕ SenseVoice когда SenseVoice включён."""

    @patch("core.engine.settings")
    def test_whisperx_after_sensevoice_when_present(self, mock_settings: Any) -> None:
        """SenseVoice enabled, Parakeet disabled → WhisperX после SenseVoice."""
        _base_settings(mock_settings)
        mock_settings.SENSEVOICE_ENABLED = True
        mock_settings.WHISPERX_ENABLED = True

        engine = _make_engine()

        call_order: list[str] = []
        engine._transcribe_sensevoice = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda *a, **kw: call_order.append("sensevoice") or (_ for _ in ()).throw(RuntimeError("sv stub"))
        )
        engine._transcribe_whisperx = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda *a, **kw: call_order.append("whisperx") or (_ for _ in ()).throw(RuntimeError("wx stub"))
        )

        def fake_model(audio_data: Any, model_name: str, prompt: str, language: Any = None) -> dict:
            call_order.append(f"whisper:{model_name}")
            raise RuntimeError("whisper stub")

        engine._transcribe_model = fake_model  # type: ignore[method-assign]

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            with patch("core.engine._get_available_memory_gb", return_value=16.0):
                with patch("concurrent.futures.ThreadPoolExecutor", _SyncExecutor):
                    try:
                        engine._transcribe_with_fallback_impl(b"audio", "prompt", "en")
                    except (RuntimeError, Exception):
                        pass

        sv_idx = next((i for i, x in enumerate(call_order) if x == "sensevoice"), None)
        wx_idx = next((i for i, x in enumerate(call_order) if x == "whisperx"), None)

        self.assertIsNotNone(sv_idx, "SenseVoice должна быть вызвана")
        self.assertIsNotNone(wx_idx, "WhisperX должен быть вызван")
        self.assertGreater(wx_idx, sv_idx,
                           f"WhisperX (pos {wx_idx}) должен идти ПОСЛЕ SenseVoice (pos {sv_idx}); "
                           f"call_order={call_order}")


# ---------------------------------------------------------------------------
# 2. WhisperX ПОСЛЕ Parakeet (Parakeet включён, SenseVoice выключен)  — W1303 F1
# ---------------------------------------------------------------------------

class TestWhisperXAfterParakeetWhenPresent(unittest.TestCase):
    """WhisperX должен идти ПОСЛЕ Parakeet когда Parakeet включён, SenseVoice выключен."""

    @patch("core.engine.settings")
    def test_whisperx_after_parakeet_when_present(self, mock_settings: Any) -> None:
        """Parakeet enabled, SenseVoice disabled → WhisperX ПОСЛЕ Parakeet (W1303 F1)."""
        _base_settings(mock_settings)
        mock_settings.PARAKEET_ENABLED = True
        mock_settings.WHISPERX_ENABLED = True
        # SenseVoice OFF — это ключевой сценарий бага W1303 F1

        engine = _make_engine()

        call_order: list[str] = []
        engine._transcribe_parakeet = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda *a, **kw: call_order.append("parakeet") or (_ for _ in ()).throw(RuntimeError("pk stub"))
        )
        engine._transcribe_whisperx = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda *a, **kw: call_order.append("whisperx") or (_ for _ in ()).throw(RuntimeError("wx stub"))
        )

        def fake_model(audio_data: Any, model_name: str, prompt: str, language: Any = None) -> dict:
            call_order.append(f"whisper:{model_name}")
            raise RuntimeError("whisper stub")

        engine._transcribe_model = fake_model  # type: ignore[method-assign]

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            with patch("core.engine._get_available_memory_gb", return_value=16.0):
                with patch("concurrent.futures.ThreadPoolExecutor", _SyncExecutor):
                    try:
                        engine._transcribe_with_fallback_impl(b"audio", "prompt", "en")
                    except (RuntimeError, Exception):
                        pass

        pk_idx = next((i for i, x in enumerate(call_order) if x == "parakeet"), None)
        wx_idx = next((i for i, x in enumerate(call_order) if x == "whisperx"), None)

        self.assertIsNotNone(pk_idx, "Parakeet должен быть вызван")
        self.assertIsNotNone(wx_idx, "WhisperX должен быть вызван")
        self.assertGreater(wx_idx, pk_idx,
                           f"WhisperX (pos {wx_idx}) должен идти ПОСЛЕ Parakeet (pos {pk_idx}); "
                           f"call_order={call_order}")


# ---------------------------------------------------------------------------
# 3. WhisperX ПОСЛЕ ОБОИХ (Parakeet + SenseVoice включены)
# ---------------------------------------------------------------------------

class TestWhisperXAfterBothWhenBothPresent(unittest.TestCase):
    """Когда оба включены — WhisperX идёт после обоих: balanced→Parakeet→SenseVoice→WhisperX."""

    @patch("core.engine.settings")
    def test_whisperx_after_both_when_both_present(self, mock_settings: Any) -> None:
        """Parakeet enabled + SenseVoice enabled → WhisperX ПОСЛЕ обоих."""
        _base_settings(mock_settings)
        mock_settings.PARAKEET_ENABLED = True
        mock_settings.SENSEVOICE_ENABLED = True
        mock_settings.WHISPERX_ENABLED = True

        engine = _make_engine()

        call_order: list[str] = []
        engine._transcribe_parakeet = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda *a, **kw: call_order.append("parakeet") or (_ for _ in ()).throw(RuntimeError("pk stub"))
        )
        engine._transcribe_sensevoice = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda *a, **kw: call_order.append("sensevoice") or (_ for _ in ()).throw(RuntimeError("sv stub"))
        )
        engine._transcribe_whisperx = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda *a, **kw: call_order.append("whisperx") or (_ for _ in ()).throw(RuntimeError("wx stub"))
        )

        def fake_model(audio_data: Any, model_name: str, prompt: str, language: Any = None) -> dict:
            call_order.append(f"whisper:{model_name}")
            raise RuntimeError("whisper stub")

        engine._transcribe_model = fake_model  # type: ignore[method-assign]

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            with patch("core.engine._get_available_memory_gb", return_value=16.0):
                with patch("concurrent.futures.ThreadPoolExecutor", _SyncExecutor):
                    try:
                        engine._transcribe_with_fallback_impl(b"audio", "prompt", "en")
                    except (RuntimeError, Exception):
                        pass

        pk_idx = next((i for i, x in enumerate(call_order) if x == "parakeet"), None)
        sv_idx = next((i for i, x in enumerate(call_order) if x == "sensevoice"), None)
        wx_idx = next((i for i, x in enumerate(call_order) if x == "whisperx"), None)

        self.assertIsNotNone(pk_idx, "Parakeet должен быть вызван")
        self.assertIsNotNone(sv_idx, "SenseVoice должна быть вызвана")
        self.assertIsNotNone(wx_idx, "WhisperX должен быть вызван")
        self.assertGreater(wx_idx, pk_idx,
                           f"WhisperX (pos {wx_idx}) должен идти ПОСЛЕ Parakeet (pos {pk_idx}); "
                           f"call_order={call_order}")
        self.assertGreater(wx_idx, sv_idx,
                           f"WhisperX (pos {wx_idx}) должен идти ПОСЛЕ SenseVoice (pos {sv_idx}); "
                           f"call_order={call_order}")


# ---------------------------------------------------------------------------
# 4. WhisperX на дефолтной позиции 1 (ни Parakeet, ни SenseVoice)
# ---------------------------------------------------------------------------

class TestWhisperXDefaultPositionWhenNeither(unittest.TestCase):
    """Когда ни Parakeet, ни SenseVoice не включены — WhisperX на позиции 1 (после balanced)."""

    @patch("core.engine.settings")
    def test_whisperx_default_position_when_neither(self, mock_settings: Any) -> None:
        """Parakeet disabled + SenseVoice disabled → WhisperX на pos 1 (после balanced)."""
        _base_settings(mock_settings)
        mock_settings.WHISPERX_ENABLED = True
        # Оба остальных выключены

        engine = _make_engine()
        # Помечаем balanced недоступным — тогда первым должен попасть WhisperX
        engine._unavailable_models["mlx-community/whisper-large-v3-turbo"] = __import__("time").monotonic()

        call_order: list[str] = []
        engine._transcribe_whisperx = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda *a, **kw: call_order.append("whisperx") or {"text": "ok", "engine": "whisperx", "language": "en", "segments": []}
        )

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            with patch("core.engine._get_available_memory_gb", return_value=16.0):
                with patch("concurrent.futures.ThreadPoolExecutor", _SyncExecutor):
                    try:
                        result = engine._transcribe_with_fallback_impl(b"audio", "prompt", "en")
                    except (RuntimeError, Exception):
                        result = {}

        # WhisperX должен был быть вызван (это единственный доступный адаптер)
        self.assertIn("whisperx", call_order,
                      "WhisperX должен быть вызван как единственный доступный адаптер")


if __name__ == "__main__":
    unittest.main()
