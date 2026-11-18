"""Регрессии миграции, ремонта истории и владения lock-файлом GigaAM."""

import ast
import fcntl
import os
import stat
import sys
from pathlib import Path

import pytest

from backend.data_migrator import DataMigrator
from backend.integrity_checker import IntegrityChecker


@pytest.mark.parametrize("confirm", [None, False, "false", 1])
def test_rollback_requires_boolean_confirmation(tmp_path, confirm):
    migrator = DataMigrator(data_dir=tmp_path)
    assert migrator.handle_rollback_migration({"confirm": confirm})["ok"] is False


def test_repair_creates_private_history_and_artifacts(tmp_path):
    history = tmp_path / "history.ndjson"
    history.write_bytes(b'{"id":"one","text":"sample"}\ninvalid record\n')
    history.chmod(0o600)

    result = IntegrityChecker().handle_repair_data({"data_dir": str(tmp_path)})

    backups = list(map(Path, result["backup_paths"]))
    quarantines = list(map(Path, result["quarantine_paths"]))
    assert len(backups) == len(quarantines) == 1
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in [history, *backups, *quarantines])


def test_migration_preserves_private_history_mode(tmp_path):
    history = tmp_path / "history.ndjson"
    history.write_text('{"id":"one","text":"sample"}\n', encoding="utf-8")
    history.chmod(0o600)

    result = DataMigrator().migrate(tmp_path)

    assert result.items_migrated == 1
    assert stat.S_IMODE(history.stat().st_mode) == 0o600


def test_migration_preserves_unparsed_records(tmp_path):
    history = tmp_path / "history.ndjson"
    history.write_bytes(b'{"id":"one","text":"sample"}\ninvalid record\r\n')

    result = DataMigrator().migrate(tmp_path)

    assert result.items_migrated == 1
    assert history.read_bytes().endswith(b"invalid record\r\n")


@pytest.fixture
def singleton_lock(monkeypatch, tmp_path):
    # Вызываем настоящий guard без импорта модели и обращений к lock рабочего процесса.
    import tempfile

    module_path = Path(__file__).resolve().parents[1] / "core/workers/gigaam_worker.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    guard = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_acquire_singleton_lock"
    )
    namespace = {"os": os, "sys": sys}
    exec(compile(ast.Module(body=[guard], type_ignores=[]), str(module_path), "exec"), namespace)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("KRAB_EAR_GIGAAM_WORKER_NO_SINGLETON", raising=False)
    yield namespace[guard.name], tmp_path / "krab_ear_gigaam_worker.lock"
    if handle := namespace.get("_singleton_lock_fd"):
        handle.close()


def test_gigaam_duplicate_preserves_owner_pid(singleton_lock):
    acquire, path = singleton_lock
    with path.open("w+", encoding="ascii") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owner.write("12345")
        owner.flush()

        with pytest.raises(SystemExit) as exit_info:
            acquire()

        assert exit_info.value.code == 0
        assert path.read_text(encoding="ascii") == "12345"


def test_gigaam_creates_private_pid_file(singleton_lock):
    acquire, path = singleton_lock
    acquire()

    assert path.read_text(encoding="ascii") == str(os.getpid())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_custom_anonymizer_applies_regex_validation():
    from core.text_anonymizer import TextAnonymizer

    anonymizer = TextAnonymizer()
    with pytest.raises(ValueError, match="альтернатив"):
        anonymizer.add_custom_rule("region", r"(?:north|south)+", "[REGION]")
    assert "region" not in anonymizer.list_rules()


def test_wake_word_declines_start_when_privacy_read_fails(tmp_path):
    from backend.openwakeword_adapter import OpenWakeWordAdapter

    def unavailable(key, default):
        raise OSError("settings temporarily unavailable")

    adapter = OpenWakeWordAdapter(data_dir=tmp_path, settings_get=unavailable)
    assert adapter.handle_wake_word_start({})["ok"] is False


