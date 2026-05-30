"""Tests for W1677 F1 HIGH fix: BackendService wires event_bus._event_replay.

Covers:
- test_event_replay_wired_in_backend_service   — _event_replay injected into event_bus after __init__
- test_emit_records_to_event_replay_when_wired — emit() calls record_event on the wired manager
- test_get_event_log_returns_emitted_events    — get_event_log IPC returns events that were emitted
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.event_bus import bus as _module_bus  # noqa: E402
from backend.event_replay import EventReplayManager  # noqa: E402
from backend.service import BackendService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


def _make_backend_service(tmp_path: Path) -> BackendService:
    """Constructs a BackendService with minimal stubbed collaborators."""
    store = StateStore(tmp_path / "data")

    fake_recorder = MagicMock()
    fake_recorder.is_recording = False
    fake_recorder.sample_rate = 16000

    fake_transcriber = MagicMock()
    fake_transcriber.transcribe.return_value = ("", 0.0, [])
    fake_transcriber.engine = MagicMock()
    fake_transcriber.engine.rewriter = MagicMock()
    fake_transcriber.engine.rewriter.is_enabled = False

    fake_translator = MagicMock()

    return BackendService(
        store=store,
        recorder=fake_recorder,
        transcriber=fake_transcriber,
        translator=fake_translator,
    )


class TestEventReplayWiredInBackendService(unittest.TestCase):
    """BackendService.__init__ must wire self._event_replay into event_bus._event_replay."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        # Restore module-level bus._event_replay to None to avoid cross-test pollution.
        _module_bus._event_replay = None
        self._tmp.cleanup()

    def test_event_replay_wired_in_backend_service(self) -> None:
        """After BackendService.__init__, event_bus._event_replay must be an EventReplayManager."""
        svc = _make_backend_service(self._tmp_path)
        self.assertIsNotNone(
            _module_bus._event_replay,
            "event_bus._event_replay must not be None after BackendService.__init__",
        )
        self.assertIsInstance(
            _module_bus._event_replay,
            EventReplayManager,
            "event_bus._event_replay must be an EventReplayManager instance",
        )
        # It must be the same object as svc._event_replay
        self.assertIs(
            _module_bus._event_replay,
            svc._event_replay,
            "event_bus._event_replay must be the same object as svc._event_replay",
        )


class TestEmitRecordsToEventReplayWhenWired(unittest.TestCase):
    """emit() must forward events to the wired EventReplayManager."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        _module_bus._event_replay = None
        self._tmp.cleanup()

    def test_emit_records_to_event_replay_when_wired(self) -> None:
        """After wiring, a bus.emit() call must invoke record_event on the manager."""
        mock_replay = MagicMock()
        _module_bus._event_replay = mock_replay

        _module_bus.emit("stt.final", {"text": "hello world"})

        mock_replay.record_event.assert_called_once()
        call_args = mock_replay.record_event.call_args
        # Positional: event_type, payload
        self.assertEqual(call_args[0][0], "stt.final")
        self.assertEqual(call_args[0][1], {"text": "hello world"})
        # W1673 F4 LOW: ts keyword arg must be present and non-empty
        ts_val = call_args[1].get("ts")
        self.assertIsNotNone(ts_val, "record_event must receive ts= kwarg from emit()")
        self.assertIsInstance(ts_val, str)
        self.assertTrue(ts_val.startswith("20"), f"ts should be ISO 8601 UTC, got: {ts_val!r}")


class TestGetEventLogReturnsEmittedEvents(unittest.TestCase):
    """get_event_log IPC must return events that were emitted via event_bus after wiring."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        _module_bus._event_replay = None
        self._tmp.cleanup()

    def test_get_event_log_returns_emitted_events(self) -> None:
        """Events emitted after BackendService.__init__ appear in get_event_log response."""
        svc = _make_backend_service(self._tmp_path)

        # Emit an event through the module-level bus (same object wired into svc)
        _module_bus.emit("test.probe", {"marker": "W1677"})

        result = svc._event_replay.get_events(event_type="test.probe")
        self.assertGreater(
            len(result),
            0,
            "get_events() must return at least one entry after emit() when _event_replay is wired",
        )
        self.assertEqual(result[0]["type"], "test.probe")
        self.assertEqual(result[0]["data"]["marker"], "W1677")


if __name__ == "__main__":
    unittest.main()
