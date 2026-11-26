"""G3-privacy-04 P04-09: recovery не доверяет маркеру COMMITTED на слово — но и не блокируется.

Раньше ``_recover_pending_restore`` видел ``state == COMMITTED`` и без проверки
возвращал ``ok: True`` с reason=None. Теперь живые журналы сверяются с size/sha256
маркера. Расхождение НЕ блокирует (recovery зовётся из точек обслуживания, а не до
записи: легитимные append диктовки после COMMITTED меняют журналы, и блокировка
стала бы вечным отказом бэкапам без пути выхода): транзакция завершена, staging
убирается, но ответ честно несёт ``snapshot_committed_unverified`` + warnings.
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.encrypted_snapshot import (
    REASON_COMMITTED_UNVERIFIED,
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
    def test_intact_live_journals_are_cleaned_and_reported_clean(self, tmp_path):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, "tx-intact")

        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert result["pending"] is False
        assert result["reason"] is None
        assert result["warnings"] == []
        assert not leftover.exists()

    def test_append_after_commit_does_not_block_recovery_forever(self, tmp_path):
        """Легитимная диктовка после COMMITTED: журнал изменился, но блока нет."""
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        journal = data_dir / HISTORY_JOURNAL_FILENAMES[0]
        journal.write_bytes(journal.read_bytes() + b"new dictation after commit\n")
        appended = journal.read_bytes()
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, "tx-append")

        first = _recover(data_dir, crypto)

        assert first["ok"] is True
        assert first["pending"] is False
        assert first["reason"] == REASON_COMMITTED_UNVERIFIED
        assert any(w.startswith("committed_marker_mismatch:") for w in first["warnings"])
        assert not leftover.exists(), "маркер обязан убраться, иначе блок навсегда"
        assert _restore_markers(data_dir) == []
        assert journal.read_bytes() == appended, "recovery не имеет права править живое"
        # Второй проход — чистый: самовосстановление без ручных действий.
        second = _recover(data_dir, crypto)
        assert second["ok"] is True
        assert second["pending"] is False

    def test_missing_live_journal_is_reported_but_not_blocking(self, tmp_path):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        (data_dir / HISTORY_JOURNAL_FILENAMES[1]).unlink()
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, "tx-missing")

        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert result["pending"] is False
        assert result["reason"] == REASON_COMMITTED_UNVERIFIED
        assert any("отсутствует" in w for w in result["warnings"])
        assert not leftover.exists()

    def test_marker_without_file_evidence_degrades_with_reason(self, tmp_path):
        """Старые маркеры (до этого PR) могли не нести пригодный files."""
        data_dir, crypto, snapshot_dir, _files = _restored_profile(tmp_path)
        leftover = _plant_committed_marker(data_dir, snapshot_dir, [], "tx-empty")

        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert result["pending"] is False
        assert result["reason"] == REASON_COMMITTED_UNVERIFIED
        assert "committed_marker_without_valid_evidence" in result["warnings"]
        assert not leftover.exists()

    def test_marker_with_forged_file_name_is_never_read_from_disk(self, tmp_path):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("do-not-touch", encoding="utf-8")
        forged = list(files) + [{"name": "../outside.txt", "size": 1, "sha256": "0" * 64}]
        leftover = _plant_committed_marker(data_dir, snapshot_dir, forged, "tx-forged")

        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert result["reason"] == REASON_COMMITTED_UNVERIFIED
        assert "committed_marker_without_valid_evidence" in result["warnings"]
        assert not leftover.exists()
        assert outside.read_text(encoding="utf-8") == "do-not-touch"