def test_wake_word_listener_stops_when_privacy_read_fails(tmp_path):
    from backend.openwakeword_adapter import OpenWakeWordAdapter

    def unavailable(key, default):
        raise OSError("settings temporarily unavailable")

    adapter = OpenWakeWordAdapter(data_dir=tmp_path, settings_get=unavailable)
    assert adapter._privacy_blocked()


def test_migration_keeps_truncated_utf8_record(tmp_path):
    history = tmp_path / "history.ndjson"
    history.write_bytes(b'{"id":"one","text":"sample"}\n\xe2\x82\n')
    (tmp_path / "settings.json").write_text('{"history_encryption_enabled":false}', encoding="utf-8")

    result = DataMigrator().migrate(tmp_path)

    assert result.items_migrated == 1
    assert history.read_bytes().endswith(b"\xe2\x82\n")


def test_rollback_requires_completed_migration_snapshot(tmp_path):
    backup = tmp_path / "backups" / "migration_backup_incomplete"
    backup.mkdir(parents=True)

    with pytest.raises(ValueError, match="резервн"):
        DataMigrator(data_dir=tmp_path).handle_rollback_migration({"confirm": True, "backup_path": str(backup)})


def test_migration_backups_created_in_same_second_are_distinct(tmp_path, monkeypatch):
    from datetime import datetime

    class FixedClock:
        @staticmethod
        def now():
            return datetime(2026, 11, 18, 6, 0)

    monkeypatch.setattr("backend.data_migrator.datetime", FixedClock)
    history = tmp_path / "history.ndjson"
    history.write_text('{"id":"one","text":"initial"}\n', encoding="utf-8")
    migrator = DataMigrator()
    first = Path(migrator._create_backup(tmp_path))
    history.write_text('{"id":"one","text":"updated"}\n', encoding="utf-8")
    second = Path(migrator._create_backup(tmp_path))

    assert first != second
    assert "initial" in (first / history.name).read_text(encoding="utf-8")


def test_wake_word_never_reads_audio_when_privacy_provider_fails(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from backend.openwakeword_adapter import OpenWakeWordAdapter

    opened = []

    def unavailable(key, default):
        raise OSError("settings temporarily unavailable")

    def input_stream(**kwargs):
        opened.append(kwargs)
        raise RuntimeError("no real audio device in test")

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=input_stream))
    adapter = OpenWakeWordAdapter(data_dir=tmp_path, settings_get=unavailable)
    adapter._listen_loop(threshold=0.5, chunk_size=1280, sample_rate=16000, generation=adapter._generation)
    assert opened == []


@pytest.mark.parametrize("separator", [b"\n", b"\r\n", b"\r"])
def test_migration_reads_legacy_line_endings(tmp_path, separator):
    history = tmp_path / "history.ndjson"
    history.write_bytes(separator.join([b'{"id":"one","text":"sample"}', b"invalid record", b""]))

    result = DataMigrator().migrate(tmp_path)

    assert result.items_migrated == 1
    assert history.read_bytes().endswith(b"invalid record" + separator)


@pytest.mark.parametrize("separator", [b"\n", b"\r\n", b"\r"])
def test_encrypted_history_probe_accepts_legacy_line_endings(tmp_path, separator):
    from backend.history_crypto import SENTINEL
    from backend.state_store import has_encrypted_history_in

    history = tmp_path / "history.ndjson"
    original = separator.join(
        [b'{"id":"one","text":"sample"}', b"incomplete record \xe2\x82", SENTINEL.encode("ascii"), b""]
    )
    history.write_bytes(original)
    (tmp_path / "settings.json").write_text('{"history_encryption_enabled":false}', encoding="utf-8")

    assert has_encrypted_history_in([history], unreadable_is_encrypted=False)
    assert DataMigrator().migrate(tmp_path).reason == "history_encryption_operation_unavailable"
    assert history.read_bytes() == original
