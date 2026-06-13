from unittest import mock
from backend.state_store import StateStore
from backend.config_presets_library import ConfigPresetsLibrary
from backend.recording_scheduler import RecordingScheduler
from backend.transcription_queue import TranscriptionQueue
from core.hallucination_manager import HallucinationManager
from core.abbreviation_expander import AbbreviationExpander


@mock.patch("core.atomic_io.atomic_write_text")
def test_class1_modules_atomic(mock_atomic, tmp_path):
    # 1. state_store
    store = StateStore(data_dir=tmp_path)
    store.save_vocabulary(["word1", "word2"])
    mock_atomic.assert_called()
    mock_atomic.reset_mock()

    # 2. config_presets_library
    lib = ConfigPresetsLibrary(data_dir=tmp_path)
    lib._custom = {"my_preset": {"setting": 1}}
    lib._save()
    mock_atomic.assert_called()
    mock_atomic.reset_mock()

    # 3. recording_scheduler
    sched = RecordingScheduler(data_dir=tmp_path)
    sched._schedules = {"123": {"id": "123"}}
    sched._save()
    mock_atomic.assert_called()
    mock_atomic.reset_mock()

    # 4. transcription_queue
    queue = TranscriptionQueue(persist_path=tmp_path / "queue.ndjson")
    queue._save()
    mock_atomic.assert_called()
    mock_atomic.reset_mock()

    hm = HallucinationManager(data_dir=tmp_path)
    hm._custom = [{"pattern": "abc"}]
    hm._save_custom()
    mock_atomic.assert_called()
    mock_atomic.reset_mock()

    # 6. abbreviation_expander
    ae = AbbreviationExpander(data_dir=tmp_path)
    ae._abbrevs = {"ru": {"тк": {"expansion": "так как", "builtin": False}}}
    ae._save_custom()
    mock_atomic.assert_called()
