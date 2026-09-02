"""Tests for AudioRecorder device parameter pass-through (W1327 F2 HIGH / W1332).

Covers:
- test_input_stream_receives_device_param
- test_default_device_when_none_passed
- test_set_device_validates_existence
"""
from __future__ import annotations

import sys
import os
import threading
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRAB_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_ROOT not in sys.path:
    sys.path.insert(0, KRAB_ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_sd():
    """Return a minimal sounddevice mock that behaves enough for AudioRecorder."""
    fake_sd = MagicMock()
    # InputStream context manager
    stream_instance = MagicMock()
    stream_instance.read.return_value = (
        __import__("numpy").zeros((1600, 1), dtype="float32"),
        False,
    )
    fake_stream_cm = MagicMock()
    fake_stream_cm.__enter__ = MagicMock(return_value=stream_instance)
    fake_stream_cm.__exit__ = MagicMock(return_value=False)
    fake_sd.InputStream.return_value = fake_stream_cm
    # query_devices: accepts (device, kind) — return value not important for validation
    fake_sd.query_devices.return_value = {}
    return fake_sd, stream_instance


class TestAudioRecorderDeviceParam(unittest.TestCase):
    """AudioRecorder honours the device parameter end-to-end."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _import_recorder(self, fake_sd):
        """Import AudioRecorder with sounddevice stubbed out."""
        import importlib
        import backend.recorder as _rec_module
        original_sd = _rec_module.sd
        _rec_module.sd = fake_sd
        try:
            from backend.recorder import AudioRecorder
            return AudioRecorder, _rec_module, original_sd
        except Exception:
            _rec_module.sd = original_sd
            raise

    # ------------------------------------------------------------------
    # Test: device kwarg forwarded to sd.InputStream
    # ------------------------------------------------------------------

    def test_input_stream_receives_device_param(self):
        """sd.InputStream must be called with device= when recorder has one set."""
        fake_sd, _stream = _make_fake_sd()

        import importlib
        import backend.recorder as _rec_module
        orig = _rec_module.sd
        _rec_module.sd = fake_sd
        try:
            from backend.recorder import AudioRecorder

            recorder = AudioRecorder(device=2)
            stop_event = threading.Event()

            # Patch stop_event so _worker exits after one iteration
            recorder._stop_event = stop_event
            stop_event.set()  # pre-set so _worker loop exits immediately

            recorder._worker()

            # Verify InputStream was called with device=2
            fake_sd.InputStream.assert_called_once()
            _, kwargs = fake_sd.InputStream.call_args
            self.assertEqual(kwargs.get("device"), 2,
                             "sd.InputStream should receive device=2")
        finally:
            _rec_module.sd = orig

    # ------------------------------------------------------------------
    # Test: no device= kwarg when device is None (default)
    # ------------------------------------------------------------------

    def test_default_device_when_none_passed(self):
        """When device is None, sd.InputStream must NOT receive a device= kwarg."""
        fake_sd, _stream = _make_fake_sd()

        import backend.recorder as _rec_module
        orig = _rec_module.sd
        _rec_module.sd = fake_sd
        try:
            from backend.recorder import AudioRecorder

            recorder = AudioRecorder()  # no device arg
            self.assertIsNone(recorder._device)

            stop_event = threading.Event()
            recorder._stop_event = stop_event
            stop_event.set()

            recorder._worker()

            fake_sd.InputStream.assert_called_once()
            _, kwargs = fake_sd.InputStream.call_args
            self.assertNotIn("device", kwargs,
                             "sd.InputStream should NOT have device= when default")
        finally:
            _rec_module.sd = orig

    # ------------------------------------------------------------------
    # Test: set_device validates existence via sd.query_devices
    # ------------------------------------------------------------------

    def test_set_device_validates_existence(self):
        """set_device raises ValueError for non-existent device."""
        fake_sd, _ = _make_fake_sd()
        # Make query_devices raise for the bad device
        fake_sd.query_devices.side_effect = Exception("Device not found")

        import backend.recorder as _rec_module
        orig = _rec_module.sd
        _rec_module.sd = fake_sd
        try:
            from backend.recorder import AudioRecorder

            recorder = AudioRecorder()
            with self.assertRaises(ValueError):
                recorder.set_device(999)
        finally:
            _rec_module.sd = orig

    def test_set_device_accepts_valid_device(self):
        """set_device succeeds and updates _device for a valid device id."""
        fake_sd, _ = _make_fake_sd()
        # query_devices returns normally (no exception)
        fake_sd.query_devices.return_value = {"name": "Mock Mic", "max_input_channels": 1}

        import backend.recorder as _rec_module
        orig = _rec_module.sd
        _rec_module.sd = fake_sd
        try:
            from backend.recorder import AudioRecorder

            recorder = AudioRecorder()
            self.assertIsNone(recorder._device)
            recorder.set_device(1)
            self.assertEqual(recorder._device, 1)
        finally:
            _rec_module.sd = orig

    def test_set_device_accepts_none(self):
        """set_device(None) resets to system default without calling query_devices."""
        fake_sd, _ = _make_fake_sd()

        import backend.recorder as _rec_module
        orig = _rec_module.sd
        _rec_module.sd = fake_sd
        try:
            from backend.recorder import AudioRecorder

            recorder = AudioRecorder(device=3)
            self.assertEqual(recorder._device, 3)
            recorder.set_device(None)
            self.assertIsNone(recorder._device)
            # query_devices should NOT be called when resetting to None
            fake_sd.query_devices.assert_not_called()
        finally:
            _rec_module.sd = orig

    def test_device_string_name_forwarded(self):
        """sd.InputStream should accept string device names too."""
        fake_sd, _stream = _make_fake_sd()

        import backend.recorder as _rec_module
        orig = _rec_module.sd
        _rec_module.sd = fake_sd
        try:
            from backend.recorder import AudioRecorder

            recorder = AudioRecorder(device="Built-in Microphone")
            stop_event = threading.Event()
            recorder._stop_event = stop_event
            stop_event.set()

            recorder._worker()

            _, kwargs = fake_sd.InputStream.call_args
            self.assertEqual(kwargs.get("device"), "Built-in Microphone")
        finally:
            _rec_module.sd = orig


class TestRecordingCoreServiceDeviceInjection(unittest.TestCase):
    """RecordingCoreService.handle_start_recording injects device from settings."""

    def _make_minimal_service(self, selected_device=None):
        """Build a minimal RecordingCoreService with mocked collaborators."""
        import backend.recording_core_service as rcs_mod

        fake_recorder = MagicMock()
        fake_recorder.is_recording = False
        fake_recorder.start.return_value = True
        fake_recorder.sample_rate = 16000

        fake_settings_svc = MagicMock()
        fake_settings_svc.cached_settings.return_value = {
            "selected_input_device": selected_device,
            "realtime_preview_enabled": False,
            "realtime_partial_enabled": False,
            "quality_profile": "balanced",
        }

        fake_transcriber = MagicMock()

        svc = rcs_mod.RecordingCoreService.__new__(rcs_mod.RecordingCoreService)
        svc.recorder = fake_recorder
        svc._settings_svc = fake_settings_svc
        svc.transcriber = fake_transcriber
        svc._preview_lock = __import__("threading").Lock()
        svc._rt_lock = __import__("threading").Lock()  # wave-27: added to __init__, required by handle_start_recording
        svc._preview_text = ""
        svc._preview_duration_sec = 0.0
        svc._preview_worker_thread = None
        svc._rt_partial = None
        svc._rt_session_id = None
        svc._session_tracker = MagicMock()
        return svc, fake_recorder

    def test_set_device_called_with_setting(self):
        """handle_start_recording calls recorder.set_device with selected_input_device."""
        svc, recorder = self._make_minimal_service(selected_device=3)
        result = svc.handle_start_recording({})
        recorder.set_device.assert_called_once_with(3)
        self.assertEqual(result.get("status"), "recording")

    def test_no_set_device_when_setting_is_none(self):
        """handle_start_recording does NOT call set_device when setting is None."""
        svc, recorder = self._make_minimal_service(selected_device=None)
        svc.handle_start_recording({})
        recorder.set_device.assert_not_called()

    def test_no_set_device_when_setting_is_empty_string(self):
        """Пустая строка — «системное по умолчанию», а не имя устройства.

        Пикер микрофона в панели кодирует первый пункт списка («По умолчанию»)
        пустой строкой, и таким же стал дефолт `selected_input_device` в
        DEFAULT_SETTINGS (02.09.2026, когда пикер наконец подключили к
        настройке). Проверка `is not None` пропустила бы "" дальше, и
        `set_device("")` упал бы в предупреждение на каждой записи с выбором
        «по умолчанию» — тихо, но на каждой.
        """
        svc, recorder = self._make_minimal_service(selected_device="")
        svc.handle_start_recording({})
        recorder.set_device.assert_not_called()

    def test_set_device_error_is_logged_not_raised(self):
        """A bad device in settings logs a warning but does not abort recording."""
        svc, recorder = self._make_minimal_service(selected_device=999)
        recorder.set_device.side_effect = ValueError("Device 999 not found")
        # Should not raise; recording should still start
        result = svc.handle_start_recording({})
        self.assertEqual(result.get("status"), "recording")
        recorder.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
