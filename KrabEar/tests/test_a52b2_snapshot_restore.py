"""A5.2b2 — restore из encrypted snapshot + fail-closed recovery.

Спека `docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md` §5
(абзацы про restore/ledger union) и карточка
`docs/superpowers/plans/2026-09-26-a52b2-snapshot-restore.md`.

Только synthetic tmp-профили и СЛУЧАЙНЫЙ тестовый ключ (`os.urandom(32)`).
Системный Keychain не трогается: единственная точка вызова `security(1)` —
`backend.crypto_keystore._run_security`, она за патчем-счётчиком, который
СЧИТАЕТ обращения и падает при первом же. Тест `test_keychain_attempts_stay_zero`
доказывает, что за весь b2 обращений к Keychain не было.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.encrypted_snapshot import (
    RESTORE_MARKER_FILENAME,
    RESTORE_STAGING_PREFIX,
    SNAPSHOT_MANIFEST_FILENAME,
    SNAPSHOT_MANIFEST_VERSION,
    STATE_COMMITTED,
    SnapshotOperationRefused,
    collect_ledger_union,
    create_encrypted_snapshot,
    has_pending_restore,
    purge_pending_restore_staging,
    recover_pending_restore,
    restore_encrypted_snapshot,
    verify_snapshot,
)
from backend.history_crypto import SENTINEL, HistoryCrypto
from backend.history_service import HistoryService
from backend.state_store import HISTORY_JOURNAL_FILENAMES, StateStore

# Строка-«canary»: её plaintext-хэш/текст не должны появляться в sidecar'ах.
CANARY_PLAINTEXT = '{"id":"canary-a52b2","text":"kolya skazal sekret"}'

# Счётчик обращений к Keychain (см. фикстуру ниже). Модульный, чтобы итоговое
# утверждение видело ВСЕ обращения за сессию, а не только за один тест.
KEYCHAIN_ATTEMPTS = {"count": 0}


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


def _settings_on(data_dir: Path) -> None:
    _settings(data_dir, {"history_encryption_enabled": True})


def _settings_off(data_dir: Path) -> None:
    _settings(data_dir, {"history_encryption_enabled": False})


def _policy_on(data_dir: Path) -> bool:
    """Fail-closed reader флага для модульных вызовов restore (без StateStore)."""
    from backend.history_encryption_policy import data_dir_policy_reader

    return data_dir_policy_reader(data_dir)


def _line(index: int) -> str:
    return json.dumps({"id": f"e{index}", "text": f"запись-{index}"}, ensure_ascii=False)


def _fill_mixed(data_dir: Path, crypto: HistoryCrypto) -> dict[str, list[str]]:
    """Заполняет все 10 журналов смесью plaintext и ENC1-строк."""
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


def _make_snapshot(
    data_dir: Path,
    crypto: HistoryCrypto,
    name: str = "snapshot_1",
    txid: str = "tx-a52b2-0001",
) -> Path:
    """Готовый COMMITTED-снимок текущего состояния (тот же протокол, что в проде)."""
    snapshot_dir = data_dir / "backups" / name
    result = create_encrypted_snapshot(
        data_dir=data_dir,
        backup_dir=snapshot_dir,
        crypto=crypto,
        transaction_id=txid,
        policy_on=True,
    )
    assert result["state"] == STATE_COMMITTED
    return snapshot_dir


def _manifest(snapshot_dir: Path) -> dict:
    return json.loads((snapshot_dir / SNAPSHOT_MANIFEST_FILENAME).read_text("utf-8"))


def _write_manifest(snapshot_dir: Path, manifest: dict) -> None:
    (snapshot_dir / SNAPSHOT_MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def _rehash(snapshot_dir: Path) -> None:
    """Пересчитывает size/sha256 манифеста под ФАКТИЧЕСКОЕ содержимое каталога.

    Нужен, чтобы проверка целостности (size/sha256) не могла замаскировать
    проверку расшифровки: подделанный ENC1 с пересчитанным хэшем обязан быть
    пойман decrypt-check'ом, а не хэшем.
    """
    manifest = _manifest(snapshot_dir)
    files = []
    for name in HISTORY_JOURNAL_FILENAMES:
        blob = (snapshot_dir / name).read_bytes()
        files.append({"name": name, "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()})
    manifest["files"] = files
    _write_manifest(snapshot_dir, manifest)


def _read_ndjson_lines(path: Path) -> list[str]:
    text = path.read_text("utf-8")
    if text == "":
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _marker(staging_dir: Path) -> dict:
    return json.loads((staging_dir / RESTORE_MARKER_FILENAME).read_text("utf-8"))


def _real_replace(tmp_file: Path, target: Path) -> None:
    os.replace(str(tmp_file), str(target))


def _data_bytes(data_dir: Path) -> dict[str, bytes]:
    return {
        name: (data_dir / name).read_bytes()
        for name in HISTORY_JOURNAL_FILENAMES
        if (data_dir / name).is_file()
    }


def _tree_state(*roots: Path) -> dict[str, tuple[int, bytes]]:
    """Слепок дерева (путь → (mtime_ns, содержимое)) для доказательства read-only."""
    state: dict[str, tuple[int, bytes]] = {}
    for root in roots:
        root = Path(root)
        if not root.exists():
            state[str(root)] = (-1, b"")
            continue
        for path in sorted(root.rglob("*")):
            stat = path.stat()
            state[str(path)] = (stat.st_mtime_ns, b"" if path.is_dir() else path.read_bytes())
        state[str(root)] = (root.stat().st_mtime_ns, b"")
    return state


def _write_ledger(data_dir: Path, name: str, ids: list[str], crypto: HistoryCrypto) -> None:
    (data_dir / name).write_text(
        "".join(crypto.encrypt_line(json.dumps({"id": i}, ensure_ascii=False)) + "\n" for i in ids),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Фикстуры: ни один тест не должен дотянуться до Keychain.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _forbid_keychain(monkeypatch):
    """Считает и запрещает любые обращения к Keychain (b1-приём + счётчик)."""
    import backend.crypto_keystore as ks
    import backend.history_crypto as hc

    def _counted(*_a, **_k):
        KEYCHAIN_ATTEMPTS["count"] += 1
        raise AssertionError("A5.2b2 не должен обращаться к системному Keychain")

    monkeypatch.setattr(ks, "_run_security", _counted)
    monkeypatch.setattr(ks, "get_or_create_history_key", _counted)
    monkeypatch.setattr(hc, "build_history_crypto", _counted)
    yield


def test_keychain_attempts_stay_zero(tmp_path):
    """Сквозная проверка счётчика: restore/recovery ключ не создают и не читают."""
    data_dir = _data_dir(tmp_path)
    crypto = _crypto()
    _fill_mixed(data_dir, crypto)
    _settings_on(data_dir)
    _make_snapshot(data_dir, crypto)
    verify_snapshot(
        backups_root=data_dir / "backups", snapshot_dir=data_dir / "backups" / "snapshot_1", crypto=crypto
    )
    collect_ledger_union(data_dir=data_dir, crypto=crypto)
    assert KEYCHAIN_ATTEMPTS["count"] == 0


# ---------------------------------------------------------------------------
# Task 1 — верификация снимка (read-only)
# ---------------------------------------------------------------------------


class TestVerifySnapshot:
    def test_valid_snapshot_verifies_and_returns_ten_registry_files(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        snapshot_dir = _make_snapshot(data_dir, crypto)

        result = verify_snapshot(
            backups_root=data_dir / "backups", snapshot_dir=snapshot_dir, crypto=crypto
        )

        assert result["ok"] is True
        assert result["files"] == list(HISTORY_JOURNAL_FILENAMES)
        assert result["checked"] == 10
        assert result["transaction_id"] == "tx-a52b2-0001"
        assert result["state"] == STATE_COMMITTED
        # Каждая строка каждого файла расшифровывается (проверено, а не заявлено).
        assert sum(result["lines"].values()) == 30

    def test_verify_is_read_only(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        before = _tree_state(data_dir)

        verify_snapshot(backups_root=data_dir / "backups", snapshot_dir=snapshot_dir, crypto=crypto)

        assert _tree_state(data_dir) == before

    def test_unknown_manifest_version_is_refused_before_any_write(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        manifest = _manifest(snapshot_dir)
        manifest["version"] = 99
        _write_manifest(snapshot_dir, manifest)
        live_before = _data_bytes(data_dir)

        with pytest.raises(SnapshotOperationRefused) as exc:
            verify_snapshot(backups_root=data_dir / "backups", snapshot_dir=snapshot_dir, crypto=crypto)

        assert exc.value.reason == "snapshot_manifest_invalid"
        assert _data_bytes(data_dir) == live_before

    def test_missing_registry_file_is_refused(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        (snapshot_dir / "history_action_items.ndjson").unlink()
        live_before = _data_bytes(data_dir)

        with pytest.raises(SnapshotOperationRefused) as exc:
            verify_snapshot(backups_root=data_dir / "backups", snapshot_dir=snapshot_dir, crypto=crypto)

        assert exc.value.reason == "snapshot_readback_failed"
        assert _data_bytes(data_dir) == live_before

    def test_size_mismatch_is_refused(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        with (snapshot_dir / "history.ndjson").open("a", encoding="utf-8") as fh:
            fh.write(SENTINEL + "AAA\n")
        live_before = _data_bytes(data_dir)

        with pytest.raises(SnapshotOperationRefused) as exc:
            verify_snapshot(backups_root=data_dir / "backups", snapshot_dir=snapshot_dir, crypto=crypto)

        assert exc.value.reason == "snapshot_readback_failed"
        assert _data_bytes(data_dir) == live_before

    def test_sha256_mismatch_is_refused(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        target = snapshot_dir / "history_tags.ndjson"
        blob = bytearray(target.read_bytes())
        blob[-2] = blob[-2] ^ 0x01  # тот же размер, другой хэш
        target.write_bytes(bytes(blob))

        with pytest.raises(SnapshotOperationRefused) as exc:
            verify_snapshot(backups_root=data_dir / "backups", snapshot_dir=snapshot_dir, crypto=crypto)

        assert exc.value.reason == "snapshot_readback_failed"

    def test_foreign_file_in_snapshot_is_refused(self, tmp_path):
        """Подложенный рядом plaintext не должен проходить как «проверенный снимок»."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        (snapshot_dir / "заметка.txt").write_text(CANARY_PLAINTEXT, encoding="utf-8")

        with pytest.raises(SnapshotOperationRefused) as exc:
            verify_snapshot(backups_root=data_dir / "backups", snapshot_dir=snapshot_dir, crypto=crypto)

        assert exc.value.reason == "snapshot_readback_failed"

    def test_wrong_key_is_refused_without_partial_write(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        live_before = _data_bytes(data_dir)

        with pytest.raises(SnapshotOperationRefused) as exc:
            verify_snapshot(
                backups_root=data_dir / "backups",
                snapshot_dir=snapshot_dir,
                crypto=_crypto(),  # чужой ключ
            )

        assert exc.value.reason == "snapshot_line_tampered"
        assert _data_bytes(data_dir) == live_before

    def test_tampered_enc1_line_is_refused_even_with_matching_hash(self, tmp_path):
        """decrypt-check не должен маскироваться пересчитанным sha256."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        target = snapshot_dir / "history.ndjson"
        lines = _read_ndjson_lines(target)
        # Валидный вид ENC1 (SENTINEL с двоеточием сохранён), неверный GCM.
        lines[0] = SENTINEL + ("A" * (len(lines[0]) - len(SENTINEL)))
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _rehash(snapshot_dir)  # хэш теперь совпадает — ловить обязана расшифровка
        live_before = _data_bytes(data_dir)

        with pytest.raises(SnapshotOperationRefused) as exc:
            verify_snapshot(backups_root=data_dir / "backups", snapshot_dir=snapshot_dir, crypto=crypto)

        assert exc.value.reason == "snapshot_line_tampered"
        assert _data_bytes(data_dir) == live_before

    def test_plaintext_content_at_on_is_refused_as_policy_mismatch(self, tmp_path):
        """Plaintext-снимок в ON-профиле — несовместимость policy, а не «восстановление»."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        _make_snapshot(data_dir, crypto)
        snapshot_dir = data_dir / "backups" / "snapshot_plain"
        snapshot_dir.mkdir(parents=True)
        files = []
        for name in HISTORY_JOURNAL_FILENAMES:
            body = (_line(1) + "\n").encode("utf-8")
            (snapshot_dir / name).write_bytes(body)
            files.append(
                {"name": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
            )
        _write_manifest(
            snapshot_dir,
            {
                "version": SNAPSHOT_MANIFEST_VERSION,
                "transaction_id": "tx-plain",
                "state": STATE_COMMITTED,
                "policy_at_capture": True,
                "files": files,
            },
        )
        live_before = _data_bytes(data_dir)

        with pytest.raises(SnapshotOperationRefused) as exc:
            verify_snapshot(backups_root=data_dir / "backups", snapshot_dir=snapshot_dir, crypto=crypto)

        assert exc.value.reason == "snapshot_policy_mismatch"
        assert _data_bytes(data_dir) == live_before

    def test_snapshot_captured_with_policy_off_is_refused_at_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        manifest = _manifest(snapshot_dir)
        manifest["policy_at_capture"] = False
        _write_manifest(snapshot_dir, manifest)

        with pytest.raises(SnapshotOperationRefused) as exc:
            verify_snapshot(backups_root=data_dir / "backups", snapshot_dir=snapshot_dir, crypto=crypto)

        assert exc.value.reason == "snapshot_policy_mismatch"

    def test_missing_crypto_is_refused(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        snapshot_dir = _make_snapshot(data_dir, crypto)

        with pytest.raises(SnapshotOperationRefused) as exc:
            verify_snapshot(
                backups_root=data_dir / "backups", snapshot_dir=snapshot_dir, crypto=None
            )

        assert exc.value.reason == "snapshot_crypto_unavailable"

    def test_snapshot_outside_backups_root_is_refused(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        _make_snapshot(data_dir, crypto)
        outside = data_dir.parent / "не_в_backups"
        outside.mkdir()
        for name in HISTORY_JOURNAL_FILENAMES:
            (outside / name).write_text("", encoding="utf-8")

        with pytest.raises(SnapshotOperationRefused) as exc:
            verify_snapshot(backups_root=data_dir / "backups", snapshot_dir=outside, crypto=crypto)

        assert exc.value.reason == "snapshot_outside_backups_root"

    def test_dotdot_escape_into_sibling_is_refused(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        _make_snapshot(data_dir, crypto)

        with pytest.raises(SnapshotOperationRefused) as exc:
            verify_snapshot(
                backups_root=data_dir / "backups",
                snapshot_dir=data_dir / "backups" / ".." / "..",
                crypto=crypto,
            )

        assert exc.value.reason == "snapshot_outside_backups_root"

    def test_symlinked_snapshot_inside_backups_is_refused(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        real = _make_snapshot(data_dir, crypto)
        link = data_dir / "backups" / "snapshot_link"
        os.symlink(str(real), str(link))

        with pytest.raises(SnapshotOperationRefused) as exc:
            verify_snapshot(backups_root=data_dir / "backups", snapshot_dir=link, crypto=crypto)

        assert exc.value.reason == "snapshot_source_symlink"

    def test_symlink_escape_out_of_backups_is_refused(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        real = _make_snapshot(data_dir, crypto)
        outside = tmp_path / "снимок_наружу"
        os.symlink(str(real.parent.parent / ".."), str(outside))  # целевой путь не имеет значения
        evil = data_dir / "backups" / "snapshot_evil"
        os.symlink(str(outside), str(evil))

        with pytest.raises(SnapshotOperationRefused) as exc:
            verify_snapshot(
                backups_root=data_dir / "backups",
                snapshot_dir=evil,
                crypto=crypto,
            )

        # Ни лексический, ни компонентный, ни разыменованный слой не пропускают.
        assert exc.value.reason in {
            "snapshot_source_symlink",
            "snapshot_outside_backups_root",
        }

    def test_resolved_path_from_backend_is_accepted(self, tmp_path):
        """Бэкенд передаёт УЖЕ разыменованный backup_path — контейнмент не ломается.

        Симлинкнутый data_dir — легальная конфигурация b1 (backups на другом
        томе); её нельзя отвергать, иначе restore не работал бы у владельца
        с такой раскладкой.
        """
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        os.symlink(str(data_dir), str(tmp_path / "link_data"))
        via_link = tmp_path / "link_data" / "backups" / snapshot_dir.name

        result = verify_snapshot(
            backups_root=tmp_path / "link_data" / "backups",
            snapshot_dir=via_link.resolve(),  # ровно так делает handle_restore_history
            crypto=crypto,
        )

        assert result["ok"] is True

    def test_unpublished_staging_is_not_a_restorable_snapshot(self, tmp_path):
        """Ориентир — `published`: backups/.staging не является снимком."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_mixed(data_dir, crypto)
        _make_snapshot(data_dir, crypto)
        staging = data_dir / "backups" / ".staging" / ".tx-a52b2-0001"
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.mkdir()
        for name in HISTORY_JOURNAL_FILENAMES:
            (staging / name).write_text("", encoding="utf-8")
        _write_manifest(
            staging,
            {
                "version": SNAPSHOT_MANIFEST_VERSION,
                "transaction_id": "tx-a52b2-0001",
                "state": "COMMITTING",
                "policy_at_capture": True,
                "files": [
                    {"name": n, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}
                    for n in HISTORY_JOURNAL_FILENAMES
                ],
            },
        )

        with pytest.raises(SnapshotOperationRefused) as exc:
            verify_snapshot(backups_root=data_dir / "backups", snapshot_dir=staging, crypto=crypto)

        assert exc.value.reason == "snapshot_stale_staging"


# ---------------------------------------------------------------------------
# Task 1 — объединение deletion ledger (read-only, fail-closed)
# ---------------------------------------------------------------------------


class TestLedgerUnion:
    def test_union_is_tombstones_union_purged(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _write_ledger(data_dir, "history_tombstones.ndjson", ["t1", "t2"], crypto)
        _write_ledger(data_dir, "history_purged_ids.ndjson", ["p1", "t1"], crypto)

        union = collect_ledger_union(data_dir=data_dir, crypto=crypto)

        assert union == ("p1", "t1", "t2")

    def test_union_keeps_ids_from_both_kinds_of_ledger_line(self, tmp_path):
        """Смесь plaintext (legacy) и ENC1 строк в ledger — как в живом профиле."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        (data_dir / "history_tombstones.ndjson").write_text(
            json.dumps({"id": "plain-1"}, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        _write_ledger(data_dir, "history_purged_ids.ndjson", ["enc-1"], crypto)

        assert collect_ledger_union(data_dir=data_dir, crypto=crypto) == ("enc-1", "plain-1")

    def test_union_is_read_only(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _write_ledger(data_dir, "history_tombstones.ndjson", ["t1"], crypto)
        before = _tree_state(data_dir)

        collect_ledger_union(data_dir=data_dir, crypto=crypto)

        assert _tree_state(data_dir) == before

    def test_missing_ledgers_give_empty_union(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        assert collect_ledger_union(data_dir=data_dir, crypto=_crypto()) == ()

    def test_missing_crypto_is_refused(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        _write_ledger(data_dir, "history_tombstones.ndjson", ["t1"], _crypto())
        before = _tree_state(data_dir)

        with pytest.raises(SnapshotOperationRefused) as exc:
            collect_ledger_union(data_dir=data_dir, crypto=None)

        assert exc.value.reason == "snapshot_crypto_unavailable"
        assert _tree_state(data_dir) == before

    def test_undecryptable_ledger_stops_restore_without_changes(self, tmp_path):
        """Ключ недоступен/чужой → restore прекращается (спека §5)."""
        data_dir = _data_dir(tmp_path)
        _write_ledger(data_dir, "history_tombstones.ndjson", ["t1"], _crypto())
        before = _tree_state(data_dir)

        with pytest.raises(SnapshotOperationRefused) as exc:
            collect_ledger_union(data_dir=data_dir, crypto=_crypto())  # чужой ключ

        assert exc.value.reason == "snapshot_ledger_unreadable"
        assert _tree_state(data_dir) == before

    def test_malformed_ledger_line_is_refused_not_skipped(self, tmp_path):
        """Молчаливый skip malformed-строки означал бы возможный resurrection."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        (data_dir / "history_tombstones.ndjson").write_text("{не json}\n", encoding="utf-8")
        before = _tree_state(data_dir)

        with pytest.raises(SnapshotOperationRefused) as exc:
            collect_ledger_union(data_dir=data_dir, crypto=crypto)

        assert exc.value.reason == "snapshot_ledger_malformed"
        assert _tree_state(data_dir) == before

    def test_ledger_line_without_id_is_refused(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        (data_dir / "history_purged_ids.ndjson").write_text(
            json.dumps({"ts": 1}, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        with pytest.raises(SnapshotOperationRefused) as exc:
            collect_ledger_union(data_dir=data_dir, crypto=crypto)

        assert exc.value.reason == "snapshot_ledger_malformed"

    def test_symlinked_ledger_is_refused(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        real = data_dir / "настоящий_ledger.ndjson"
        real.write_text(json.dumps({"id": "x"}) + "\n", encoding="utf-8")
        os.symlink(str(real), str(data_dir / "history_tombstones.ndjson"))

        with pytest.raises(SnapshotOperationRefused) as exc:
            collect_ledger_union(data_dir=data_dir, crypto=crypto)

        assert exc.value.reason == "snapshot_source_symlink"


# ---------------------------------------------------------------------------
# Task 2 — применение снимка (commit-протокол) + restore-маркер
# ---------------------------------------------------------------------------


def _fill_profile(data_dir: Path, crypto: HistoryCrypto) -> dict[str, list[str]]:
    """Валидное содержимое всех 10 журналов (ledger-строки — только с ``id``).

    ID данных (``dN-J``) и ID deletion ledger (``lN-J``) не пересекаются, чтобы
    фильтрация по union была проверяема по отдельности.
    """
    expected: dict[str, list[str]] = {}
    for i, name in enumerate(HISTORY_JOURNAL_FILENAMES):
        plain: list[str] = []
        for j in range(3):
            if name in ("history_tombstones.ndjson", "history_purged_ids.ndjson"):
                plain.append(json.dumps({"id": f"l{i}-{j}"}, ensure_ascii=False))
            else:
                plain.append(
                    json.dumps(
                        {"id": f"d{i}-{j}", "text": f"текст-{i}-{j}"}, ensure_ascii=False
                    )
                )
        lines = [
            crypto.encrypt_line(text) if (i + j) % 2 == 0 else text
            for j, text in enumerate(plain)
        ]
        expected[name] = plain
        (data_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return expected


def _add_tombstone(data_dir: Path, item_id: str, crypto: HistoryCrypto) -> None:
    with (data_dir / "history_tombstones.ndjson").open("a", encoding="utf-8") as fh:
        fh.write(crypto.encrypt_line(json.dumps({"id": item_id}, ensure_ascii=False)) + "\n")


def _live_ids(data_dir: Path, crypto: HistoryCrypto, name: str) -> list[str]:
    """ID всех строк живого журнала (для проверки отсутствия resurrection)."""
    ids: list[str] = []
    for line in _read_ndjson_lines(data_dir / name):
        if line.startswith(SENTINEL):
            line = crypto.decrypt_line(line)
        payload = json.loads(line)
        item_id = payload.get("id")
        if item_id:
            ids.append(str(item_id))
    return ids


def _restore_markers(data_dir: Path) -> list[Path]:
    return sorted(
        p for p in data_dir.glob(f"{RESTORE_STAGING_PREFIX}*") if p.is_dir()
    )


class TestRestoreRoundTrip:
    def test_round_trip_restores_all_ten_journals_as_enc1(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        expected = _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        # Живая история после снимка — посторонняя.
        (data_dir / "history.ndjson").write_text(
            json.dumps({"id": "live", "text": "не из снимка"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        result = restore_encrypted_snapshot(
            data_dir=data_dir,
            backups_root=data_dir / "backups",
            snapshot_dir=snapshot_dir,
            crypto=crypto,
            policy_read=_policy_on(data_dir),
        )

        assert result["ok"] is True
        assert result["state"] == "COMMITTED"
        assert result["restored_entries"] == 3
        union = set(collect_ledger_union(data_dir=data_dir, crypto=crypto))
        for name in HISTORY_JOURNAL_FILENAMES:
            lines = _read_ndjson_lines(data_dir / name)
            assert all(ln.startswith(SENTINEL) for ln in lines), f"{name}: не ENC1"
            if name in ("history_tombstones.ndjson", "history_purged_ids.ndjson"):
                # Выходной ledger = ОБЪЕДИНЕНИЕ в обоих журналах: любой из них
                # сам по себе уже запрещает resurrection.
                assert set(_live_ids(data_dir, crypto, name)) == union
            else:
                # Остальные восемь журналов — побайтово то, что было в снимке.
                assert lines, f"{name}: журнал не восстановлен"
                assert [crypto.decrypt_line(ln) for ln in lines] == expected[name]

    def test_settings_json_is_never_restored(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings(data_dir, {"history_encryption_enabled": True, "язык": "ru"})
        snapshot_dir = _make_snapshot(data_dir, crypto)
        settings_before = (data_dir / "settings.json").read_bytes()

        restore_encrypted_snapshot(
            data_dir=data_dir,
            backups_root=data_dir / "backups",
            snapshot_dir=snapshot_dir,
            crypto=crypto,
            policy_read=_policy_on(data_dir),
        )

        assert (data_dir / "settings.json").read_bytes() == settings_before

    def test_restore_settings_request_is_explicit_refusal(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        live_before = _data_bytes(data_dir)
        settings_before = (data_dir / "settings.json").read_bytes()

        with pytest.raises(SnapshotOperationRefused) as exc:
            restore_encrypted_snapshot(
                data_dir=data_dir,
                backups_root=data_dir / "backups",
                snapshot_dir=snapshot_dir,
                crypto=crypto,
                restore_settings=True,
                policy_read=_policy_on(data_dir),
            )

        assert exc.value.reason == "restore_settings_unsupported_at_on"
        assert _data_bytes(data_dir) == live_before
        assert (data_dir / "settings.json").read_bytes() == settings_before

    def test_pre_restore_snapshot_is_left_and_returned(self, tmp_path):
        """Страховка для ручного решения владельца — не откат, а отдельный снимок."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        (data_dir / "history.ndjson").write_text(
            json.dumps({"id": "live-до-restore", "text": "текущее"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        result = restore_encrypted_snapshot(
            data_dir=data_dir,
            backups_root=data_dir / "backups",
            snapshot_dir=snapshot_dir,
            crypto=crypto,
            policy_read=_policy_on(data_dir),
        )

        pre = Path(result["pre_restore_snapshot"])
        assert pre.is_dir()
        assert pre != snapshot_dir
        pre_lines = _read_ndjson_lines(pre / "history.ndjson")
        assert [crypto.decrypt_line(ln) for ln in pre_lines] == [
            json.dumps({"id": "live-до-restore", "text": "текущее"}, ensure_ascii=False)
        ]
        # Страховка сама является валидным снимком (её можно восстановить).
        assert _manifest(pre)["state"] == "COMMITTED"

    def test_no_plaintext_appears_anywhere_after_restore(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)

        restore_encrypted_snapshot(
            data_dir=data_dir,
            backups_root=data_dir / "backups",
            snapshot_dir=snapshot_dir,
            crypto=crypto,
            policy_read=_policy_on(data_dir),
        )

        for root in (data_dir, data_dir / "backups"):
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                blob = path.read_bytes()
                assert b"tekst" not in blob and "текст".encode("utf-8") not in blob

    def test_transient_restore_staging_is_removed_on_success(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)

        restore_encrypted_snapshot(
            data_dir=data_dir,
            backups_root=data_dir / "backups",
            snapshot_dir=snapshot_dir,
            crypto=crypto,
            policy_read=_policy_on(data_dir),
        )

        assert _restore_markers(data_dir) == []


class TestRestoreLedgerUnion:
    def test_resurrection_is_impossible_for_tombstoned_snapshot_entry(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        # Владелец удалил запись ИЗ СНИМКА ( tombstone появился после capture).
        _add_tombstone(data_dir, "d0-1", crypto)

        result = restore_encrypted_snapshot(
            data_dir=data_dir,
            backups_root=data_dir / "backups",
            snapshot_dir=snapshot_dir,
            crypto=crypto,
            policy_read=_policy_on(data_dir),
        )

        assert "d0-1" not in _live_ids(data_dir, crypto, "history.ndjson")
        assert result["filtered_out"] >= 1
        # Ни в одном дельта-журнале записи с этим ID быть не должно.
        for name in HISTORY_JOURNAL_FILENAMES:
            if name in ("history_tombstones.ndjson", "history_purged_ids.ndjson"):
                continue
            assert "d0-1" not in _live_ids(data_dir, crypto, name), name

    def test_union_from_current_ledger_survives_snapshot_that_lacks_it(self, tmp_path):
        """ID, которого нет в снимке, но есть в текущем ledger, остаётся в ledger."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _add_tombstone(data_dir, "ghost-1", crypto)

        result = restore_encrypted_snapshot(
            data_dir=data_dir,
            backups_root=data_dir / "backups",
            snapshot_dir=snapshot_dir,
            crypto=crypto,
            policy_read=_policy_on(data_dir),
        )

        assert result["ok"] is True
        for name in ("history_tombstones.ndjson", "history_purged_ids.ndjson"):
            assert "ghost-1" in _live_ids(data_dir, crypto, name), name
        assert "ghost-1" not in _live_ids(data_dir, crypto, "history.ndjson")

    def test_ledger_union_never_shrinks(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _add_tombstone(data_dir, "d0-2", crypto)
        _add_tombstone(data_dir, "ghost-2", crypto)
        before = set(collect_ledger_union(data_dir=data_dir, crypto=crypto))

        restore_encrypted_snapshot(
            data_dir=data_dir,
            backups_root=data_dir / "backups",
            snapshot_dir=snapshot_dir,
            crypto=crypto,
            policy_read=_policy_on(data_dir),
        )

        after = set(collect_ledger_union(data_dir=data_dir, crypto=crypto))
        assert before <= after, "старый снимок уменьшил deletion ledger"
        assert {"d0-2", "ghost-2"} <= after

    def test_corrupted_current_ledger_stops_restore_without_changes(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        with (data_dir / "history_tombstones.ndjson").open("a", encoding="utf-8") as fh:
            fh.write("{не json}\n")
        live_before = _data_bytes(data_dir)

        with pytest.raises(SnapshotOperationRefused) as exc:
            restore_encrypted_snapshot(
                data_dir=data_dir,
                backups_root=data_dir / "backups",
                snapshot_dir=snapshot_dir,
                crypto=crypto,
                policy_read=_policy_on(data_dir),
            )

        assert exc.value.reason == "snapshot_ledger_malformed"
        assert _data_bytes(data_dir) == live_before
        assert _restore_markers(data_dir) == []

    def test_missing_crypto_stops_restore_without_changes(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        live_before = _data_bytes(data_dir)

        with pytest.raises(SnapshotOperationRefused) as exc:
            restore_encrypted_snapshot(
                data_dir=data_dir,
                backups_root=data_dir / "backups",
                snapshot_dir=snapshot_dir,
                crypto=None,
                policy_read=_policy_on(data_dir),
            )

        assert exc.value.reason == "snapshot_crypto_unavailable"
        assert _data_bytes(data_dir) == live_before


class TestRestorePolicyGates:
    def test_encrypted_snapshot_at_off_profile_is_refused(self, tmp_path):
        """OFF + ENC1-снимок: тихая расшифровка означала бы понижение policy."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _settings_off(data_dir)
        live_before = _data_bytes(data_dir)

        with pytest.raises(SnapshotOperationRefused) as exc:
            restore_encrypted_snapshot(
                data_dir=data_dir,
                backups_root=data_dir / "backups",
                snapshot_dir=snapshot_dir,
                crypto=crypto,
                policy_read=_policy_on(data_dir),
            )

        assert exc.value.reason == "snapshot_requires_encryption_on"
        assert _data_bytes(data_dir) == live_before

    def test_plaintext_snapshot_at_on_profile_is_refused(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        _make_snapshot(data_dir, crypto)
        plain_dir = data_dir / "backups" / "snapshot_plain"
        plain_dir.mkdir(parents=True)
        files = []
        for name in HISTORY_JOURNAL_FILENAMES:
            body = (json.dumps({"id": "p", "text": "открытый текст"}) + "\n").encode("utf-8")
            (plain_dir / name).write_bytes(body)
            files.append({"name": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        _write_manifest(
            plain_dir,
            {
                "version": SNAPSHOT_MANIFEST_VERSION,
                "transaction_id": "tx-plain",
                "state": STATE_COMMITTED,
                "policy_at_capture": True,
                "files": files,
            },
        )
        live_before = _data_bytes(data_dir)

        with pytest.raises(SnapshotOperationRefused) as exc:
            restore_encrypted_snapshot(
                data_dir=data_dir,
                backups_root=data_dir / "backups",
                snapshot_dir=plain_dir,
                crypto=crypto,
                policy_read=_policy_on(data_dir),
            )

        assert exc.value.reason == "snapshot_policy_mismatch"
        assert _data_bytes(data_dir) == live_before

    def test_policy_flipped_off_under_lock_stops_before_first_write(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        live_before = _data_bytes(data_dir)
        state = {"reads": 0}

        def _flipping_reader() -> bool:
            state["reads"] += 1
            if state["reads"] >= 2:  # вторая проверка — уже под lock
                return False
            return True

        with pytest.raises(SnapshotOperationRefused) as exc:
            restore_encrypted_snapshot(
                data_dir=data_dir,
                backups_root=data_dir / "backups",
                snapshot_dir=snapshot_dir,
                crypto=crypto,
                policy_read=_flipping_reader,
            )

        assert exc.value.reason == "snapshot_policy_unavailable"
        assert _data_bytes(data_dir) == live_before

    def test_unreadable_policy_is_fail_closed(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        live_before = _data_bytes(data_dir)

        def _boom() -> bool:
            raise OSError("settings недоступны")

        with pytest.raises(SnapshotOperationRefused) as exc:
            restore_encrypted_snapshot(
                data_dir=data_dir,
                backups_root=data_dir / "backups",
                snapshot_dir=snapshot_dir,
                crypto=crypto,
                policy_read=_boom,
            )

        assert exc.value.reason == "snapshot_policy_unavailable"
        assert _data_bytes(data_dir) == live_before


class TestRestoreCommitProtocol:
    def _restore(self, data_dir, crypto, snapshot_dir, **kw):
        return restore_encrypted_snapshot(
            data_dir=data_dir,
            backups_root=data_dir / "backups",
            snapshot_dir=snapshot_dir,
            crypto=crypto,
            policy_read=_policy_on(data_dir),
            **kw,
        )

    def test_crash_after_pre_restore_snapshot_before_marker_leaves_no_evidence(self, tmp_path):
        """Окно (a): отмена ДО COMMITTING оставляет исходные файлы без изменений."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        live_before = _data_bytes(data_dir)

        with patch(
            "backend.encrypted_snapshot._write_restore_marker",
            side_effect=OSError("synthetic crash before marker"),
        ):
            with pytest.raises(SnapshotOperationRefused):
                self._restore(data_dir, crypto, snapshot_dir)

        assert _data_bytes(data_dir) == live_before
        # Незавершённой транзакции на диске нет — отменено, а не «зависло».
        assert _restore_markers(data_dir) == []
        # Снимок-страховка успел опубликоваться (шаг до маркера) и остаётся доказательством.
        pre = [p for p in (data_dir / "backups").iterdir() if "prerestore" in p.name]
        assert len(pre) == 1

    def test_crash_at_first_replacement_keeps_committing_marker(self, tmp_path):
        """Окно (b): COMMITTING переживает crash, живая история цела, отката нет."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        live_before = _data_bytes(data_dir)

        with patch(
            "backend.encrypted_snapshot._replace_journal",
            side_effect=OSError("synthetic crash on first replacement"),
        ):
            with pytest.raises(SnapshotOperationRefused) as exc:
                self._restore(data_dir, crypto, snapshot_dir)

        assert exc.value.pending is True
        assert _data_bytes(data_dir) == live_before
        markers = _restore_markers(data_dir)
        assert len(markers) == 1
        assert _marker(markers[0])["state"] == "COMMITTING"

    def test_crash_midway_leaves_mixed_journals_and_surviving_marker(self, tmp_path):
        """Окно (b'): половина замен выполнена — состояние не объявляется успехом."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        live_before = _data_bytes(data_dir)
        calls = {"n": 0}

        def _fifth_fails(tmp_path_arg, target):
            calls["n"] += 1
            if calls["n"] == 5:
                raise OSError("synthetic crash on fifth replacement")
            return _real_replace(tmp_path_arg, target)

        with patch("backend.encrypted_snapshot._replace_journal", _fifth_fails):
            with pytest.raises(SnapshotOperationRefused) as exc:
                self._restore(data_dir, crypto, snapshot_dir)

        assert exc.value.pending is True
        assert calls["n"] == 5
        assert _data_bytes(data_dir) != live_before  # набор уже смешанный
        markers = _restore_markers(data_dir)
        assert len(markers) == 1
        assert _marker(markers[0])["state"] == "COMMITTING"

    def test_crash_before_readback_leaves_marker_not_committed(self, tmp_path):
        """Окно (c): замены сделаны, но COMMITTED недостижим без read-back."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)

        with patch(
            "backend.encrypted_snapshot._readback_live_journals",
            side_effect=OSError("synthetic crash before readback"),
        ):
            with pytest.raises(SnapshotOperationRefused) as exc:
                self._restore(data_dir, crypto, snapshot_dir)

        assert exc.value.pending is True
        markers = _restore_markers(data_dir)
        assert len(markers) == 1
        assert _marker(markers[0])["state"] == "COMMITTING"
        # Никакой маркер COMMITTED на диске.
        assert all(_marker(m)["state"] != "COMMITTED" for m in markers)

    def test_two_restores_in_a_row_keep_their_own_transaction_ids(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        first = _make_snapshot(data_dir, crypto, name="snapshot_1", txid="tx-первый")
        first_result = self._restore(data_dir, crypto, first)
        second = _make_snapshot(data_dir, crypto, name="snapshot_2", txid="tx-второй")
        second_result = self._restore(data_dir, crypto, second)

        assert first_result["transaction_id"] != second_result["transaction_id"]
        assert Path(first_result["pre_restore_snapshot"]) != Path(
            second_result["pre_restore_snapshot"]
        )
        assert _manifest(first)["transaction_id"] == "tx-первый"
        assert _manifest(second)["transaction_id"] == "tx-второй"

    def test_second_restore_blocked_while_marker_survives(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        with patch(
            "backend.encrypted_snapshot._replace_journal",
            side_effect=OSError("synthetic crash"),
        ):
            with pytest.raises(SnapshotOperationRefused):
                self._restore(data_dir, crypto, snapshot_dir)
        assert _restore_markers(data_dir)

        with pytest.raises(SnapshotOperationRefused) as exc:
            self._restore(data_dir, crypto, snapshot_dir)

        assert exc.value.reason == "snapshot_recovery_pending"
        assert exc.value.pending is True
        assert len(_restore_markers(data_dir)) == 1


# ---------------------------------------------------------------------------
# Task 3 — fail-closed recovery с докачкой + ленивый wiring
# ---------------------------------------------------------------------------


def _crash_restore(data_dir: Path, crypto: HistoryCrypto, snapshot_dir: Path, *, at: str):
    """Роняет restore в нужном crash-окне и возвращает живой маркер."""
    if at == "marker":
        target = "backend.encrypted_snapshot._write_restore_marker"
        side_effect = OSError("synthetic crash at marker")
    elif at == "replace":
        target = "backend.encrypted_snapshot._replace_journal"
        side_effect = OSError("synthetic crash at replacement")
    else:
        raise ValueError(at)
    with patch(target, side_effect=side_effect):
        with pytest.raises(SnapshotOperationRefused):
            restore_encrypted_snapshot(
                data_dir=data_dir,
                backups_root=data_dir / "backups",
                snapshot_dir=snapshot_dir,
                crypto=crypto,
                policy_read=_policy_on(data_dir),
            )


def _recover(data_dir: Path, crypto: HistoryCrypto) -> dict:
    return recover_pending_restore(
        data_dir=data_dir,
        backups_root=data_dir / "backups",
        crypto=crypto,
        policy_read=_policy_on(data_dir),
    )


class TestRecoveryRollsForward:
    def test_committing_restore_is_rolled_forward_to_committed(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        expected = _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_restore(data_dir, crypto, snapshot_dir, at="replace")
        assert len(_restore_markers(data_dir)) == 1

        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert result["rolled_forward"] is True
        assert result["state"] == "COMMITTED"
        assert result["restored_entries"] == 3
        # Доказан результат, а не «оставлено как было».
        for name in HISTORY_JOURNAL_FILENAMES:
            lines = _read_ndjson_lines(data_dir / name)
            assert lines, name
        assert [crypto.decrypt_line(ln) for ln in _read_ndjson_lines(data_dir / "history.ndjson")] == [
            expected["history.ndjson"][0],
            expected["history.ndjson"][1],
            expected["history.ndjson"][2],
        ]
        assert _restore_markers(data_dir) == []

    def test_recovery_reverifies_target_before_applying(self, tmp_path):
        """Снимок, испорченный ПОСЛЕ crash, не докатывается молча."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_restore(data_dir, crypto, snapshot_dir, at="replace")
        target = snapshot_dir / "history.ndjson"
        target.write_bytes(target.read_bytes() + b"ENC1:AAAA\n")

        result = _recover(data_dir, crypto)

        assert result["ok"] is False
        assert result["pending"] is True
        assert result["reason"] in {
            "snapshot_readback_failed",
            "snapshot_line_tampered",
        }
        assert result["pre_restore_snapshot"] is not None
        # Ничего не удалено и не заменено «как есть».
        assert len(_restore_markers(data_dir)) == 1

    def test_recovery_refuses_rollforward_at_off_policy(self, tmp_path):
        """Fail-closed: никакого отката в plaintext, никакого нового ключа."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_restore(data_dir, crypto, snapshot_dir, at="replace")
        live_before = _data_bytes(data_dir)
        _settings_off(data_dir)

        result = _recover(data_dir, crypto)

        assert result["ok"] is False
        assert result["pending"] is True
        assert result["reason"] == "snapshot_requires_encryption_on"
        assert _data_bytes(data_dir) == live_before
        assert len(_restore_markers(data_dir)) == 1
        assert KEYCHAIN_ATTEMPTS["count"] == 0

    def test_recovery_without_key_refuses_and_creates_none(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_restore(data_dir, crypto, snapshot_dir, at="replace")
        live_before = _data_bytes(data_dir)

        result = _recover(data_dir, None)

        assert result["ok"] is False
        assert result["pending"] is True
        assert result["reason"] == "snapshot_crypto_unavailable"
        assert _data_bytes(data_dir) == live_before
        assert KEYCHAIN_ATTEMPTS["count"] == 0

    def test_recovery_reports_pre_restore_path_for_manual_decision(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_restore(data_dir, crypto, snapshot_dir, at="replace")
        marker = _marker(_restore_markers(data_dir)[0])

        result = _recover(data_dir, crypto)

        assert result["pre_restore_snapshot"] == marker["pre_restore_snapshot"]
        assert Path(result["pre_restore_snapshot"]).is_dir()

    def test_committed_marker_left_by_crash_is_only_cleaned(self, tmp_path):
        """COMMITTED на диске = транзакция уже завершена: убрать, не переделывать."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        # Доводим до успеха, затем возвращаем каталог staging на место — как если бы
        # процесс умер между записью COMMITTED и уборкой staging.
        restore_encrypted_snapshot(
            data_dir=data_dir,
            backups_root=data_dir / "backups",
            snapshot_dir=snapshot_dir,
            crypto=crypto,
            policy_read=_policy_on(data_dir),
        )
        live_after_restore = _data_bytes(data_dir)
        leftover = data_dir / f"{RESTORE_STAGING_PREFIX}tx-проигранный"
        leftover.mkdir()
        (leftover / RESTORE_MARKER_FILENAME).write_text(
            json.dumps(
                {
                    "version": 1,
                    "transaction_id": "tx-проигранный",
                    "state": "COMMITTED",
                    "target_snapshot": str(snapshot_dir),
                    "pre_restore_snapshot": str(snapshot_dir),
                    "files": [],
                }
            ),
            encoding="utf-8",
        )

        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert result["rolled_forward"] is False
        assert result["state"] == "COMMITTED"
        assert result["pending"] is False
        assert _restore_markers(data_dir) == []
        assert _data_bytes(data_dir) == live_after_restore

    def test_recovery_of_midway_crash_completes_mixed_set(self, tmp_path):
        """Половина замен + докачка = связный полный набор, а не смесь."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        expected = _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        calls = {"n": 0}

        def _fifth_fails(tmp_arg, target):
            calls["n"] += 1
            if calls["n"] == 5:
                raise OSError("synthetic crash on fifth replacement")
            return _real_replace(tmp_arg, target)

        with patch("backend.encrypted_snapshot._replace_journal", _fifth_fails):
            with pytest.raises(SnapshotOperationRefused):
                restore_encrypted_snapshot(
                    data_dir=data_dir,
                    backups_root=data_dir / "backups",
                    snapshot_dir=snapshot_dir,
                    crypto=crypto,
                    policy_read=_policy_on(data_dir),
                )

        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert result["rolled_forward"] is True
        for name in HISTORY_JOURNAL_FILENAMES:
            if name in ("history_tombstones.ndjson", "history_purged_ids.ndjson"):
                continue
            got = [crypto.decrypt_line(ln) for ln in _read_ndjson_lines(data_dir / name)]
            assert got == expected[name], name


class TestRecoveryNoOpAndStaleStaging:
    def test_no_marker_is_cheap_noop(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        before = _tree_state(data_dir)

        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert result["pending"] is False
        assert result["reason"] is None
        assert result["rolled_forward"] is False
        assert result["state"] is None
        assert _tree_state(data_dir) == before

    def test_unpublished_snapshot_staging_is_musor_not_pending(self, tmp_path):
        """Ориентир — `published`: staging без публикации не трогаем и не чистим."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        _make_snapshot(data_dir, crypto)
        staging = data_dir / "backups" / ".staging" / ".tx-мусор"
        staging.mkdir(parents=True)
        (staging / "history.ndjson").write_text("", encoding="utf-8")
        (staging / "snapshot_manifest.json").write_text(
            json.dumps({"version": 1, "state": "COMMITTING"}), encoding="utf-8"
        )

        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert result["pending"] is False
        assert result["reason"] == "snapshot_stale_staging"
        # Доказательство остаётся владельцу.
        assert staging.is_dir()

    def test_published_snapshot_committing_stays_visible_as_pending(self, tmp_path):
        """b1-транзакция (backup) не докатывается под видом restore и не прячется."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        manifest = _manifest(snapshot_dir)
        manifest["state"] = "COMMITTING"
        _write_manifest(snapshot_dir, manifest)

        result = _recover(data_dir, crypto)

        assert result["ok"] is False
        assert result["pending"] is True
        assert result["reason"] == "snapshot_recovery_pending"
        assert result["rolled_forward"] is False
        # Живая история не тронута: backup-COMMITTING не превращается в restore.
        assert len(_read_ndjson_lines(data_dir / "history.ndjson")) == 3

    def test_has_pending_restore_is_cheap_and_false_without_marker(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        _fill_profile(data_dir, _crypto())
        assert has_pending_restore(data_dir) is False
        leftover = data_dir / f"{RESTORE_STAGING_PREFIX}tx-1"
        leftover.mkdir()
        (leftover / RESTORE_MARKER_FILENAME).write_text("{}", encoding="utf-8")
        assert has_pending_restore(data_dir) is True


class TestRecoveryWiring:
    def test_state_store_init_recovers_only_when_marker_exists(self, tmp_path, monkeypatch):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_restore(data_dir, crypto, snapshot_dir, at="replace")
        assert _restore_markers(data_dir)
        # Ключ подставляется ДО конструктора: recovery работает внутри __init__.
        monkeypatch.setattr(StateStore, "_get_history_crypto", lambda self: crypto)

        store = StateStore(data_dir)  # конструктор обязан докатить маркер

        assert store.restore_recovery["rolled_forward"] is True
        assert store.restore_recovery["state"] == "COMMITTED"
        assert KEYCHAIN_ATTEMPTS["count"] == 0
        assert _restore_markers(data_dir) == []
        assert has_pending_restore(data_dir) is False
        lines = _read_ndjson_lines(data_dir / "history.ndjson")
        assert lines and all(ln.startswith(SENTINEL) for ln in lines)

    def test_state_store_init_without_marker_does_nothing(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        _make_snapshot(data_dir, crypto)
        before = _data_bytes(data_dir)
        settings_before = (data_dir / "settings.json").read_bytes()

        StateStore(data_dir)

        assert _data_bytes(data_dir) == before
        assert (data_dir / "settings.json").read_bytes() == settings_before
        assert _restore_markers(data_dir) == []

    def test_off_profile_start_with_stray_marker_does_not_decrypt(self, tmp_path):
        """OFF-профиль (прод) + случайный каталог: никаких обращений к ключу."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_off(data_dir)
        _make_snapshot(data_dir, crypto)
        live_before = _data_bytes(data_dir)
        leftover = data_dir / f"{RESTORE_STAGING_PREFIX}tx-мусор"
        leftover.mkdir()
        (leftover / RESTORE_MARKER_FILENAME).write_text("{}", encoding="utf-8")

        StateStore(data_dir)

        assert _data_bytes(data_dir) == live_before
        assert leftover.is_dir()  # доказательство не удалено
        assert KEYCHAIN_ATTEMPTS["count"] == 0


# ---------------------------------------------------------------------------
# Task 4 — IPC-поверхность (handle_restore_history при Encryption ON)
# ---------------------------------------------------------------------------


def _store_with_crypto(data_dir: Path, crypto) -> StateStore:
    store = StateStore(data_dir)
    # Инъекция ключа БЕЗ Keychain (приём b1/A5.1).
    store._get_history_crypto = lambda: crypto
    return store


def _svc(data_dir: Path, crypto) -> HistoryService:
    return HistoryService(store=_store_with_crypto(data_dir, crypto), cached_settings=lambda: {})


def _seed_items(store, texts: list[str]) -> None:
    for text in texts:
        store.add_history_item(text=text)


class TestRestoreIpc:
    def test_on_profile_restores_encrypted_snapshot(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed_items(store, ["первая", "вторая", "третья"])
        svc = HistoryService(store=store, cached_settings=lambda: {})
        made = svc.handle_backup_history({})
        assert made["ok"] is True
        snapshot_dir = Path(made["backup_path"])
        # Профиль уехал вперёд: лишняя запись, которой не было в снимке.
        _seed_items(store, ["четвёртая"])
        settings_before = (data_dir / "settings.json").read_bytes()

        result = svc.handle_restore_history({"backup_path": str(snapshot_dir)})

        assert result["ok"] is True
        assert result["reason"] is None
        assert result["encrypted"] is True
        assert result["state"] == "COMMITTED"
        assert result["restored_entries"] == 3
        assert Path(result["pre_restore_snapshot"]).is_dir()
        assert result["transaction_id"]
        # settings.json не восстановлен — ни при каких обстоятельствах.
        assert (data_dir / "settings.json").read_bytes() == settings_before
        # Профиль после restore ровно из снимка.
        assert store.count_active_items() == 3
        texts = {item.text for item in store._load_active_items_unlocked()}
        assert texts == {"первая", "вторая", "третья"}
        # Всё на диске — ENC1 (в т.ч. новая запись «четвёртая» исчезла).
        for name in HISTORY_JOURNAL_FILENAMES:
            for line in _read_ndjson_lines(data_dir / name):
                assert line.startswith(SENTINEL), name

    def test_on_profile_refuses_wrong_key_before_any_write(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        _seed_items(_store_with_crypto(data_dir, crypto), ["запись"])
        svc = _svc(data_dir, crypto)
        snapshot_dir = Path(svc.handle_backup_history({})["backup_path"])
        live_before = _data_bytes(data_dir)

        wrong = _svc(data_dir, _crypto())  # другой ключ
        result = wrong.handle_restore_history({"backup_path": str(snapshot_dir)})

        assert result["ok"] is False
        assert result["reason"] == "snapshot_line_tampered"
        assert result["restored_entries"] == 0
        assert _data_bytes(data_dir) == live_before

    def test_on_profile_refuses_broken_manifest_before_any_write(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        _seed_items(_store_with_crypto(data_dir, crypto), ["запись"])
        svc = _svc(data_dir, crypto)
        snapshot_dir = Path(svc.handle_backup_history({})["backup_path"])
        live_before = _data_bytes(data_dir)
        target = snapshot_dir / "history.ndjson"
        target.write_bytes(target.read_bytes() + b"ENC1:zzz\n")

        result = svc.handle_restore_history({"backup_path": str(snapshot_dir)})

        assert result["ok"] is False
        assert result["reason"] == "snapshot_readback_failed"
        assert _data_bytes(data_dir) == live_before

    def test_on_profile_refuses_restore_settings_request(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        _seed_items(_store_with_crypto(data_dir, crypto), ["запись"])
        svc = _svc(data_dir, crypto)
        snapshot_dir = Path(svc.handle_backup_history({})["backup_path"])
        live_before = _data_bytes(data_dir)
        settings_before = (data_dir / "settings.json").read_bytes()

        result = svc.handle_restore_history(
            {"backup_path": str(snapshot_dir), "restore_settings": True}
        )

        assert result["ok"] is False
        assert result["reason"] == "restore_settings_unsupported_at_on"
        assert _data_bytes(data_dir) == live_before
        assert (data_dir / "settings.json").read_bytes() == settings_before

    def test_on_profile_keeps_a52a_gate_for_legacy_backup(self, tmp_path):
        """A5.2a-гейт не ослаблен: legacy-копия при ON по-прежнему недоступна."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_off(data_dir)
        legacy = data_dir / "backups" / "backup_20260101_000000"
        legacy.mkdir(parents=True)
        (legacy / "history.ndjson").write_text(
            json.dumps({"id": "x", "text": "легаси"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _settings_on(data_dir)
        svc = _svc(data_dir, crypto)
        live_before = _data_bytes(data_dir)

        result = svc.handle_restore_history({"backup_path": str(legacy)})

        assert result["ok"] is False
        assert result["reason"] == "history_encryption_operation_unavailable"
        assert _data_bytes(data_dir) == live_before

    def test_on_profile_refuses_staging_and_foreign_dirs(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        _make_snapshot(data_dir, crypto)
        staging = data_dir / "backups" / ".staging" / ".tx-мусор"
        staging.mkdir(parents=True)
        foreign = data_dir / "backups" / "my_folder"
        foreign.mkdir(parents=True)
        svc = _svc(data_dir, crypto)

        for path in (staging, foreign):
            result = svc.handle_restore_history({"backup_path": str(path)})
            assert result["ok"] is False, path
            assert result["reason"] == "unsupported_backup_format", path

    def test_on_profile_rejects_path_outside_backups(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        svc = _svc(data_dir, crypto)
        evil = tmp_path / "evil"
        evil.mkdir()
        (evil / "history.ndjson").write_text("{}\n", encoding="utf-8")

        with pytest.raises(RuntimeError):
            svc.handle_restore_history({"backup_path": str(evil)})

    def test_restore_result_reports_snapshot_backup_date(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        _seed_items(_store_with_crypto(data_dir, crypto), ["запись"])
        svc = _svc(data_dir, crypto)
        snapshot_dir = Path(svc.handle_backup_history({})["backup_path"])

        result = svc.handle_restore_history({"backup_path": str(snapshot_dir)})

        assert result["ok"] is True
        assert result["backup_date"] != "unknown"
        assert result["backup_date"] == _manifest(snapshot_dir)["created_at"]

    def test_restored_count_is_honest_after_search_cache_was_warm(self, tmp_path):
        """In-RAM индексы StateStore не имеют права врать после замены журналов."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed_items(store, ["одна", "две"])
        svc = HistoryService(store=store, cached_settings=lambda: {})
        snapshot_dir = Path(svc.handle_backup_history({})["backup_path"])
        _seed_items(store, ["три", "четыре", "пять"])
        assert store.count_active_items() == 5  # кэш прогрет

        result = svc.handle_restore_history({"backup_path": str(snapshot_dir)})

        assert result["ok"] is True
        assert result["restored_entries"] == 2
        assert store.count_active_items() == 2

    def test_off_profile_legacy_restore_still_works(self, tmp_path):
        """OFF-регресс: прежнее поведение legacy restore не изменилось."""
        data_dir = _data_dir(tmp_path)
        _settings_off(data_dir)
        store = _store_with_crypto(data_dir, None)
        svc = HistoryService(store=store, cached_settings=lambda: {})
        _seed_items(store, ["one", "two"])
        made = svc.handle_backup_history({})
        (data_dir / "history.ndjson").write_text("", encoding="utf-8")

        result = svc.handle_restore_history({"backup_path": made["backup_path"]})

        assert result.get("ok") is not False
        assert result["restored_entries"] == 2
        assert "one" in (data_dir / "history.ndjson").read_text("utf-8")


class TestPurgeCoversRestoreStaging:
    def test_purge_all_data_removes_pending_restore_staging(self, tmp_path):
        """Приватный staging restore — тоже новое хранилище: purge обязан его убрать."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_restore(data_dir, crypto, snapshot_dir, at="replace")
        markers = _restore_markers(data_dir)
        assert len(markers) == 1
        svc = _svc(data_dir, crypto)

        result = svc.handle_purge_all_data({"confirm": True})

        assert "restore_staging" not in (result.get("errors") or [])
        assert not markers[0].exists()
        assert has_pending_restore(data_dir) is False
        # Данные действительно стёрты, а staging не остался источником утечки.
        assert (data_dir / "history.ndjson").read_bytes() == b""

    def test_purge_helper_is_noop_without_markers(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        _fill_profile(data_dir, _crypto())
        before = _data_bytes(data_dir)
        assert purge_pending_restore_staging(data_dir) == []
        assert _data_bytes(data_dir) == before
