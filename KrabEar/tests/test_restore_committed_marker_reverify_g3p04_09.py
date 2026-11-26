"""G3-privacy-04 P04-09: recovery не доверяет маркеру COMMITTED на слово.

Раньше ``_recover_pending_restore`` видел ``state == COMMITTED`` и без проверки
возвращал ``ok: True`` и убирал приватный staging. Но маркер — лишь запись о том,
что read-back когда-то прошёл: живые журналы после этого могли испортиться или
исчезнуть. Теперь перед «успехом» они заново сверяются с size/sha256 из маркера;
при расхождении recovery fail-closed, а маркер и staging остаются для разбора.
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.encrypted_snapshot import (
    REASON_MANIFEST_INVALID,
    REASON_READBACK_FAILED,
    RESTORE_MARKER_FILENAME,
    RESTORE_STAGING_PREFIX,
    restore_encrypted_snapshot,
)
from backend.state_store import HISTORY_JOURNAL_FILENAMES

# Общие помощники — те же synthetic tmp-профили и случайный ключ, что в b2.
from test_a52b2_snapshot_restore import (  # noqa: E402
    _crypto,
    _data_dir,
    _fill_profile,
    _make_snapshot,
    _policy_on,
    _recover,
    _restore_markers,
    _settings_on,
)


def _restored_profile(tmp_path: Path):
    """Профиль после УСПЕШНОГО restore: возвращает (data_dir, crypto, files_meta)."""
    data_dir = _data_dir(tmp_path)
    crypto = _crypto()
    _fill_profile(data_dir, crypto)
    _settings_on(data_dir)
    snapshot_dir = _make_snapshot(data_dir, crypto)
    result = restore_encrypted_snapshot(
        data_dir=data_dir,
        backups_root=data_dir / "backups",
        snapshot_dir=snapshot_dir,
        crypto=crypto,
        policy_read=_policy_on(data_dir),
    )
    assert result["ok"] is True, result
    return data_dir, crypto, snapshot_dir, result["files"]


def _plant_committed_marker(data_dir: Path, snapshot_dir: Path, files, txid: str) -> Path:
    """Маркер COMMITTED, оставшийся после crash между записью маркера и уборкой."""
    leftover = data_dir / f"{RESTORE_STAGING_PREFIX}{txid}"
    leftover.mkdir()
    (leftover / RESTORE_MARKER_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "transaction_id": txid,
                "state": "COMMITTED",
                "target_snapshot": str(snapshot_dir),
                "pre_restore_snapshot": str(snapshot_dir),
                "restored_entries": 3,
                "files": files,
            }
        ),
        encoding="utf-8",
    )
    return leftover


class TestCommittedMarkerIsReverified:
    def test_intact_live_journals_are_cleaned_and_reported_ok(self, tmp_path):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, "tx-intact")

        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert result["pending"] is False
        assert not leftover.exists()

    def test_tampered_live_journal_blocks_recovery_and_keeps_marker(self, tmp_path):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        victim = data_dir / HISTORY_JOURNAL_FILENAMES[0]
        victim.write_bytes(victim.read_bytes() + b"tampered-after-commit\n")
        tampered_bytes = victim.read_bytes()
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, "tx-tampered")

        result = _recover(data_dir, crypto)

        assert result["ok"] is False
        assert result["pending"] is True
        assert result["reason"] == REASON_READBACK_FAILED
        assert leftover.exists(), "маркер — единственное доказательство; терять нельзя"
        assert victim.read_bytes() == tampered_bytes, "recovery не имеет права править живое"
        assert _restore_markers(data_dir), "маркер должен остаться для разбора"

    def test_missing_live_journal_blocks_recovery(self, tmp_path):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        (data_dir / HISTORY_JOURNAL_FILENAMES[1]).unlink()
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, "tx-missing")

        result = _recover(data_dir, crypto)

        assert result["ok"] is False
        assert result["reason"] == REASON_READBACK_FAILED
        assert leftover.exists()

    def test_marker_without_file_evidence_is_not_trusted(self, tmp_path):
        data_dir, crypto, snapshot_dir, _files = _restored_profile(tmp_path)
        leftover = _plant_committed_marker(data_dir, snapshot_dir, [], "tx-empty")

        result = _recover(data_dir, crypto)

        assert result["ok"] is False
        assert result["pending"] is True
        assert result["reason"] == REASON_MANIFEST_INVALID
        assert leftover.exists()

    def test_marker_with_unknown_file_name_is_not_trusted(self, tmp_path):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        forged = list(files) + [{"name": "../../etc/passwd", "size": 1, "sha256": "0" * 64}]
        leftover = _plant_committed_marker(data_dir, snapshot_dir, forged, "tx-forged")

        result = _recover(data_dir, crypto)

        assert result["ok"] is False
        assert result["reason"] == REASON_MANIFEST_INVALID
        assert leftover.exists()
