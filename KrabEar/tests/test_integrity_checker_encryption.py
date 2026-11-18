"""Ремонт создаёт копии только при подтверждённой plaintext-политике."""

import fcntl
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.history_crypto import HistoryCrypto
from backend.integrity_checker import IntegrityChecker
from backend.plaintext_export_authorization import POLICY_REVISION_KEY

OFF_POLICY = {
    "history_encryption_enabled": False,
    "privacy_mode_enabled": False,
    POLICY_REVISION_KEY: "a" * 32,
}
HISTORY = b'{"id":"one","ts":"2026-11-18T00:00:00","text":"sample"}\ninvalid record\n'


def assert_refused(profile, original, fixed, skipped, details):
    assert fixed == 0
    assert skipped >= 1
    assert details
    assert (profile / "history.ndjson").read_bytes() == original
    assert not list(profile.glob("*.corrupt-backup-*"))
    assert not list(profile.glob("*.quarantine-*"))


@pytest.mark.parametrize("policy", [
    None,
    "{",
    "{}",
    json.dumps({**OFF_POLICY, "history_encryption_enabled": True}),
    json.dumps({**OFF_POLICY, "privacy_mode_enabled": True}),
    json.dumps({**OFF_POLICY, "history_encryption_enabled": 0}),
    json.dumps({**OFF_POLICY, POLICY_REVISION_KEY: ""}),
    json.dumps(OFF_POLICY).replace("{", '{"history_encryption_enabled":true,', 1),
], ids=["missing", "malformed", "missing-flags", "encrypted", "privacy", "non-bool", "revision", "duplicate"])
def test_refuse_before_plaintext_backup(tmp_path, policy):
    (tmp_path / "history.ndjson").write_bytes(HISTORY)
    if policy is not None:
        (tmp_path / "settings.json").write_text(policy, encoding="utf-8")

    result = IntegrityChecker().handle_repair_data({"data_dir": str(tmp_path)})

    assert_refused(tmp_path, HISTORY, result["fixed"], result["skipped"], result["details"])
    assert result["backup_paths"] == result["quarantine_paths"] == []


@pytest.mark.parametrize("ledger", ["history.ndjson", "history_tags.ndjson"])
def test_residual_ciphertext_blocks_known_off_repair(tmp_path, ledger):
    (tmp_path / "settings.json").write_text(json.dumps(OFF_POLICY), encoding="utf-8")
    history = tmp_path / "history.ndjson"
    history.write_bytes(HISTORY)
    token = HistoryCrypto(b"k" * 32).encrypt_line('{"text":"sample"}').encode("ascii")
    with (tmp_path / ledger).open("ab") as stream:
        stream.write(b"incomplete \xff\r \t" + token + b" \r")
    original = history.read_bytes()

    result = IntegrityChecker().handle_repair_data({"data_dir": str(tmp_path)})

    assert_refused(tmp_path, original, result["fixed"], result["skipped"], result["details"])


def test_recheck_policy_after_waiting_for_history_lock(tmp_path, monkeypatch):
    checker = IntegrityChecker()
    history = tmp_path / "history.ndjson"
    history.write_bytes(HISTORY)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(OFF_POLICY), encoding="utf-8")
    report = checker.check_integrity(tmp_path)
    flock = fcntl.flock
    waiting = threading.Event()

    def signal_wait(fd, operation):
        if operation & fcntl.LOCK_EX:
            waiting.set()
        return flock(fd, operation)

    with ThreadPoolExecutor(max_workers=1) as pool, (tmp_path / "history.lock").open("w") as writer:
        flock(writer, fcntl.LOCK_EX)
        monkeypatch.setattr(fcntl, "flock", signal_wait)
        future = pool.submit(checker.repair, tmp_path, report)
        try:
            assert waiting.wait(timeout=5)
            settings.write_text(json.dumps({**OFF_POLICY, "history_encryption_enabled": True}), encoding="utf-8")
        finally:
            flock(writer, fcntl.LOCK_UN)
        result = future.result(timeout=5)

    assert_refused(tmp_path, HISTORY, result.fixed, result.skipped, result.details)


@pytest.mark.parametrize("ledger", ["history.ndjson", "history_tags.ndjson"])
def test_repair_refuses_unverified_ledger(tmp_path, ledger):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "settings.json").write_text(json.dumps(OFF_POLICY), encoding="utf-8")
    history = profile / "history.ndjson"
    if ledger != history.name:
        history.write_bytes(HISTORY)
    source = tmp_path / "source"
    source.write_bytes(b"ENC1:sample\n")
    (profile / ledger).symlink_to(source)
    original = history.read_bytes()

    result = IntegrityChecker().handle_repair_data({"data_dir": str(profile)})

    assert_refused(profile, original, result["fixed"], result["skipped"], result["details"])
    assert (profile / ledger).is_symlink()
