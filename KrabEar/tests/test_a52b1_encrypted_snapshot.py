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
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.auto_backup import AutoBackupManager
from backend.encrypted_snapshot import (
    REASON_POLICY_UNAVAILABLE,
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
from backend.history_encryption_policy import (
    OPERATION_UNAVAILABLE_REASON as REASON,
)
from backend.history_service import HistoryService
from backend.state_store import HISTORY_JOURNAL_FILENAMES, StateStore

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


def _read_ndjson_lines(path: Path) -> list[str]:
    """Читает файл НА ТОЧНУЮ границу строки ``\\n`` — как text-mode reader.

    ``str.splitlines()`` здесь неприменим: он делит по \\v \\f \\x1c
    \\x1d \\x1e \\x85 \\u2028 \\u2029, которых не видит ни writer, ни reader.
    """
    text = path.read_text("utf-8")
    if text == "":
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":  # хвостовой перевод строки
        lines.pop()
    return lines


class TestSnapshotRegistryCompleteness:
    def test_snapshot_contains_exactly_ten_registry_files(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = data_dir / "backups" / "snap"
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
        backup_dir = data_dir / "backups" / "snap"
        result = _prepared(tmp_path, data_dir, crypto, backup_dir)
        staging = Path(result["staging_dir"])

        for name in HISTORY_JOURNAL_FILENAMES:
            out_lines = _read_ndjson_lines(staging / name)
            assert out_lines, f"{name}: snapshot не должен быть пустым"
            for line in out_lines:
                assert line.startswith(SENTINEL), f"{name}: строка не ENC1"
            # ПОЗИЦИОННОЕ равенство: одна исходная строка → одна выходная, в том
            # же порядке. Проверка «membership» пропускала бы перестановку и
            # разбиение записи (CRITICAL-1).
            assert [crypto.decrypt_line(ln) for ln in out_lines] == expected[name]

    # Разрывы, которые str.splitlines() считает границей строки, а writer/reader
    # истории — нет (см. state_store: json.dumps(..., ensure_ascii=False) + "\n").
    _SPLIT_LIKE = ("\u2028", "\x85", "\v", "\f", "\x1c", "\x1e", "\u2029")

    def test_embedded_line_separators_do_not_split_one_record(self, tmp_path):
        """Одна NDJSON-запись со встроенными разрывами → ровно одна ENC1-строка."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        record = {"id": "r1", "text": "a\u2028" + "b\x85c\vd" + "\u2029"}
        source_line = json.dumps(record, ensure_ascii=False)
        (data_dir / "history.ndjson").write_text(
            source_line + "\n", encoding="utf-8"
        )
        backup_dir = data_dir / "backups" / "snap"
        result = _prepared(tmp_path, data_dir, crypto, backup_dir)
        staging = Path(result["staging_dir"])

        out_lines = _read_ndjson_lines(staging / "history.ndjson")
        assert len(out_lines) == 1, "запись не должна дробиться границами строк"
        assert out_lines[0].startswith(SENTINEL)
        assert crypto.decrypt_line(out_lines[0]) == source_line

    def test_records_with_unicode_separators_survive_committed_snapshot(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        expected = []
        for i, sep in enumerate(self._SPLIT_LIKE):
            rec = {"id": f"sep-{i}", "text": f"x{sep}y"}
            expected.append(json.dumps(rec, ensure_ascii=False))
        (data_dir / "history.ndjson").write_text(
            "\n".join(expected) + "\n", encoding="utf-8"
        )
        backup_dir = data_dir / "backups" / "snap"
        create_encrypted_snapshot(
            data_dir=data_dir,
            backup_dir=backup_dir,
            crypto=crypto,
            transaction_id="tx-sep",
            policy_on=True,
        )

        out_lines = _read_ndjson_lines(backup_dir / "history.ndjson")
        assert len(out_lines) == len(expected)
        assert [crypto.decrypt_line(ln) for ln in out_lines] == expected
        assert _manifest(backup_dir)["state"] == STATE_COMMITTED

    def test_roundtrip_guard_refuses_when_line_does_not_decrypt_back(self, tmp_path):
        """Сторож round-trip: выходная строка обязана расшифровываться в исходную.

        Даже если неисправность в самом шифровании (здесь — обрезающая обёртка),
        снимок не должен получить состояние PREPARED/COMMITTED.
        """
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        (data_dir / "history.ndjson").write_text(_line(1) + "\n", encoding="utf-8")

        class _CorruptingCrypto:
            def __init__(self, inner):
                self._inner = inner

            def is_encrypted(self, line):
                return self._inner.is_encrypted(line)

            def encrypt_line(self, plaintext):
                return self._inner.encrypt_line(plaintext)[:-4]

            def decrypt_line(self, token):
                return self._inner.decrypt_line(token)

        with pytest.raises(SnapshotOperationRefused) as exc:
            _prepared(tmp_path, data_dir, _CorruptingCrypto(crypto), data_dir / "backups" / "snap")
        assert exc.value.reason == "snapshot_roundtrip_mismatch"

    def test_manifest_records_only_fixed_names_size_and_ciphertext_hash(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = data_dir / "backups" / "snap"
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

        backup_dir = data_dir / "backups" / "snap"
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
        backup_dir = data_dir / "backups" / "snap"
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
        backup_dir = data_dir / "backups" / "snap"
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
        backup_dir = data_dir / "backups" / "snap"
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
        backup_dir = data_dir / "backups" / "snap"

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
        backup_dir = data_dir / "backups" / "snap"
        with pytest.raises(SnapshotOperationRefused) as exc:
            _prepared(tmp_path, data_dir, crypto, backup_dir)
        assert exc.value.reason == "snapshot_line_tampered"
        assert not (backup_dir / "history.ndjson").exists()

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
            _prepared(tmp_path, data_dir, crypto, data_dir / "backups" / "snap")
        assert exc.value.reason == "snapshot_source_symlink"

    def test_missing_crypto_is_refused(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        _fill_mixed(data_dir, _crypto())
        with pytest.raises(SnapshotOperationRefused) as exc:
            build_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=data_dir / "backups" / "snap",
                crypto=None,
                transaction_id="tx-a52b1-0002",
                policy_on=True,
            )
        assert exc.value.reason == "snapshot_crypto_unavailable"

    def test_staging_directory_is_private(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        result = _prepared(tmp_path, data_dir, crypto, data_dir / "backups" / "snap")
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
    """Неопубликованные staging-каталоги: backups/.staging/<транзакция>."""
    staging_root = Path(backups_root) / ".staging"
    if not staging_root.is_dir():
        return []
    return [p for p in staging_root.iterdir() if p.is_dir()]


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
        backup_dir = data_dir / "backups" / "snapshot_1"
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
        _fill_mixed(data_dir, crypto)
        backup_dir = data_dir / "backups" / "snapshot_1"
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
        assert exc.value.reason == "snapshot_publish_failed"
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
        # MAJOR-6: rename не состоялся ⇒ ни одного файла снимка в backups/ нет,
        # живая история цела и согласованна, откатывать нечего ⇒ это НЕ
        # незавершённая операция, а мусор (b2 удалит staging и повторит backup).
        # Fail-closed «pending» остаётся только для ОПУБЛИКОВАННОГО COMMITTING —
        # см. test_crash_after_publication_keeps_valid_snapshot_and_reports_pending.
        recovery = recover_pending_state(data_dir=data_dir, backups_root=backup_dir.parent)
        assert recovery["ok"] is True
        assert recovery["pending"] is False
        assert recovery["reason"] == "snapshot_stale_staging"
        assert recovery["published"] is False
        assert recovery["state"] == STATE_COMMITTING
        assert recovery["transaction_id"] == "tx-a52b1-crash1"
        # Доказательство для b2 при этом сохранено — staging не удалён.
        assert staging[0].is_dir()
        assert _payload_files(staging[0]) == sorted(HISTORY_JOURNAL_FILENAMES)
        # Никакого нового ключа: build_history_crypto не вызывался (патч-ловушка).

    def test_crash_after_publication_keeps_valid_snapshot_and_reports_pending(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = data_dir / "backups" / "snapshot_1"
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
        recovery = recover_pending_state(data_dir=data_dir, backups_root=backup_dir.parent)
        assert recovery["ok"] is False
        assert recovery["reason"] == "snapshot_recovery_pending"
        assert recovery["path"] == str(backup_dir)
        # Новый снимок при незавершённой транзакции запрещён.
        with pytest.raises(SnapshotOperationRefused) as blocked:
            create_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=data_dir / "backups" / "snapshot_2",
                crypto=crypto,
                transaction_id="tx-a52b1-crash2b",
                policy_on=True,
            )
        assert blocked.value.reason == "snapshot_pending_operation"

    def test_committed_requires_successful_readback(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = data_dir / "backups" / "snapshot_1"

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
        recovery = recover_pending_state(data_dir=data_dir, backups_root=backup_dir.parent)
        assert recovery["ok"] is False

    def test_successful_commit_reports_committed_after_full_readback(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = data_dir / "backups" / "snapshot_1"

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
        recovery = recover_pending_state(data_dir=data_dir, backups_root=backup_dir.parent)
        assert recovery["ok"] is True
        assert recovery["pending"] is False
        assert recovery["reason"] is None
        _no_plaintext_anywhere(backup_dir.parent)

    def test_readback_detects_corrupted_and_missing_files(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = data_dir / "backups" / "snapshot_1"
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
        backup_dir = data_dir / "backups" / "snapshot_1"
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
        backup_dir = data_dir / "backups" / "snapshot_1"
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

    def test_refusal_before_publication_leaves_no_committing_evidence(self, tmp_path):
        """Отказ ДО первой замены не должен оставлять durable COMMITTING (MAJOR-2).

        Иначе `COMMITTING` перестаёт означать «замена началась» — доказательная
        база b2 разрушается, а `recover_pending_state` залипает навсегда.
        """
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backups_root = data_dir / "backups"
        backup_dir = backups_root / "snapshot_1"
        backup_dir.mkdir(parents=True)
        (backup_dir / "history.ndjson").write_text(_line(1) + "\n", encoding="utf-8")

        with pytest.raises(SnapshotOperationRefused) as exc:
            create_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=backup_dir,
                crypto=crypto,
                transaction_id="tx-a52b1-dest2",
                policy_on=True,
            )
        assert exc.value.reason == "snapshot_destination_exists"
        # Ни staging, ни признака незавершённой транзакции.
        assert _staging_dirs(backups_root) == []
        recovery = recover_pending_state(data_dir=data_dir, backups_root=backups_root)
        assert recovery["pending"] is False
        assert recovery["reason"] is None
        assert recovery["ok"] is True

    def test_second_backup_in_same_second_does_not_poison_recovery(self, tmp_path):
        """Два backup'а в одну секунду: второй честно отказывает, b2-база цела."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backups_root = data_dir / "backups"
        first = backups_root / "snapshot_1"
        create_encrypted_snapshot(
            data_dir=data_dir, backup_dir=first, crypto=crypto,
            transaction_id="tx-a52b1-1", policy_on=True,
        )
        with pytest.raises(SnapshotOperationRefused) as exc:
            create_encrypted_snapshot(
                data_dir=data_dir, backup_dir=first, crypto=crypto,
                transaction_id="tx-a52b1-2", policy_on=True,
            )
        assert exc.value.reason == "snapshot_destination_exists"
        assert _staging_dirs(backups_root) == []
        recovery = recover_pending_state(data_dir=data_dir, backups_root=backups_root)
        assert recovery["pending"] is False
        # Первый снимок остался валидным и зафиксированным.
        assert _manifest(first)["state"] == STATE_COMMITTED

    def test_readback_rejects_manifest_with_foreign_names(self, tmp_path):
        """Манифест из 10 записей, но с ЧУЖИМИ именами — это не наш реестр."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = data_dir / "backups" / "snapshot_1"
        create_encrypted_snapshot(
            data_dir=data_dir,
            backup_dir=backup_dir,
            crypto=crypto,
            transaction_id="tx-a52b1-foreign",
            policy_on=True,
        )
        manifest = _manifest(backup_dir)
        for i, entry in enumerate(manifest["files"]):
            entry["name"] = f"foreign_{i}.ndjson"
        (backup_dir / SNAPSHOT_MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        check = verify_snapshot_readback(backup_dir=backup_dir)
        assert check["ok"] is False
        assert any("реестр" in m for m in check["mismatches"])

    def test_readback_rejects_extra_payload_file(self, tmp_path):
        """Лишний файл в снимке (например, подброшенный plaintext) — не «успех»."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = data_dir / "backups" / "snapshot_1"
        create_encrypted_snapshot(
            data_dir=data_dir,
            backup_dir=backup_dir,
            crypto=crypto,
            transaction_id="tx-a52b1-extra",
            policy_on=True,
        )
        (backup_dir / "extra_plaintext.ndjson").write_text(
            '{"id":"canary-extra","text":"kolya skazal sekret"}\n', encoding="utf-8"
        )
        check = verify_snapshot_readback(backup_dir=backup_dir)
        assert check["ok"] is False
        assert any("extra_plaintext.ndjson" in m for m in check["mismatches"])

    def test_readback_rejects_extra_subdirectory(self, tmp_path):
        """MINOR: read-back проверял только is_file() — подкаталог проходил молча.

        Внутрь опубликованного снимка нельзя подложить ничего: ни файл, ни
        каталог. Иначе «валидный» снимок может тащить посторонние данные.
        """
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = data_dir / "backups" / "snapshot_1"
        create_encrypted_snapshot(
            data_dir=data_dir, backup_dir=backup_dir, crypto=crypto,
            transaction_id="tx-subdir", policy_on=True,
        )
        (backup_dir / "nested").mkdir()
        (backup_dir / "nested" / "payload.ndjson").write_text(
            '{"id":"sneaky"}\n', encoding="utf-8"
        )

        check = verify_snapshot_readback(backup_dir=backup_dir)
        assert check["ok"] is False
        assert any("nested" in m for m in check["mismatches"])


# ----------------------------------------------------------------------
# Task 3: manual + auto backup при Encryption ON
# ----------------------------------------------------------------------

CANARY_CANARY = '{"id":"canary-legacy","text":"kolya skazal sekret"}'


def _settings_on(data_dir: Path) -> None:
    _settings(data_dir, {"history_encryption_enabled": True})


def _settings_off(data_dir: Path) -> None:
    _settings(data_dir, {"history_encryption_enabled": False})


def _store_with_crypto(data_dir: Path, crypto: HistoryCrypto | None) -> StateStore:
    store = StateStore(data_dir)
    # Инъекция ключа БЕЗ Keychain (тот же приём, что в существующих тестах A5.1).
    store._get_history_crypto = lambda: crypto
    return store


def _snapshot_payload(snapshot_dir: Path) -> list[str]:
    return _payload_files(snapshot_dir)


class TestManualBackupEncryptedSnapshot:
    def test_manual_backup_on_creates_encrypted_snapshot(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        svc = HistoryService(store=store, cached_settings=lambda: {})
        sources_before = _data_bytes(data_dir)

        result = svc.handle_backup_history({})

        assert result["ok"] is True
        assert result["reason"] is None
        snapshot_dir = Path(result["backup_path"])
        assert snapshot_dir.is_dir()
        # Ровно 10 журналов реестра + манифест, все строки ENC1.
        assert _snapshot_payload(snapshot_dir) == sorted(HISTORY_JOURNAL_FILENAMES)
        for name in HISTORY_JOURNAL_FILENAMES:
            body = (snapshot_dir / name).read_text("utf-8")
            if body:
                assert all(ln.startswith(SENTINEL) for ln in body.splitlines())
        manifest = _manifest(snapshot_dir)
        assert manifest["state"] == STATE_COMMITTED
        assert manifest["version"] == SNAPSHOT_MANIFEST_VERSION
        assert manifest["policy_at_capture"] is True
        # settings.json не входит в payload.
        assert "settings.json" not in _snapshot_payload(snapshot_dir)
        # Источники не тронуты.
        assert _data_bytes(data_dir) == sources_before

    def test_manual_backup_on_writes_no_plaintext_anywhere(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        (data_dir / "history.ndjson").write_text(CANARY_CANARY + "\n", encoding="utf-8")
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        svc = HistoryService(store=store, cached_settings=lambda: {})

        result = svc.handle_backup_history({})
        assert result["ok"] is True
        _no_plaintext_anywhere(Path(result["backup_path"]))
        # Ровно один снимок создан — никаких legacy plaintext-копий рядом.
        backups = data_dir / "backups"
        # .staging — служебный корень, а не бэкап.
        created = [p for p in backups.iterdir() if p.is_dir() and not p.name.startswith(".")]
        assert len(created) == 1
        # Исходный plaintext-журнал на месте (мы не перезаписываем историю).
        assert (data_dir / "history.ndjson").read_text("utf-8") == CANARY_CANARY + "\n"

    def test_manual_backup_on_without_key_refuses(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, None)  # ключ недоступен
        svc = HistoryService(store=store, cached_settings=lambda: {})

        result = svc.handle_backup_history({})
        assert result["ok"] is False
        assert result["reason"] == REASON
        assert result["backup_path"] is None
        assert not (data_dir / "backups").exists()

    def test_manual_backup_on_refuses_when_policy_flips_off_under_lock(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        svc = HistoryService(store=store, cached_settings=lambda: {})
        real_lock = store._lock
        state = {"flipped": False}

        @contextmanager
        def flipping_lock(*args, **kwargs):
            if not state["flipped"]:
                state["flipped"] = True
                _settings_off(data_dir)  # ON → OFF прямо под lock
            with real_lock(*args, **kwargs):
                yield

        with patch.object(store, "_lock", flipping_lock):
            result = svc.handle_backup_history({})

        assert result["ok"] is False
        # Причина — константа модуля, а не строковый литерал в сервисе.
        assert result["reason"] == REASON_POLICY_UNAVAILABLE
        assert not (data_dir / "backups").exists()

    def test_manual_backup_off_path_is_untouched(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        (data_dir / "history.ndjson").write_text(_line(1) + "\n", encoding="utf-8")
        _settings_off(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        svc = HistoryService(store=store, cached_settings=lambda: {})

        with patch(
            "backend.history_service.create_encrypted_snapshot",
            side_effect=AssertionError("OFF-профиль не должен вызывать snapshot"),
            create=True,
        ):
            result = svc.handle_backup_history({})

        # Прежнее поведение: legacy-копия history + settings, имя backup_<ts>.
        assert result["backup_path"]
        assert Path(result["backup_path"]).name.startswith("backup_")
        names = sorted(p.name for p in Path(result["backup_path"]).iterdir())
        assert "history.ndjson" in names
        assert "settings.json" in names
        assert "backup_meta.json" in names

    def test_manual_backup_off_never_touches_snapshot_module(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = _store_with_crypto(data_dir, None)
        svc = HistoryService(store=store, cached_settings=lambda: {})
        with patch(
            "backend.encrypted_snapshot.create_encrypted_snapshot",
            side_effect=AssertionError("OFF-профиль не должен вызывать snapshot"),
        ):
            result = svc.handle_backup_history({})
        assert result["backup_path"]


class TestAutoBackupEncryptedSnapshot:
    def _manager(self, store, **kwargs):
        return AutoBackupManager(store=store, interval_hours=0, **kwargs)

    def test_auto_backup_on_creates_encrypted_snapshot(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        mgr = self._manager(store)

        out = mgr.check_and_backup()

        assert out["backed_up"] is True
        assert out["skipped_reason"] is None
        snapshot_dir = Path(out["backup_path"])
        assert snapshot_dir.is_dir()
        assert _snapshot_payload(snapshot_dir) == sorted(HISTORY_JOURNAL_FILENAMES)
        assert _manifest(snapshot_dir)["state"] == STATE_COMMITTED
        assert "settings.json" not in _snapshot_payload(snapshot_dir)
        _no_plaintext_anywhere(snapshot_dir)
        # Снимок не перепутан с legacy-копией и попал в meta.
        assert snapshot_dir.name.startswith("auto_snapshot_")
        meta = json.loads((data_dir / "backups" / "auto_backup_meta.json").read_text("utf-8"))
        assert meta["backup_count"] == 1

    def test_auto_backup_on_without_key_reports_skipped_reason(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        _settings_on(data_dir)
        sources_before = _data_bytes(data_dir)
        store = _store_with_crypto(data_dir, None)
        mgr = self._manager(store)

        out = mgr.check_and_backup()
        assert out["backed_up"] is False
        assert out["skipped_reason"] == REASON
        # Отказ не создаёт СОДЕРЖИМОГО бэкапа: ни одного каталога-копии, ни
        # settings.json, ни файлов истории. Единственное, что может появиться, —
        # dot-prefixed sidecar протокола (метаданные последнего исхода), который
        # переживает рестарт (N1) и не является копией истории.
        backups = data_dir / "backups"
        assert [p.name for p in backups.iterdir() if p.is_dir()] == []
        assert [p.name for p in backups.iterdir() if p.is_file()] == [".last_result.json"]
        raw = (backups / ".last_result.json").read_text("utf-8")
        assert "ENC1:" not in raw
        # Источники не тронуты.
        assert _data_bytes(data_dir) == sources_before
        assert (data_dir / "history.ndjson").stat().st_size > 0

    def test_auto_backup_on_does_not_prune_legacy_or_new_snapshots(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backups = data_dir / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        legacy = []
        for i in range(4):
            d = backups / f"auto_backup_2020010{i}_000000"
            d.mkdir()
            (d / "backup_meta.json").write_text(f'{{"i": {i}}}', encoding="utf-8")
            legacy.append(d.name)
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        mgr = self._manager(store, max_copies=1)

        out = mgr.check_and_backup()
        assert out["backed_up"] is True
        # Retention при ON не удаляет ни legacy-копии, ни снимки нового протокола.
        for name in legacy:
            assert (backups / name / "backup_meta.json").exists()
        snapshot_dir = Path(out["backup_path"])
        assert snapshot_dir.is_dir()
        assert _manifest(snapshot_dir)["state"] == STATE_COMMITTED

    def test_auto_backup_off_path_and_prune_unchanged(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        (data_dir / "history.ndjson").write_text(_line(1) + "\n", encoding="utf-8")
        _settings_off(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        backups = data_dir / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        old = backups / "auto_backup_20200101_000000"
        old.mkdir()
        (old / "backup_meta.json").write_text("{}", encoding="utf-8")
        mgr = self._manager(store, max_copies=1)

        with patch(
            "backend.encrypted_snapshot.create_encrypted_snapshot",
            side_effect=AssertionError("OFF-профиль не должен вызывать snapshot"),
        ):
            out = mgr.check_and_backup()

        assert out["backed_up"] is True
        # Прежнее поведение: legacy-копия, prune по max_copies, meta обновлена.
        legacy_dir = Path(out["backup_path"])
        assert legacy_dir.name.startswith("auto_backup_")
        assert (legacy_dir / "history.ndjson").exists()
        assert (legacy_dir / "settings.json").exists()
        assert not old.exists()  # prune отработал как раньше


class TestSnapshotIsNotRestorable:
    """MAJOR-3: снимок нельзя скормить legacy restore (шифротекст ≠ plaintext).

    Legacy restore копирует `history.ndjson` через copy2. Каталог снимка тоже
    содержит `history.ndjson` — но строки ENC1. До b2 такой restore затирал бы
    живую историю шифротекстом и возвращал «успех».
    """

    def _on_store(self, data_dir: Path) -> tuple[StateStore, HistoryService, HistoryCrypto]:
        crypto = _crypto()
        _settings_on(data_dir)
        _fill_mixed(data_dir, crypto)
        store = _store_with_crypto(data_dir, crypto)
        return store, HistoryService(store=store, cached_settings=lambda: {}), crypto

    def _auto_snapshot(self, data_dir: Path) -> Path:
        store, _svc, _crypto_ = self._on_store(data_dir)
        out = AutoBackupManager(store=store, interval_hours=0).check_and_backup()
        assert out["backed_up"] is True
        return Path(out["backup_path"])

    def test_list_backups_does_not_offer_snapshot_as_restorable(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store, svc, _crypto_ = self._on_store(data_dir)
        snapshot_dir = self._auto_snapshot(data_dir)

        listed = svc.handle_list_backups({})
        paths = [b["path"] for b in listed["backups"]]
        assert str(snapshot_dir) not in paths
        # Владелец всё равно видит, что снимок существует, но НЕ как restorable.
        snaps = listed.get("encrypted_snapshots") or []
        assert [s["path"] for s in snaps] == [str(snapshot_dir)]
        assert snaps[0]["restorable"] is False
        assert snaps[0]["reason"] == "unsupported_backup_format"

    def test_restore_from_snapshot_refuses_and_keeps_live_history(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store, svc, _crypto_ = self._on_store(data_dir)
        snapshot_dir = self._auto_snapshot(data_dir)

        # Владелец выключил шифрование (OFF-профиль) и указал на «бэкап».
        _settings_off(data_dir)
        off_store = _store_with_crypto(data_dir, None)
        off_svc = HistoryService(store=off_store, cached_settings=lambda: {})
        live = '{"id":"live","text":"живая запись"}\n'
        (data_dir / "history.ndjson").write_text(live, encoding="utf-8")

        result = off_svc.handle_restore_history({"backup_path": str(snapshot_dir)})

        assert result["ok"] is False
        assert result["reason"] == "unsupported_backup_format"
        assert result["restored_entries"] == 0
        # 🔴 Живая история не тронута — ни байтом.
        assert (data_dir / "history.ndjson").read_text("utf-8") == live

    def test_restore_from_manual_snapshot_refuses(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        # ТОТ ЖЕ ключ, которым зашифрованы журналы: снимок обязан читаться им.
        _store, _svc, crypto = self._on_store(data_dir)
        svc_on = HistoryService(
            store=_store_with_crypto(data_dir, crypto), cached_settings=lambda: {}
        )
        made = svc_on.handle_backup_history({})
        assert made["ok"] is True
        snapshot_dir = Path(made["backup_path"])

        _settings_off(data_dir)
        off_svc = HistoryService(
            store=_store_with_crypto(data_dir, None), cached_settings=lambda: {}
        )
        result = off_svc.handle_restore_history({"backup_path": str(snapshot_dir)})
        assert result["ok"] is False
        assert result["reason"] == "unsupported_backup_format"

    def test_restore_from_staging_dir_refuses(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = data_dir / "backups" / "snapshot_1"
        prepared = build_encrypted_snapshot(
            data_dir=data_dir,
            backup_dir=backup_dir,
            crypto=crypto,
            transaction_id="tx-staging-restore",
            policy_on=True,
        )
        staging = Path(prepared["staging_dir"])
        # OFF явно: иначе первым сработает A5.2a-гейт (settings отсутствуют, а
        # ENC1 в журналах есть ⇒ policy fail-closed = ON).
        _settings_off(data_dir)
        off_svc = HistoryService(
            store=_store_with_crypto(data_dir, None), cached_settings=lambda: {}
        )
        result = off_svc.handle_restore_history({"backup_path": str(staging)})
        assert result["ok"] is False
        assert result["reason"] == "unsupported_backup_format"
        # Неопубликованный staging вообще не должен попадать в backups-список.
        listed = off_svc.handle_list_backups({})
        assert staging.name not in [Path(b["path"]).name for b in listed["backups"]]

    def test_restore_from_unknown_dir_name_refuses(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = _store_with_crypto(data_dir, None)
        svc = HistoryService(store=store, cached_settings=lambda: {})
        stray = data_dir / "backups" / "my_random_folder"
        stray.mkdir(parents=True)
        (stray / "history.ndjson").write_text('{"id":"x"}\n', encoding="utf-8")

        result = svc.handle_restore_history({"backup_path": str(stray)})
        assert result["ok"] is False
        assert result["reason"] == "unsupported_backup_format"

    def test_legacy_backup_is_still_restorable(self, tmp_path):
        """OFF-регресс: настоящий legacy-бэкап восстанавливается как раньше."""
        data_dir = _data_dir(tmp_path)
        store = _store_with_crypto(data_dir, None)
        svc = HistoryService(store=store, cached_settings=lambda: {})
        # Записи пишет сам store — иначе count_active_items не увидит «живых».
        store.add_history_item(text="one")
        store.add_history_item(text="two")
        made = svc.handle_backup_history({})
        assert Path(made["backup_path"]).name.startswith("backup_")
        (data_dir / "history.ndjson").write_text("", encoding="utf-8")

        result = svc.handle_restore_history({"backup_path": made["backup_path"]})
        assert result.get("ok") is not False
        assert result["restored_entries"] == 2
        assert "one" in (data_dir / "history.ndjson").read_text("utf-8")

    def test_staging_is_placed_under_dot_staging_dir(self, tmp_path):
        """Staging живёт в backups/.staging/ — dot-prefixed, чтобы не всплывал."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = data_dir / "backups" / "snapshot_1"
        prepared = build_encrypted_snapshot(
            data_dir=data_dir,
            backup_dir=backup_dir,
            crypto=crypto,
            transaction_id="tx-stage-loc",
            policy_on=True,
        )
        staging = Path(prepared["staging_dir"])
        backups_root = backup_dir.parent
        assert staging.parent == backups_root / ".staging"
        assert staging.name.startswith(".")


class TestAutoBackupStatusHonesty:
    """MAJOR-4: статус при ON не должен вечно врать «backup недоступен»."""

    def _manager(self, store, **kwargs):
        return AutoBackupManager(store=store, interval_hours=0, **kwargs)

    def test_status_after_successful_snapshot_is_honest(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        mgr = self._manager(store)

        out = mgr.check_and_backup()
        assert out["backed_up"] is True
        status = mgr.get_auto_backup_status()

        # Backup при ON доступен — снимок создан и зафиксирован.
        assert status["encryption_operation_unavailable"] is False
        assert status["skipped_reason"] is None
        assert status["last_backup_kind"] == "encrypted_snapshot"
        assert status["last_refusal_reason"] is None
        assert status["encrypted_snapshots"] == 1
        # total_backups по-прежнему про legacy auto_backup_* (не ломаем потребителя)
        assert status["total_backups"] == 0

    def test_status_reports_real_refusal_reason(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, None)  # ключ недоступен
        mgr = self._manager(store)

        out = mgr.check_and_backup()
        assert out["backed_up"] is False
        status = mgr.get_auto_backup_status()

        # Настоящий отказ виден и отличим от «просто ничего не было».
        assert status["last_refusal_reason"] == REASON
        assert status["last_backup_kind"] is None
        assert status["encrypted_snapshots"] == 0

    def test_status_flags_unfinished_transaction_as_unavailable(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        mgr = self._manager(store)
        # Опубликованная незавершённая транзакция: backup сейчас невозможен.
        backup_dir = data_dir / "backups" / "snapshot_stuck"
        create_encrypted_snapshot(
            data_dir=data_dir, backup_dir=backup_dir, crypto=crypto,
            transaction_id="tx-stuck", policy_on=True,
        )
        manifest = _manifest(backup_dir)
        manifest["state"] = STATE_COMMITTING
        (backup_dir / SNAPSHOT_MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        status = mgr.get_auto_backup_status()
        assert status["encryption_operation_unavailable"] is True
        assert status["skipped_reason"] == "snapshot_recovery_pending"
        assert status["last_backup_kind"] is None

    def test_status_off_profile_keeps_legacy_fields(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        (data_dir / "history.ndjson").write_text(_line(1) + "\n", encoding="utf-8")
        _settings_off(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        mgr = self._manager(store)

        out = mgr.check_and_backup()
        assert out["backed_up"] is True
        status = mgr.get_auto_backup_status()

        assert status["last_backup_kind"] == "legacy_plaintext"
        assert status["last_refusal_reason"] is None
        assert status["encrypted_snapshots"] == 0
        assert status["encryption_operation_unavailable"] is False

    def test_auto_backup_snapshot_reports_real_entry_count(self, tmp_path):
        """MAJOR-5: успешный снимок обязан сообщать entries, а не 0.

        Legacy-путь считает записи (count_active_items) и падает в исключение
        при ошибке; снимок возвращал hardcoded 0 с обещанием «вызывающий
        заполнит», а вызывающий просто пробрасывал.
        """
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        store.add_history_item(text="one")
        store.add_history_item(text="two")

        out = AutoBackupManager(store=store, interval_hours=0).check_and_backup()

        assert out["backed_up"] is True
        assert out["entries"] == 2
        mgr = AutoBackupManager(store=store, interval_hours=0)
        assert mgr.get_auto_backup_status()["last_backup_kind"] == "encrypted_snapshot"


class TestStaleStagingIsNotPending:
    """MAJOR-6: неопубликованный staging — мусор, а не незавершённая операция.

    b2 будет строить recovery на recover_pending_state. Если она вечно видит
    pending=True из-за брошенного staging (crash в prepare), recovery не сможет
    отличить «нужен разбор» от «удали мусор и работай дальше».
    """

    def _data_dir_with_staging(self, tmp_path: Path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = data_dir / "backups" / "snapshot_1"
        prepared = build_encrypted_snapshot(
            data_dir=data_dir, backup_dir=backup_dir, crypto=crypto,
            transaction_id="tx-crash-prepare", policy_on=True,
        )
        return data_dir, crypto, backup_dir.parent, Path(prepared["staging_dir"])

    def test_unpublished_staging_is_not_pending(self, tmp_path):
        data_dir, _crypto_, backups_root, staging = self._data_dir_with_staging(tmp_path)
        assert staging.is_dir()

        rec = recover_pending_state(data_dir=data_dir, backups_root=backups_root)

        assert rec["pending"] is False
        assert rec["ok"] is True
        assert rec["reason"] == "snapshot_stale_staging"
        assert rec["stale_staging"], "мусорный staging должен быть назван прямо"
        assert staging.name in rec["stale_staging"][0]

    def test_stale_staging_does_not_block_new_snapshot(self, tmp_path):
        data_dir, crypto, backups_root, _staging = self._data_dir_with_staging(tmp_path)
        res = create_encrypted_snapshot(
            data_dir=data_dir, backup_dir=data_dir / "backups" / "snapshot_2",
            crypto=crypto, transaction_id="tx-after-stale", policy_on=True,
        )
        assert res["state"] == STATE_COMMITTED
        rec = recover_pending_state(data_dir=data_dir, backups_root=backups_root)
        assert rec["pending"] is False

    def test_auto_status_not_blocked_by_stale_staging(self, tmp_path):
        data_dir, crypto, _root, _staging = self._data_dir_with_staging(tmp_path)
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        mgr = AutoBackupManager(store=store, interval_hours=0)
        status = mgr.get_auto_backup_status()
        assert status["encryption_operation_unavailable"] is False
        assert status["skipped_reason"] is None

    def test_published_committing_remains_pending(self, tmp_path):
        """Опубликованный COMMITTING по-прежнему fail-closed (b2 обязан его видеть)."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        backup_dir = data_dir / "backups" / "snapshot_1"
        create_encrypted_snapshot(
            data_dir=data_dir, backup_dir=backup_dir, crypto=crypto,
            transaction_id="tx-published", policy_on=True,
        )
        manifest = _manifest(backup_dir)
        manifest["state"] = STATE_COMMITTING
        (backup_dir / SNAPSHOT_MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        rec = recover_pending_state(data_dir=data_dir, backups_root=backup_dir.parent)
        assert rec["pending"] is True
        assert rec["ok"] is False
        assert rec["reason"] == "snapshot_recovery_pending"
        assert rec["state"] == STATE_COMMITTING


class TestAutoBackupPolicyRace:
    """MAJOR-7: гонка политики под store-lock на АВТО-пути (у ручного — тест выше).

    Решение «снимок или legacy-копия» принимается ОДИН раз — под store-lock в
    `_do_backup` (см. коммит refactor(a5.2b1)). Поэтому авто-путь следует
    политике, наблюдаемой в момент записи, а ручной — отказывается: там
    пользователь уже инициировал операцию под ON, и молча переключать её в
    plaintext нельзя. Асимметрия зафиксирована тестами намеренно.
    """

    def test_on_to_off_under_store_lock_follows_policy_at_write_time(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        mgr = AutoBackupManager(store=store, interval_hours=0)
        real_lock = store._lock
        state = {"flipped": False}

        @contextmanager
        def flipping_lock(*args, **kwargs):
            if not state["flipped"]:
                state["flipped"] = True
                # ON → OFF ровно перед захватом реального lock.
                (data_dir / "settings.json").write_text(
                    json.dumps({"history_encryption_enabled": False}), encoding="utf-8"
                )
            with real_lock(*args, **kwargs):
                yield

        with patch.object(store, "_lock", flipping_lock):
            out = mgr.check_and_backup()

        assert state["flipped"] is True
        backups = data_dir / "backups"
        names = sorted(p.name for p in backups.iterdir() if p.is_dir())
        # Политика в момент записи — OFF ⇒ OFF-профиль, снимка быть не должно.
        assert out["backed_up"] is True
        assert not [n for n in names if n.startswith("auto_snapshot_")]
        assert [n for n in names if n.startswith("auto_backup_")]
        assert mgr.get_auto_backup_status()["last_backup_kind"] == "legacy_plaintext"

    def test_on_to_off_never_leaves_half_written_state(self, tmp_path):
        """Гонка не должна оставить ни staging, ни незавершённой транзакции."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        mgr = AutoBackupManager(store=store, interval_hours=0)
        real_lock = store._lock
        state = {"flipped": False}

        @contextmanager
        def flipping_lock(*args, **kwargs):
            if not state["flipped"]:
                state["flipped"] = True
                (data_dir / "settings.json").write_text(
                    json.dumps({"history_encryption_enabled": False}), encoding="utf-8"
                )
            with real_lock(*args, **kwargs):
                yield

        with patch.object(store, "_lock", flipping_lock):
            mgr.check_and_backup()

        backups = data_dir / "backups"
        rec = recover_pending_state(data_dir=data_dir, backups_root=backups)
        assert rec["pending"] is False


class TestSnapshotContainment:
    """MINOR (containment): снимок не может уйти за пределы backups-области.

    Проверяется РАЗРЕШЁННЫЙ путь назначения: и лексический (никаких `..` и
    чужих каталогов), и разыменованный (симлинк внутри backups не уводит снимок
    наружу). Отдельно закреплено решение по симлинку на САМОМ каталоге backups.
    """

    def _data_dir_with_history(self, tmp_path: Path) -> tuple[Path, HistoryCrypto]:
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        (data_dir / "history.ndjson").write_text(_line(1) + "\n", encoding="utf-8")
        return data_dir, crypto

    def test_destination_outside_backups_root_is_refused(self, tmp_path):
        data_dir, crypto = self._data_dir_with_history(tmp_path)
        outside = tmp_path / "elsewhere" / "snap"
        with pytest.raises(SnapshotOperationRefused) as exc:
            build_encrypted_snapshot(
                data_dir=data_dir, backup_dir=outside, crypto=crypto,
                transaction_id="tx-outside", policy_on=True,
            )
        assert exc.value.reason == "snapshot_outside_backups_root"
        assert not outside.exists()

    def test_lexical_escape_via_parent_segments_is_refused(self, tmp_path):
        data_dir, crypto = self._data_dir_with_history(tmp_path)
        (data_dir / "backups").mkdir()
        escape = data_dir / "backups" / ".." / "evil"
        with pytest.raises(SnapshotOperationRefused) as exc:
            build_encrypted_snapshot(
                data_dir=data_dir, backup_dir=escape, crypto=crypto,
                transaction_id="tx-dotdot", policy_on=True,
            )
        assert exc.value.reason == "snapshot_outside_backups_root"
        assert not (data_dir / "evil").exists()

    def test_symlinked_destination_inside_backups_is_refused(self, tmp_path):
        data_dir, crypto = self._data_dir_with_history(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (data_dir / "backups").mkdir()
        (data_dir / "backups" / "snap").symlink_to(outside)
        with pytest.raises(SnapshotOperationRefused) as exc:
            build_encrypted_snapshot(
                data_dir=data_dir, backup_dir=data_dir / "backups" / "snap",
                crypto=crypto, transaction_id="tx-symlink-dest", policy_on=True,
            )
        assert exc.value.reason == "snapshot_outside_backups_root"
        assert list(outside.iterdir()) == []

    def test_symlinked_backups_root_is_followed_to_real_location(self, tmp_path):
        """РЕШЕНИЕ: симлинк на САМ каталог backups допускается и разыменовывается.

        Владелец вправе держать backups на другом томе — это легальная
        конфигурация, и запрещать её нельзя. Снимок обязан лежать рядом с
        остальными бэкапами, то есть по РЕАЛЬНОМУ пути backups; проверка
        containment сравнивает разыменованные пути с обеих сторон, поэтому
        «настоящий» backups и снимок совпадают. Запрещено лишь уйти из этой
        области (три теста выше).
        """
        data_dir, crypto = self._data_dir_with_history(tmp_path)
        real_backups = tmp_path / "real_backups"
        real_backups.mkdir()
        (data_dir / "backups").symlink_to(real_backups)

        result = create_encrypted_snapshot(
            data_dir=data_dir, backup_dir=data_dir / "backups" / "snapshot_1",
            crypto=crypto, transaction_id="tx-root-symlink", policy_on=True,
        )

        assert result["state"] == STATE_COMMITTED
        published = Path(result["backup_dir"])
        # Снимок физически лежит в РЕАЛЬНОМ backups, а не рядом с симлинком.
        assert published.parent == real_backups
        assert _manifest(published)["state"] == STATE_COMMITTED
        assert _payload_files(published) == sorted(HISTORY_JOURNAL_FILENAMES)


class TestAutoBackupRefusalObservability:
    """N1: отказ при ON обязан быть виден в полях СТАТУСА, а не только цикла.

    A5.2a ввела `encryption_operation_unavailable`/`skipped_reason` именно
    потому, что backend startup и RecordingCore игнорируют результат
    `check_and_backup` (единственная IPC-поверхность — get_auto_backup_status).
    Полный отказ backup-цикла, не отражённый в статусе, — это тихий отказ.
    """

    def _on_profile(self, tmp_path: Path, crypto):
        """Профиль с ON и заданным ключом (None ⇒ ключ недоступен)."""
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        return data_dir, _store_with_crypto(data_dir, crypto)

    def test_refusal_is_visible_in_status_fields(self, tmp_path):
        """N1.3 (наблюдаемость): реальный отказ ⇒ unavailable/skipped_reason."""
        data_dir, store = self._on_profile(tmp_path, None)  # ключ недоступен
        mgr = AutoBackupManager(store=store, interval_hours=0)

        out = mgr.check_and_backup()
        assert out["backed_up"] is False
        status = mgr.get_auto_backup_status()

        assert status["encryption_operation_unavailable"] is True
        assert status["skipped_reason"] == REASON
        assert status["last_refusal_reason"] == REASON

    def test_refusal_survives_restart(self, tmp_path):
        """N1.1: после рестарта причина отказа не теряется (sidecar-протокол)."""
        data_dir, store = self._on_profile(tmp_path, None)
        first = AutoBackupManager(store=store, interval_hours=0)
        assert first.check_and_backup()["backed_up"] is False

        # Свежий менеджер — эмуляция рестарта backend на том же профиле.
        fresh_store = _store_with_crypto(data_dir, None)
        fresh = AutoBackupManager(store=fresh_store, interval_hours=0)
        status = fresh.get_auto_backup_status()

        assert status["encryption_operation_unavailable"] is True
        assert status["skipped_reason"] == REASON
        assert status["last_refusal_reason"] == REASON
        assert status["last_backup_kind"] is None
        # Sidecar dot-prefixed и без секретов.
        sidecar = data_dir / "backups" / ".last_result.json"
        assert sidecar.is_file()
        raw = sidecar.read_text("utf-8")
        payload = json.loads(raw)
        assert payload["refusal_reason"] == REASON
        # Sidecar хранит только метаданные протокола: ни ключа, ни ENC1, ни текста.
        assert set(payload) <= {"version", "kind", "refusal_reason", "recorded_at"}
        assert not any(token in raw for token in ("ENC1:", "history_key", "BEGIN"))

    def test_status_dict_is_not_self_contradictory(self, tmp_path):
        """N1.2: отказ не может сосуществовать с kind=encrypted_snapshot."""
        crypto = _crypto()
        data_dir, store = self._on_profile(tmp_path, crypto)
        mgr = AutoBackupManager(store=store, interval_hours=0)
        assert mgr.check_and_backup()["backed_up"] is True

        # Ключ стал недоступен — следующий цикл честно отказывает.
        store._get_history_crypto = lambda: None
        assert mgr.check_and_backup()["backed_up"] is False

        status = mgr.get_auto_backup_status()
        assert status["last_refusal_reason"] == REASON
        # Каталог снимка на диске есть, но последний цикл отказал ⇒ kind=None.
        assert status["last_backup_kind"] is None
        assert status["encryption_operation_unavailable"] is True

    def test_successful_on_backup_is_not_unavailable(self, tmp_path):
        """N1.3 (отдельный сценарий): успешный ON-бэкап ⇒ unavailable == False."""
        crypto = _crypto()
        data_dir, store = self._on_profile(tmp_path, crypto)
        mgr = AutoBackupManager(store=store, interval_hours=0)
        assert mgr.check_and_backup()["backed_up"] is True

        status = mgr.get_auto_backup_status()
        assert status["encryption_operation_unavailable"] is False
        assert status["skipped_reason"] is None
        assert status["last_backup_kind"] == "encrypted_snapshot"
        assert status["last_refusal_reason"] is None

    def test_sidecar_does_not_appear_in_legacy_backup_listing(self, tmp_path):
        data_dir, store = self._on_profile(tmp_path, None)
        mgr = AutoBackupManager(store=store, interval_hours=0)
        mgr.check_and_backup()
        svc = HistoryService(store=store, cached_settings=lambda: {})
        listed = svc.handle_list_backups({})
        assert not any(
            "last_result" in b["path"] for b in listed["backups"]
        ), "sidecar не должен попадать в список бэкапов"
        assert not any(
            "last_result" in s["path"] for s in listed.get("encrypted_snapshots") or []
        )

    def test_purge_clears_recorded_outcome(self, tmp_path):
        """Purge не должен оставлять в статусе следы удалённых бэкапов."""
        crypto = _crypto()
        data_dir, store = self._on_profile(tmp_path, crypto)
        mgr = AutoBackupManager(store=store, interval_hours=0)
        assert mgr.check_and_backup()["backed_up"] is True

        mgr.set_purged()
        status = mgr.get_auto_backup_status()
        assert status["last_backup_kind"] is None
        assert status["last_refusal_reason"] is None
        assert status["encrypted_snapshots"] == 0
        assert status["encryption_operation_unavailable"] is False
