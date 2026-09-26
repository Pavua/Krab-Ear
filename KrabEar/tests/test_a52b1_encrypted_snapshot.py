"""A5.2b1 — encrypted snapshot: полнота реестра, манифест, commit-протокол.

Спека `docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md` §5
(шаги 1–6) + карточка `docs/superpowers/plans/2026-09-26-a52b1-encrypted-snapshot.md`.

Только synthetic tmp-профили и СЛУЧАЙНЫЙ тестовый ключ (`os.urandom(32)`) —
system Keychain не трогается: тесты никогда не вызывают
`build_history_crypto()`, а production-путь запирается патчем, который
ЗАПРЕЩАЕТ обращение к Keychain.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.encrypted_snapshot import (
    SNAPSHOT_MANIFEST_FILENAME,
    SNAPSHOT_MANIFEST_VERSION,
    STATE_COMMITTED,
    STATE_COMMITTING,
    STATE_PREPARED,
    SnapshotOperationRefused,
    build_encrypted_snapshot,
    commit_encrypted_snapshot,
    create_encrypted_snapshot,
    recover_pending_state,
    verify_snapshot_readback,
)
from backend.history_crypto import SENTINEL, HistoryCrypto
from backend.state_store import HISTORY_JOURNAL_FILENAMES

# Строка-«canary»: её plaintext-хэш НЕ должен встречаться в манифесте.
CANARY_PLAINTEXT = '{"id":"canary-a52b1","text":"kolya skazal sekret"}'


def _key() -> bytes:
    return os.urandom(32)


def _crypto(key: bytes | None = None) -> HistoryCrypto:
    return HistoryCrypto(key or _key())


def _data_dir(tmp_path: Path) -> Path:
    base = tmp_path / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _settings(data_dir: Path, payload: dict) -> None:
    (data_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")


def _no_keychain(*_a, **_k):
    raise AssertionError("snapshot path must not touch the system Keychain")


def _line(index: int) -> str:
    return json.dumps({"id": f"e{index}", "text": f"запись-{index}"}, ensure_ascii=False)


def _fill_mixed(data_dir: Path, crypto: HistoryCrypto) -> dict[str, list[str]]:
    """Заполняет все 10 журналов: смесь plaintext и ENC1-строк.

    Возвращает ожидаемый plaintext на строку по каждому имени файла.
    """
    expected: dict[str, list[str]] = {}
    for i, name in enumerate(HISTORY_JOURNAL_FILENAMES):
        plain = [_line(i * 10 + j) for j in range(3)]
        lines: list[str] = []
        for j, text in enumerate(plain):
            if (i + j) % 2 == 0:
                lines.append(crypto.encrypt_line(text))
            else:
                lines.append(text)
        expected[name] = plain
        (data_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return expected


def _prepared(tmp_path: Path, data_dir: Path, crypto: HistoryCrypto, backup_dir: Path):
    return build_encrypted_snapshot(
        data_dir=data_dir,
        backup_dir=backup_dir,
        crypto=crypto,
        transaction_id="tx-a52b1-0001",
        policy_on=True,
    )


def _payload_files(snapshot_dir: Path) -> list[str]:
    return sorted(
        p.name for p in snapshot_dir.iterdir()
        if p.is_file() and p.name != SNAPSHOT_MANIFEST_FILENAME
    )


def _manifest(snapshot_dir: Path) -> dict:
    return json.loads((snapshot_dir / SNAPSHOT_MANIFEST_FILENAME).read_text("utf-8"))


class TestSnapshotRegistryCompleteness:
    def test_snapshot_contains_exactly_ten_registry_files(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = tmp_path / "snap"
        result = _prepared(tmp_path, data_dir, crypto, backup_dir)
        assert result["state"] == STATE_PREPARED

        staging = Path(result["staging_dir"])
        assert _payload_files(staging) == sorted(HISTORY_JOURNAL_FILENAMES)
        assert len(_payload_files(staging)) == 10
        # settings.json НЕ входит в payload (спека §1/§5).
        assert "settings.json" not in _payload_files(staging)

    def test_every_snapshot_line_decrypts_to_its_source_line(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        expected = _fill_mixed(data_dir, crypto)
        backup_dir = tmp_path / "snap"
        result = _prepared(tmp_path, data_dir, crypto, backup_dir)
        staging = Path(result["staging_dir"])

        for name in HISTORY_JOURNAL_FILENAMES:
            out_lines = (staging / name).read_text("utf-8").splitlines()
            assert out_lines, f"{name}: snapshot не должен быть пустым"
            for line in out_lines:
                assert line.startswith(SENTINEL), f"{name}: строка не ENC1"
                # Побайтовое соответствие исходной строке после расшифровки.
                assert crypto.decrypt_line(line) in expected[name]

    def test_manifest_records_only_fixed_names_size_and_ciphertext_hash(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = tmp_path / "snap"
        result = _prepared(tmp_path, data_dir, crypto, backup_dir)
        staging = Path(result["staging_dir"])
        manifest = _manifest(staging)

        assert manifest["version"] == SNAPSHOT_MANIFEST_VERSION
        assert manifest["transaction_id"] == "tx-a52b1-0001"
        assert manifest["state"] == STATE_PREPARED
        assert [f["name"] for f in manifest["files"]] == list(HISTORY_JOURNAL_FILENAMES)

        for entry in manifest["files"]:
            blob = (staging / entry["name"]).read_bytes()
            assert entry["size"] == len(blob)
            assert entry["sha256"] == hashlib.sha256(blob).hexdigest()

    def test_manifest_contains_no_plaintext_and_no_plaintext_hash(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        expected = _fill_mixed(data_dir, crypto)
        # Подставляем canary-строку в один из журналов.
        (data_dir / "history.ndjson").write_text(CANARY_PLAINTEXT + "\n", encoding="utf-8")
        expected["history.ndjson"] = [CANARY_PLAINTEXT]

        backup_dir = tmp_path / "snap"
        result = _prepared(tmp_path, data_dir, crypto, backup_dir)
        staging = Path(result["staging_dir"])
        raw_manifest = (staging / SNAPSHOT_MANIFEST_FILENAME).read_text("utf-8")

        canary_hash = hashlib.sha256(CANARY_PLAINTEXT.encode("utf-8")).hexdigest()
        assert canary_hash not in raw_manifest
        assert canary_hash[:32] not in raw_manifest
        for entry in manifest_files(staging):
            assert entry["sha256"] != canary_hash
        # Ни plaintext, ни ключ, ни хэш plaintext в манифесте.
        assert "kolya" not in raw_manifest
        assert "skazal" not in raw_manifest
        assert CANARY_PLAINTEXT not in raw_manifest
        assert crypto._aesgcm is not None
        # Все хэши манифеста — от ciphertext (они не совпадают с plaintext-хэшами).
        plaintext_hashes = {
            hashlib.sha256(t.encode("utf-8")).hexdigest()
            for lines in expected.values() for t in lines
        }
        for entry in manifest_files(staging):
            assert entry["sha256"] not in plaintext_hashes

    def test_no_plaintext_leaks_into_any_snapshot_file(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        (data_dir / "history_tags.ndjson").write_text(CANARY_PLAINTEXT + "\n", encoding="utf-8")
        backup_dir = tmp_path / "snap"
        result = _prepared(tmp_path, data_dir, crypto, backup_dir)
        staging = Path(result["staging_dir"])

        for path in staging.rglob("*"):
            if not path.is_file():
                continue
            blob = path.read_bytes()
            assert b"kolya" not in blob
            assert CANARY_PLAINTEXT.encode("utf-8") not in blob

    def test_empty_and_missing_journals_yield_valid_empty_entries(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        (data_dir / "history.ndjson").write_text(_line(1) + "\n", encoding="utf-8")
        (data_dir / "history_tags.ndjson").write_text("", encoding="utf-8")  # пустой
        # остальные 8 отсутствуют
        backup_dir = tmp_path / "snap"
        result = _prepared(tmp_path, data_dir, crypto, backup_dir)
        staging = Path(result["staging_dir"])

        assert _payload_files(staging) == sorted(HISTORY_JOURNAL_FILENAMES)
        manifest = _manifest(staging)
        by_name = {e["name"]: e for e in manifest["files"]}
        assert by_name["history_tags.ndjson"]["size"] == 0
        assert by_name["history_status.ndjson"]["size"] == 0
        for name in HISTORY_JOURNAL_FILENAMES:
            assert (staging / name).exists()

    def test_plaintext_lines_are_encrypted_and_enc1_lines_verified(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        plain = _line(7)
        encrypted = crypto.encrypt_line(_line(8))
        (data_dir / "history.ndjson").write_text(plain + "\n" + encrypted + "\n", encoding="utf-8")
        backup_dir = tmp_path / "snap"
        result = _prepared(tmp_path, data_dir, crypto, backup_dir)
        staging = Path(result["staging_dir"])

        out = (staging / "history.ndjson").read_text("utf-8").splitlines()
        assert len(out) == 2
        assert all(line.startswith(SENTINEL) for line in out)
        # plaintext зашифрован (не скопирован как есть)
        assert out[0] != plain
        assert crypto.decrypt_line(out[0]) == plain
        # уже ENC1 проверен расшифровкой и сохранён байт-в-байт
        assert out[1] == encrypted
        assert crypto.decrypt_line(out[1]) == _line(8)

    def test_tampered_enc1_line_is_refused_not_silently_skipped(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        good = crypto.encrypt_line(_line(3))
        tampered = good[:-6] + ("A" if good[-6] != "A" else "B") + good[-5:]
        (data_dir / "history.ndjson").write_text(
            good + "\n" + tampered + "\n" + _line(4) + "\n", encoding="utf-8"
        )
        backup_dir = tmp_path / "snap"

        with pytest.raises(SnapshotOperationRefused) as exc:
            _prepared(tmp_path, data_dir, crypto, backup_dir)
        assert exc.value.reason == "snapshot_line_tampered"
        # Никаких строк не потеряно молча: snapshot не опубликован.
        assert not (backup_dir / "history.ndjson").exists()

    def test_malformed_enc1_line_is_refused(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        (data_dir / "history.ndjson").write_text(
            SENTINEL + "not-base64!!\n", encoding="utf-8"
        )
        with pytest.raises(SnapshotOperationRefused) as exc:
            _prepared(tmp_path, data_dir, crypto, backup_dir := tmp_path / "snap")
        assert exc.value.reason == "snapshot_line_tampered"

    def test_symlinked_registry_entry_is_refused(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        outside = tmp_path / "outside.ndjson"
        outside.write_text(_line(99) + "\n", encoding="utf-8")
        target = data_dir / "history_tags.ndjson"
        target.unlink()
        target.symlink_to(outside)

        with pytest.raises(SnapshotOperationRefused) as exc:
            _prepared(tmp_path, data_dir, crypto, tmp_path / "snap")
        assert exc.value.reason == "snapshot_source_symlink"

    def test_missing_crypto_is_refused(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        _fill_mixed(data_dir, _crypto())
        with pytest.raises(SnapshotOperationRefused) as exc:
            build_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=tmp_path / "snap",
                crypto=None,
                transaction_id="tx-a52b1-0002",
                policy_on=True,
            )
        assert exc.value.reason == "snapshot_crypto_unavailable"

    def test_staging_directory_is_private(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        result = _prepared(tmp_path, data_dir, crypto, tmp_path / "snap")
        staging = Path(result["staging_dir"])
        assert staging.is_dir()
        assert oct(staging.stat().st_mode & 0o777) == "0o700"


def manifest_files(snapshot_dir: Path) -> list[dict]:
    return _manifest(snapshot_dir)["files"]


def _data_bytes(data_dir: Path) -> dict[str, bytes]:
    return {
        name: (data_dir / name).read_bytes()
        for name in HISTORY_JOURNAL_FILENAMES
        if (data_dir / name).is_file()
    }


def _staging_dirs(backups_root: Path) -> list[Path]:
    if not backups_root.is_dir():
        return []
    return [p for p in backups_root.iterdir() if p.is_dir() and p.name.startswith(".snapshot_staging_")]


def _no_plaintext_anywhere(*roots: Path) -> None:
    """Ни один байт plaintext-истории не должен лежать в backups-корне."""
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                blob = path.read_bytes()
                assert b"kolya" not in blob
                assert b"skazal" not in blob
                assert b"CANARY" not in blob


class TestCommitProtocol:
    """Спека §5 шаги 4–6: fingerprint, COMMITTING, read-back, fail-closed."""

    def test_commit_refuses_when_source_changed_between_prepare_and_commit(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = tmp_path / "backups" / "snapshot_1"
        prepared = _prepared(tmp_path, data_dir, crypto, backup_dir)

        # Источник изменился после подготовки (append в журнал).
        with (data_dir / "history.ndjson").open("a", encoding="utf-8") as fh:
            fh.write(_line(777) + "\n")
        sources_before = _data_bytes(data_dir)

        with pytest.raises(SnapshotOperationRefused) as exc:
            commit_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=backup_dir,
                transaction_id="tx-a52b1-0001",
                prepared=prepared,
            )
        assert exc.value.reason == "snapshot_fingerprint_mismatch"
        # Ничего не опубликовано, источники не тронуты, незавершённой транзакции нет.
        assert not backup_dir.exists()
        assert _data_bytes(data_dir) == sources_before
        assert _staging_dirs(backup_dir.parent) == []

    def test_crash_after_committing_before_publication_is_fail_closed(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        expected = _fill_mixed(data_dir, crypto)
        backup_dir = tmp_path / "backups" / "snapshot_1"
        sources_before = _data_bytes(data_dir)

        # Crash ровно на первой замене: COMMITTING уже durable.
        with patch(
            "backend.encrypted_snapshot._publish_staging",
            side_effect=OSError("synthetic crash on first replacement"),
        ), patch(
            "backend.history_crypto.build_history_crypto", side_effect=_no_keychain
        ):
            with pytest.raises(SnapshotOperationRefused) as exc:
                create_encrypted_snapshot(
                    data_dir=data_dir,
                    backup_dir=backup_dir,
                    crypto=crypto,
                    transaction_id="tx-a52b1-crash1",
                    policy_on=True,
                )
        assert exc.value.reason == "snapshot_readback_failed"
        assert exc.value.pending is True
        # Признак COMMITTING пережил crash — источник для b2-доказки.
        assert not backup_dir.exists()
        staging = _staging_dirs(backup_dir.parent)
        assert len(staging) == 1
        assert _manifest(staging[0])["state"] == STATE_COMMITTING
        assert _manifest(staging[0])["transaction_id"] == "tx-a52b1-crash1"
        # Исходные журналы целы, никакого отката в plaintext.
        assert _data_bytes(data_dir) == sources_before
        _no_plaintext_anywhere(backup_dir.parent)
        # Fail-closed признак, а не «успех».
        recovery = recover_pending_state(data_dir=data_dir, backup_dir=backup_dir.parent)
        assert recovery["ok"] is False
        assert recovery["pending"] is True
        assert recovery["reason"] == "snapshot_recovery_pending"
        assert recovery["transaction_id"] == "tx-a52b1-crash1"
        # Никакого нового ключа: build_history_crypto не вызывался (патч-ловушка).

    def test_crash_after_publication_keeps_valid_snapshot_and_reports_pending(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = tmp_path / "backups" / "snapshot_1"
        sources_before = _data_bytes(data_dir)

        # Crash сразу после публикации, до read-back/COMMITTED.
        with patch(
            "backend.encrypted_snapshot.verify_snapshot_readback",
            side_effect=OSError("synthetic crash after publication"),
        ):
            with pytest.raises(SnapshotOperationRefused) as exc:
                create_encrypted_snapshot(
                    data_dir=data_dir,
                    backup_dir=backup_dir,
                    crypto=crypto,
                    transaction_id="tx-a52b1-crash2",
                    policy_on=True,
                )
        assert exc.value.reason == "snapshot_readback_failed"
        assert exc.value.pending is True

        # Снимок на диске валиден и пригоден для докажки в b2, но НЕ COMMITTED.
        assert backup_dir.is_dir()
        assert _manifest(backup_dir)["state"] == STATE_COMMITTING
        assert _payload_files(backup_dir) == sorted(HISTORY_JOURNAL_FILENAMES)
        check = verify_snapshot_readback(backup_dir=backup_dir)
        assert check["ok"] is True
        assert check["checked"] == 10
        # Исходные журналы целы.
        assert _data_bytes(data_dir) == sources_before
        _no_plaintext_anywhere(backup_dir.parent)
        recovery = recover_pending_state(data_dir=data_dir, backup_dir=backup_dir.parent)
        assert recovery["ok"] is False
        assert recovery["reason"] == "snapshot_recovery_pending"
        assert recovery["path"] == str(backup_dir)
        # Новый снимок при незавершённой транзакции запрещён.
        with pytest.raises(SnapshotOperationRefused) as blocked:
            create_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=tmp_path / "backups" / "snapshot_2",
                crypto=crypto,
                transaction_id="tx-a52b1-crash2b",
                policy_on=True,
            )
        assert blocked.value.reason == "snapshot_pending_operation"

    def test_committed_requires_successful_readback(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = tmp_path / "backups" / "snapshot_1"

        with patch(
            "backend.encrypted_snapshot.verify_snapshot_readback",
            return_value={
                "ok": False,
                "state": STATE_COMMITTING,
                "transaction_id": "tx-a52b1-rb",
                "checked": 10,
                "mismatches": ["history.ndjson: sha256 не совпадает"],
            },
        ):
            with pytest.raises(SnapshotOperationRefused) as exc:
                create_encrypted_snapshot(
                    data_dir=data_dir,
                    backup_dir=backup_dir,
                    crypto=crypto,
                    transaction_id="tx-a52b1-rb",
                    policy_on=True,
                )
        assert exc.value.reason == "snapshot_readback_failed"
        # Состояние НЕ COMMITTED — несмотря на «успешную» публикацию.
        assert _manifest(backup_dir)["state"] == STATE_COMMITTING
        recovery = recover_pending_state(data_dir=data_dir, backup_dir=backup_dir.parent)
        assert recovery["ok"] is False

    def test_successful_commit_reports_committed_after_full_readback(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = tmp_path / "backups" / "snapshot_1"

        result = create_encrypted_snapshot(
            data_dir=data_dir,
            backup_dir=backup_dir,
            crypto=crypto,
            transaction_id="tx-a52b1-ok",
            policy_on=True,
        )
        assert result["ok"] is True
        assert result["state"] == STATE_COMMITTED
        assert result["readback"]["ok"] is True
        assert result["readback"]["checked"] == 10
        assert _manifest(backup_dir)["state"] == STATE_COMMITTED
        # Staging убран публикацией — незавершённых транзакций нет.
        assert _staging_dirs(backup_dir.parent) == []
        recovery = recover_pending_state(data_dir=data_dir, backup_dir=backup_dir.parent)
        assert recovery["ok"] is True
        assert recovery["pending"] is False
        assert recovery["reason"] is None
        _no_plaintext_anywhere(backup_dir.parent)

    def test_readback_detects_corrupted_and_missing_files(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = tmp_path / "backups" / "snapshot_1"
        create_encrypted_snapshot(
            data_dir=data_dir,
            backup_dir=backup_dir,
            crypto=crypto,
            transaction_id="tx-a52b1-corrupt",
            policy_on=True,
        )

        (backup_dir / "history_tags.ndjson").write_text("ENC1:tampered\n", encoding="utf-8")
        (backup_dir / "history_status.ndjson").unlink()
        check = verify_snapshot_readback(backup_dir=backup_dir)
        assert check["ok"] is False
        assert any("history_tags.ndjson" in m for m in check["mismatches"])
        assert any("history_status.ndjson" in m for m in check["mismatches"])

    def test_commit_refuses_when_policy_flipped_off_before_commit(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = tmp_path / "backups" / "snapshot_1"
        prepared = _prepared(tmp_path, data_dir, crypto, backup_dir)

        with pytest.raises(SnapshotOperationRefused) as exc:
            commit_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=backup_dir,
                transaction_id="tx-a52b1-0001",
                prepared=prepared,
                policy_read=lambda: False,
            )
        assert exc.value.reason == "snapshot_policy_unavailable"
        assert not backup_dir.exists()
        assert _staging_dirs(backup_dir.parent) == []

    def test_commit_refuses_into_existing_destination(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = tmp_path / "backups" / "snapshot_1"
        backup_dir.mkdir(parents=True)
        (backup_dir / "history.ndjson").write_text(_line(1) + "\n", encoding="utf-8")

        with pytest.raises(SnapshotOperationRefused) as exc:
            create_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=backup_dir,
                crypto=crypto,
                transaction_id="tx-a52b1-dest",
                policy_on=True,
            )
        assert exc.value.reason == "snapshot_destination_exists"
        # Существующий бэкап не тронут.
        assert (backup_dir / "history.ndjson").read_text("utf-8") == _line(1) + "\n"

