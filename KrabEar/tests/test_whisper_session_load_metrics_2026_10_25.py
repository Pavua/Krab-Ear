"""Метрики резидентности Whisper-воркера: cold_start_count, last_load_sec, resident_mb.

Нужны для 3-суточной канарейки enforce_whisper: без них нельзя увидеть цену
выгрузки (сколько раз воркер поднимался заново и сколько стоил первый запрос).
"""
from unittest.mock import MagicMock, patch

from core import mlx_whisper_session as mws


def _session():
    return mws.MLXWhisperSession()


def _fake_proc(pid=4242):
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = None
    return proc


def test_fresh_session_reports_zero_cold_starts():
    snap = _session().residency_snapshot()
    assert snap["cold_start_count"] == 0
    assert snap["last_load_sec"] is None
    assert snap["resident_mb"] is None


def test_each_spawn_counts_as_cold_start():
    s = _session()
    with patch.object(mws.subprocess, "Popen", return_value=_fake_proc()), \
            patch.object(s, "_start_stderr_drain"):
        s._spawn_unlocked()
        s._proc = None  # имитация выгрузки
        s._spawn_unlocked()
    assert s.residency_snapshot()["cold_start_count"] == 2


def test_first_successful_transcribe_after_spawn_records_load_sec_once():
    s = _session()
    with patch.object(mws.subprocess, "Popen", return_value=_fake_proc()), \
            patch.object(s, "_start_stderr_drain"):
        s._spawn_unlocked()
    ok = {"ok": True, "result": {"text": "x"}}
    with patch.object(s, "start"), patch.object(s, "_send", return_value=ok):
        s.transcribe("a.wav", {}, 5.0, "m")
        first = s.residency_snapshot()["last_load_sec"]
        assert first is not None and first >= 0.0
        s.transcribe("b.wav", {}, 5.0, "m")
    # второй запрос на тёплой модели не перезаписывает цену холодного старта
    assert s.residency_snapshot()["last_load_sec"] == first


def test_failed_first_transcribe_does_not_record_load_sec():
    s = _session()
    with patch.object(mws.subprocess, "Popen", return_value=_fake_proc()), \
            patch.object(s, "_start_stderr_drain"):
        s._spawn_unlocked()
    with patch.object(s, "start"), \
            patch.object(s, "_send", return_value={"ok": False, "error": "boom"}):
        try:
            s.transcribe("a.wav", {}, 5.0, "m")
        except RuntimeError:
            pass
    assert s.residency_snapshot()["last_load_sec"] is None


def test_resident_mb_reads_child_rss_without_raising():
    s = _session()
    s._proc = _fake_proc(pid=777)
    with patch.object(mws.subprocess, "check_output", return_value=" 1572864\n") as co:
        assert s.residency_snapshot()["resident_mb"] == 1536
        assert co.call_args[0][0][-1] == "777"


def test_resident_mb_none_when_psutil_fails():
    s = _session()
    s._proc = _fake_proc(pid=777)
    with patch.object(mws.subprocess, "check_output", side_effect=OSError("gone")):
        assert s.residency_snapshot()["resident_mb"] is None
