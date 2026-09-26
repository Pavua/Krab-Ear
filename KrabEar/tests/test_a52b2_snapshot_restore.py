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

import pytest

from backend.encrypted_snapshot import (
    SNAPSHOT_MANIFEST_FILENAME,
    SNAPSHOT_MANIFEST_VERSION,
    STATE_COMMITTED,
    SnapshotOperationRefused,
    collect_ledger_union,
    create_encrypted_snapshot,
    verify_snapshot,
)
from backend.history_crypto import SENTINEL, HistoryCrypto
from backend.state_store import HISTORY_JOURNAL_FILENAMES

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
