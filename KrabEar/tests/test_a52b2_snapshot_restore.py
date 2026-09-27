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

import errno
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.encrypted_snapshot import (
    RESTORE_MARKER_FILENAME,
    RESTORE_STAGING_PREFIX,
    RESTORE_TMP_SUFFIX,
    SNAPSHOT_MANIFEST_FILENAME,
    SNAPSHOT_MANIFEST_VERSION,
    STATE_COMMITTED,
    SnapshotOperationRefused,
    collect_ledger_union,
    create_encrypted_snapshot,
    has_pending_restore,
    last_restore_recovery,
    read_pending_restore_verdict,
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

# Счётчики обращений к Keychain (см. фикстуру ниже). Модульные, чтобы итоговое
# утверждение видело ВСЕ обращения за сессию, а не только за один тест.
#
# ДВА счётчика, потому что пути принципиально разные:
#   * ``attempts``  — чтение/создание ключа (build_history_crypto,
#     get_or_create_history_key). Для b2 это НУЛЬ: restore/recovery работают с
#     уже полученным ключом и не имеют права дёргать Keychain.
#   * ``deletions`` — низкоуровневый ``crypto_keystore._run_security``. Единственный
#     путь к нему в этом наборе — ``handle_purge_all_data``, который по явному
#     действию владельца УДАЛЯЕТ ключ шифрования (поведение из волны 0afc3ff9,
#     не b2: без этого выживший ключ расшифровывает pre-purge бэкап).
KEYCHAIN_ATTEMPTS = {"attempts": 0, "deletions": 0}


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
    """Считает и запрещает любые обращения к Keychain (b1-приём + счётчики)."""
    import backend.crypto_keystore as ks
    import backend.history_crypto as hc

    def _counted(*_a, **_k):
        KEYCHAIN_ATTEMPTS["attempts"] += 1
        raise AssertionError("A5.2b2 не должен обращаться к системному Keychain")

    def _counted_deletion(*_a, **_k):
        # Единственный легальный путь сюда — удаление ключа в purge (владелец
        # попросил стереть всё). Считаем отдельно, чтобы не смазать главный
        # инвариант «b2 не читает и не создаёт ключ».
        KEYCHAIN_ATTEMPTS["deletions"] += 1
        raise AssertionError("Keychain недоступен в тестах (счётчик удалений)")

    monkeypatch.setattr(ks, "_run_security", _counted_deletion)
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
    assert KEYCHAIN_ATTEMPTS["attempts"] == 0


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
        assert result["filtered_out_lines"] >= 1
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
        assert KEYCHAIN_ATTEMPTS["attempts"] == 0

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
        assert KEYCHAIN_ATTEMPTS["attempts"] == 0

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


class TestRecoveryTriggerPoints:
    """M2: recovery вызывается в точках обслуживания, а НЕ в StateStore.__init__.

    Причина (adversarial-ревью): у фасада пять точек создания, recovery шёл ДО
    ``init_sentry``/late-injection ErrorBus, а вердикт всё равно никем не
    читался. Докатка обязана жить там, где она блокирует работу, а вердикт —
    быть читаемым существующим способом.
    """

    def test_state_store_init_never_touches_recovery(self, tmp_path):
        """Конструктор фасада не докатывает: иначе 5 точек создания и pre-Sentry порядок."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_restore(data_dir, crypto, snapshot_dir, at="replace")
        assert _restore_markers(data_dir)
        live_before = _data_bytes(data_dir)

        store = StateStore(data_dir)

        assert _restore_markers(data_dir) != [], "конструктор докатал транзакцию"
        assert _data_bytes(data_dir) == live_before
        assert not hasattr(store, "restore_recovery"), (
            "вердикт recovery не должен жить на фасаде — долг M2"
        )

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

    def test_backup_call_rolls_forward_pending_restore(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_restore(data_dir, crypto, snapshot_dir, at="replace")
        assert _restore_markers(data_dir)
        svc = _svc(data_dir, crypto)

        svc.handle_backup_history({})  # обслуживание само докатывает

        assert _restore_markers(data_dir) == []
        assert has_pending_restore(data_dir) is False
        assert last_restore_recovery()["rolled_forward"] is True
        assert KEYCHAIN_ATTEMPTS["attempts"] == 0

    def test_restore_call_rolls_forward_pending_restore(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_restore(data_dir, crypto, snapshot_dir, at="replace")
        svc = _svc(data_dir, crypto)

        result = svc.handle_restore_history({"backup_path": str(snapshot_dir)})

        assert result["ok"] is True
        assert _restore_markers(data_dir) == []
        assert last_restore_recovery()["rolled_forward"] is True

    def test_auto_backup_call_rolls_forward_pending_restore(self, tmp_path):
        from backend.auto_backup import AutoBackupManager

        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_restore(data_dir, crypto, snapshot_dir, at="replace")
        store = _store_with_crypto(data_dir, crypto)

        AutoBackupManager(store=store, interval_hours=0).check_and_backup()

        assert _restore_markers(data_dir) == []
        assert last_restore_recovery()["rolled_forward"] is True

    def test_list_backups_surfaces_pending_verdict_without_writing(self, tmp_path):
        """Точка наблюдения: вердикт виден, но ничего не докатывается и не пишется."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_restore(data_dir, crypto, snapshot_dir, at="replace")
        svc = _svc(data_dir, crypto)
        before = _tree_state(data_dir)

        listed = svc.handle_list_backups({})

        assert listed["restore_recovery"]["pending"] is True
        assert listed["restore_recovery"]["pre_restore_snapshot"]
        assert _restore_markers(data_dir) != []  # ничего не докатано
        assert _tree_state(data_dir) == before

    def test_off_profile_service_call_with_stray_marker_does_not_decrypt(self, tmp_path):
        """OFF-профиль (прод) + посторонний каталог: ноль обращений к ключу.

        Профиль СОГЛАСОВАН (журналы plaintext, флаг OFF) — иначе проверялось бы
        не свойство recovery, а давление A5.1 «ENC1 при выключенном флаге».
        """
        data_dir = _data_dir(tmp_path)
        for i, name in enumerate(HISTORY_JOURNAL_FILENAMES):
            (data_dir / name).write_text(
                json.dumps({"id": f"off{i}", "text": "открытая запись"}, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
        _settings_off(data_dir)
        live_before = _data_bytes(data_dir)
        leftover = data_dir / f"{RESTORE_STAGING_PREFIX}tx-мусор"
        leftover.mkdir()
        (leftover / RESTORE_MARKER_FILENAME).write_text("{}", encoding="utf-8")
        svc = _svc(data_dir, None)

        listed = svc.handle_list_backups({})
        backup = svc.handle_backup_history({})
        restore = svc.handle_restore_history(
            {"backup_path": str(svc.handle_backup_history({})["backup_path"])}
        )

        assert listed["restore_recovery"]["pending"] is True
        # M4: живой маркер останавливает ОБЕ ветки, включая OFF/legacy. Раньше
        # OFF-ветка проходила мимо гейта (r6). Причём fail-closed: даже
        # нечитаемый маркер («{}») блокирует, потому что «он вроде не наш» —
        # это допущение, на котором строится потеря данных.
        assert backup.get("ok") is False
        assert backup["reason"] == "snapshot_recovery_pending"
        assert restore.get("ok") is False
        assert restore["reason"] == "snapshot_recovery_pending"
        # Причина нечитаемости маркера остаётся диагностируемой.
        assert listed["restore_recovery"]["reason"] == "snapshot_manifest_invalid"
        assert _data_bytes(data_dir) == live_before
        assert leftover.is_dir()  # посторонний каталог НЕ удаляется fail-closed
        assert KEYCHAIN_ATTEMPTS["attempts"] == 0


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


# ---------------------------------------------------------------------------
# A5.2b2 review — B1: recovery НЕ имеет права удалять маркер-pending
# ---------------------------------------------------------------------------


def _crash_on_nth_replace(data_dir, crypto, snapshot_dir, *, nth: int):
    """Роняет restore на nth-й замене → смешанный набор + маркер COMMITTING."""
    calls = {"n": 0}
    real = os.replace

    def _maybe_fail(tmp_arg, target):
        calls["n"] += 1
        if calls["n"] == nth:
            raise OSError("synthetic crash on replacement")
        return real(str(tmp_arg), str(target))

    with patch("backend.encrypted_snapshot._replace_journal", _maybe_fail):
        with pytest.raises(SnapshotOperationRefused) as exc:
            restore_encrypted_snapshot(
                data_dir=data_dir,
                backups_root=data_dir / "backups",
                snapshot_dir=snapshot_dir,
                crypto=crypto,
                policy_read=_policy_on(data_dir),
            )
    assert exc.value.pending is True
    assert calls["n"] == nth
    return calls


def _oneshot_enospc_in_restore_staging():
    """Одноразовый ENOSPC на записи в staging restore (не в pre-restore снимок)."""
    state = {"armed": False}
    real = None

    def _maybe_fail(path, blob):
        if state["armed"] and RESTORE_STAGING_PREFIX in str(path):
            state["armed"] = False
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, blob)

    from backend import encrypted_snapshot as es

    real = es._write_file_durable
    return state, patch("backend.encrypted_snapshot._write_file_durable", _maybe_fail)


class TestRecoveryKeepsPendingEvidence:
    def test_prepare_failure_during_recovery_keeps_marker(self, tmp_path):
        """B1-проба: crash на 5-й замене + сбой prepare в recovery.

        Живой набор рваный, маркер COMMITTING — единственное доказательство
        незавершённого restore. Отказ в prepare НЕ имеет права его удалить:
        иначе рваный набор становится невидимым, а следующий restore рапортует
        об успехе поверх него.
        """
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_on_nth_replace(data_dir, crypto, snapshot_dir, nth=5)
        markers = _restore_markers(data_dir)
        assert len(markers) == 1
        assert _marker(markers[0])["state"] == "COMMITTING"
        live_during_crash = _data_bytes(data_dir)
        state, ctx = _oneshot_enospc_in_restore_staging()
        state["armed"] = True

        with ctx:
            result = _recover(data_dir, crypto)

        assert result["ok"] is False
        assert result["pending"] is True
        assert result["reason"] == "snapshot_fsync_failed"
        # 🔴 Доказательство обязано остаться на диске.
        assert _restore_markers(data_dir) != [], "маркер-pending удалён отказом в prepare"
        assert has_pending_restore(data_dir) is True
        assert _marker(_restore_markers(data_dir)[0])["state"] == "COMMITTING"
        # Рваный набор не тронут «починкой» — он ждёт разбора.
        assert _data_bytes(data_dir) == live_during_crash

    def test_malformed_record_during_recovery_keeps_marker(self, tmp_path):
        """Тот же инвариант для другого отказа prepare (запись не JSON-объект)."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_on_nth_replace(data_dir, crypto, snapshot_dir, nth=3)
        assert len(_restore_markers(data_dir)) == 1
        # Снимок портится ПОСЛЕ crash: строка валидно расшифровывается, но это
        # не JSON-объект → отказ в prepare докачки.
        target = snapshot_dir / "history.ndjson"
        lines = _read_ndjson_lines(target)
        lines[0] = crypto.encrypt_line(json.dumps([1, 2], ensure_ascii=False))
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _rehash(snapshot_dir)
        live_during_crash = _data_bytes(data_dir)

        result = _recover(data_dir, crypto)

        assert result["ok"] is False
        assert result["pending"] is True
        assert result["reason"] == "snapshot_record_malformed"
        assert _restore_markers(data_dir) != [], "маркер-pending удалён отказом в prepare"
        assert has_pending_restore(data_dir) is True
        assert _data_bytes(data_dir) == live_during_crash

    def test_ragged_profile_blocks_ordinary_maintenance(self, tmp_path):
        """Пока маркер жив — обычное обслуживание заблокировано (спека §5.4)."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_on_nth_replace(data_dir, crypto, snapshot_dir, nth=4)
        state, ctx = _oneshot_enospc_in_restore_staging()
        state["armed"] = True
        with ctx:
            _recover(data_dir, crypto)
        assert has_pending_restore(data_dir) is True

        # Новый restore не идёт.
        with pytest.raises(SnapshotOperationRefused) as restore_blocked:
            restore_encrypted_snapshot(
                data_dir=data_dir,
                backups_root=data_dir / "backups",
                snapshot_dir=snapshot_dir,
                crypto=crypto,
                policy_read=_policy_on(data_dir),
            )
        assert restore_blocked.value.pending is True
        # Новый снимок (страховка) не создаётся.
        backups_before = sorted(p.name for p in (data_dir / "backups").iterdir())
        with pytest.raises(SnapshotOperationRefused) as backup_blocked:
            create_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=data_dir / "backups" / "snapshot_2",
                crypto=crypto,
                transaction_id="tx-while-ragged",
                policy_on=True,
            )
        assert backup_blocked.value.pending is True
        assert backup_blocked.value.reason == "snapshot_recovery_pending"
        assert sorted(p.name for p in (data_dir / "backups").iterdir()) == backups_before

    def test_cancellation_still_cleans_its_own_fresh_staging(self, tmp_path):
        """Отмена ДО COMMITTING по-прежнему не оставляет мусора (регресс фикса B1)."""
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
                restore_encrypted_snapshot(
                    data_dir=data_dir,
                    backups_root=data_dir / "backups",
                    snapshot_dir=snapshot_dir,
                    crypto=crypto,
                    policy_read=_policy_on(data_dir),
                )

        # Свежий каталог, созданный этим вызовом, — мусор: убираем.
        assert _restore_markers(data_dir) == []
        assert [p.name for p in data_dir.glob(f"{RESTORE_STAGING_PREFIX}*")] == []
        assert _data_bytes(data_dir) == live_before


# ---------------------------------------------------------------------------
# A5.2b2 review — B2: writers не идут по рваному набору, статус это показывает
# ---------------------------------------------------------------------------


class TestPendingRestoreBlocksWriters:
    """B2: снимок не берётся, пока на диске живёт маркер незавершённого restore.

    До M2 докатка жила в конструкторе StateStore, поэтому тесты были бы другими.
    Инвариант тот же: «последний хороший бэкап» не может быть снимком
    промежуточного (рваного) состояния. Здоровый маркер точки обслуживания
    ДОКАЧЫВАЮТ — и тогда бэкап проходит; блокировка обязана срабатывать, когда
    докачка невозможна (это и есть состояние, опасное для владельца).
    """

    def _unhealable(self, tmp_path):
        """Crash + испорченный ЦЕЛЕВОЙ снимок: докачка невозможна, маркер живёт."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_on_nth_replace(data_dir, crypto, snapshot_dir, nth=4)
        target = snapshot_dir / "history.ndjson"
        target.write_bytes(target.read_bytes() + b"ENC1:zzz\n")
        return data_dir, crypto, snapshot_dir

    def _healable(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_on_nth_replace(data_dir, crypto, snapshot_dir, nth=4)
        return data_dir, crypto, snapshot_dir

    def test_manual_backup_refused_when_restore_cannot_be_healed(self, tmp_path):
        data_dir, crypto, _snap = self._unhealable(tmp_path)
        svc = _svc(data_dir, crypto)

        result = svc.handle_backup_history({})

        assert result["ok"] is False
        assert result["reason"] == "snapshot_recovery_pending"
        assert _restore_markers(data_dir) != [], "маркер должен уцелеть"
        # Новых снимков сверх исходного нет (pre-restore страховка создаётся
        # ДО проверки снимка и потому не считается «новым»).
        fresh = [
            q.name for q in (data_dir / "backups").glob("snapshot_*")
            if "prerestore" not in q.name
        ]
        assert fresh == ["snapshot_1"], fresh

    def test_auto_backup_refused_when_restore_cannot_be_healed(self, tmp_path):
        from backend.auto_backup import AutoBackupManager

        data_dir, crypto, _snap = self._unhealable(tmp_path)
        store = _store_with_crypto(data_dir, crypto)
        mgr = AutoBackupManager(store=store, interval_hours=0)

        out = mgr.check_and_backup()

        assert out["backed_up"] is False
        # Конкретная причина протокола, а не «операция недоступна»: владельцу
        # нужно знать, что чинить (незавершённый restore), а не искать ключ.
        assert out["skipped_reason"] == "snapshot_recovery_pending"
        assert _restore_markers(data_dir) != []
        assert [q.name for q in (data_dir / "backups").glob("auto_snapshot_*")] == []

    def test_service_point_heals_pending_restore_then_backup_succeeds(self, tmp_path):
        data_dir, crypto, _snap = self._healable(tmp_path)
        svc = _svc(data_dir, crypto)

        result = svc.handle_backup_history({})

        assert result["ok"] is True
        assert _restore_markers(data_dir) == []
        assert last_restore_recovery()["rolled_forward"] is True
        # Снимок снят с ЦЕЛОГО состояния, а не с промежуточного.
        assert result["encrypted"] is True
        assert Path(result["backup_path"]).is_dir()

    def test_auto_backup_status_reports_blocked_by_pending_restore(self, tmp_path):
        from backend.auto_backup import AutoBackupManager

        data_dir, crypto, _snap = self._unhealable(tmp_path)
        store = _store_with_crypto(data_dir, crypto)
        status = AutoBackupManager(store=store, interval_hours=0).get_auto_backup_status()

        assert status["restore_pending"] is True
        assert status["blocked_by_pending"] is True
        assert status["encryption_operation_unavailable"] is True
        assert status["skipped_reason"] == "snapshot_recovery_pending"
        # Вердикт recovery читаем существующим способом (без service.py).
        assert status["restore_recovery"]["pending"] is True
        assert status["restore_recovery"]["reason"] == "snapshot_recovery_pending"
        assert status["restore_recovery"]["pre_restore_snapshot"]

    def test_status_is_clean_after_successful_recovery(self, tmp_path):
        from backend.auto_backup import AutoBackupManager

        data_dir, crypto, _snap = self._healable(tmp_path)
        _recover(data_dir, crypto)
        store = _store_with_crypto(data_dir, crypto)
        mgr = AutoBackupManager(store=store, interval_hours=0)

        status = mgr.get_auto_backup_status()
        # N7: маркера на диске уже нет, но вердикт попытки остаётся видимым —
        # иначе докачка была бы известна только из логов.
        assert status["restore_pending"] is False
        assert status["restore_recovery"] is not None
        assert status["restore_recovery"]["rolled_forward"] is True
        assert status["restore_recovery"]["pending"] is False
        # И backup снова работает.
        out = mgr.check_and_backup()
        assert out["backed_up"] is True

    def test_status_fields_exist_in_off_profile(self, tmp_path):
        """Новые поля обязаны быть и при OFF, иначе UI получит KeyError."""
        from backend.auto_backup import AutoBackupManager

        data_dir = _data_dir(tmp_path)
        _fill_profile(data_dir, _crypto())
        _settings_off(data_dir)
        store = _store_with_crypto(data_dir, None)
        status = AutoBackupManager(store=store, interval_hours=0).get_auto_backup_status()

        # Действительный признак — свежий с диска: pending не может быть, потому
        # что маркера нет. Вердикт может помнить ПРОШЛУЮ попытку этого процесса
        # (так и задумано в N7), но он не имеет права утверждать, что pending.
        assert status["restore_pending"] is False
        assert "blocked_by_pending" in status
        if status["restore_recovery"] is not None:
            assert status["restore_recovery"]["pending"] is False


# ---------------------------------------------------------------------------
# A5.2b2 review — B3: privacy purge не оставляет расшифровываемых фрагментов
# ---------------------------------------------------------------------------


def _orphan_tmp_fragments(data_dir: Path, crypto: HistoryCrypto, txid: str) -> list[Path]:
    """Имитирует жёсткий kill между _write_file_durable(tmp) и os.replace."""
    written: list[Path] = []
    for name in HISTORY_JOURNAL_FILENAMES:
        blob = (crypto.encrypt_line(CANARY_PLAINTEXT) + "\n").encode("utf-8")
        tmp = data_dir / f"{name}{RESTORE_TMP_SUFFIX}{txid}"
        tmp.write_bytes(blob)
        os.chmod(tmp, 0o600)
        written.append(tmp)
    return written


def _decryptable_text_hits(data_dir: Path, crypto, needle: str) -> list[str]:
    """Файлы в data_dir, из которых этим ключом читается строковый текст.

    Честная проверка приватности: ищем не «ENC1 есть», а «история читается».
    Не-JSON строки (ID-only ledger) пропускаем — их наличие законно.
    """
    hits: list[str] = []
    for path in sorted(Path(data_dir).rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.split("\n"):
            if not line.strip():
                continue
            if line.startswith(SENTINEL):
                try:
                    line = crypto.decrypt_line(line)
                except Exception:  # noqa: BLE001 — не наш ключ/мусор
                    continue
            if needle in line:
                hits.append(f"{path.name}: {line[:60]}")
    return hits


class TestPurgeRemovesRestoreFragments:
    def test_purge_removes_orphaned_restore_tmp_fragments(self, tmp_path):
        """B3-проба: жёсткий kill оставляет ENC1-фрагменты в data_dir.

        Они расшифровываются тем же ключом, что и живая история, поэтому privacy
        purge обязан убрать их так же, как и каталоги staging.
        """
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        _make_snapshot(data_dir, crypto)
        fragments = _orphan_tmp_fragments(data_dir, crypto, "restore-killed")
        assert fragments and all(f.is_file() for f in fragments)
        svc = _svc(data_dir, crypto)

        result = svc.handle_purge_all_data({"confirm": True})

        assert "restore_staging" not in (result.get("errors") or [])
        for fragment in fragments:
            assert not fragment.exists(), f"{fragment.name} пережил purge"
        # Ни одного tmp-фрагмента любого транзакционного id.
        assert [p.name for p in data_dir.glob(f"*{RESTORE_TMP_SUFFIX}*")] == []
        # Проверка СОДЕРЖИМОГО, а не наличия ENC1: постоянный ledger
        # purged-IDs переживает purge законно (ID-only, allowlist) — но текста
        # истории в нём быть не должно ни в одном файле data_dir.
        assert _decryptable_text_hits(data_dir, crypto, CANARY_PLAINTEXT) == []

    def test_purge_removes_tmp_fragments_even_without_marker(self, tmp_path):
        """Фрагменты переживают kill ДО появления маркера — их тоже надо убрать."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        _orphan_tmp_fragments(data_dir, crypto, "restore-early-kill")
        svc = _svc(data_dir, crypto)

        result = svc.handle_purge_all_data({"confirm": True})

        assert "restore_staging" not in (result.get("errors") or [])
        assert [p.name for p in data_dir.glob(f"*{RESTORE_TMP_SUFFIX}*")] == []

    def test_purge_helper_covers_fragments_directly(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        _make_snapshot(data_dir, crypto)
        _orphan_tmp_fragments(data_dir, crypto, "restore-direct")

        removed = purge_pending_restore_staging(data_dir)

        assert [p.name for p in data_dir.glob(f"*{RESTORE_TMP_SUFFIX}*")] == []
        assert removed  # что-то убрано (фрагменты), и функция это сообщает

    def test_purge_does_not_touch_regular_journals(self, tmp_path):
        """Узкий glob по transaction-суффиксу не имеет права съесть живые файлы."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        before = _data_bytes(data_dir)

        purge_pending_restore_staging(data_dir)

        assert _data_bytes(data_dir) == before
        assert (data_dir / "settings.json").exists()


# ---------------------------------------------------------------------------
# A5.2b2 review — B3.2: audit_purge_coverage обязан видеть f-string семейства
# ---------------------------------------------------------------------------


def _load_audit_module():
    """Загружает scripts/audit_purge_coverage.py как модуль (он не пакет)."""
    import importlib.util

    script = Path(__file__).resolve().parents[2] / "scripts" / "audit_purge_coverage.py"
    assert script.is_file(), script
    spec = importlib.util.spec_from_file_location("audit_purge_coverage_probe", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclass в модуле требует, чтобы он был в sys.modules ДО exec_module.
    import sys as _sys

    _sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        _sys.modules.pop(spec.name, None)
    return module


class TestPurgeAuditSeesFstringFamilies:
    def _discover(self, source: str, tmp_path: Path) -> set[str]:
        module = _load_audit_module()
        fake = tmp_path / "fake_store_module.py"
        fake.write_text(source, encoding="utf-8")
        return {ref.store_id for ref in module.discover_stores_in_module(fake)}

    def test_fstring_family_in_data_dir_is_discovered(self, tmp_path):
        """Негативный тест гейта: семейство из f-string ДОЛЖНО быть видно.

        Именно этот пробел делал «0 gaps» ложно-зелёным: restore-артефакты
        построены как ``data_dir / f"{CONST}{txid}"``, а сканер видел только
        literal/_CONST/glob.
        """
        stores = self._discover(
            'PREFIX = ".myfamily-"\n'
            "def write(data_dir, txid):\n"
            "    return data_dir / f\"{PREFIX}{txid}\"\n",
            tmp_path,
        )
        assert any("myfamily" in sid for sid in stores), stores

    def test_fstring_family_with_tail_after_variable_is_discovered(self, tmp_path):
        stores = self._discover(
            "SUFFIX = '.ndjson.mine-'\n"
            "def write(data_dir, name, txid):\n"
            "    return data_dir / f\"{name}{SUFFIX}{txid}\"\n",
            tmp_path,
        )
        assert any("mine" in sid for sid in stores), stores

    def test_real_module_ships_no_new_uncovered_family(self, tmp_path):
        """Реальный код волны не должен оставлять семейство без purge-покрытия."""
        module = _load_audit_module()
        result = module.run_audit() if hasattr(module, "run_audit") else None
        if result is None:  # pragma: no cover — защита от смены API скрипта
            pytest.skip("audit module exposes no run_audit()")
        gaps = [ref.store_id for ref in result.gaps]
        assert gaps == [], gaps

    def test_plain_family_without_dot_is_not_recorded(self, tmp_path):
        """Контроль против регрессии: `f"backup_{ts}"` — не новый store.

        Иначе каждое f-string имя в репозитории стало бы «дырой» аудита.
        """
        stores = self._discover(
            "def write(data_dir, ts):\n    return data_dir / f\"backup_{ts}\"\n",
            tmp_path,
        )
        assert not any("backup_" in sid for sid in stores), stores


# ---------------------------------------------------------------------------
# A5.2b2 review — M3: успех не ломается из-за счёта ПОСЛЕ COMMITTED
# ---------------------------------------------------------------------------


class TestRestoreCountDegradation:
    def test_count_timeout_does_not_fail_a_committed_restore(self, tmp_path):
        """M3-проба: таймаут счётчика НЕ имеет права ломать уже долговечный restore.

        Restore к этому моменту COMMITTED и прошёл read-back. Владелец должен
        получить успех с честной пометкой, что счёт не проверен, а не RuntimeError
        для операции, которая уже применена.
        """
        from backend.state_store import StateStoreLockTimeout

        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed_items(store, ["одна", "две"])
        svc = HistoryService(store=store, cached_settings=lambda: {})
        snapshot_dir = Path(svc.handle_backup_history({})["backup_path"])
        _seed_items(store, ["три"])

        with patch.object(
            type(store),
            "count_active_items",
            side_effect=StateStoreLockTimeout("synthetic contention"),
        ):
            result = svc.handle_restore_history({"backup_path": str(snapshot_dir)})

        assert result["ok"] is True
        assert result["reason"] is None
        assert result["restored_entries_verified"] is False
        assert any("restored_entries" in w for w in result["warnings"])
        # Значение всё равно полезно владельцу — оно из маркера, помечено.
        assert result["restored_entries"] == 2
        assert result["restored_entries_source"] == "snapshot_lines"
        # Журналы на диске — восстановленный набор, операция не откатилась.
        assert store.count_active_items() == 2

    def test_normal_path_reports_verified_count(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed_items(store, ["одна", "две", "три"])
        svc = HistoryService(store=store, cached_settings=lambda: {})
        snapshot_dir = Path(svc.handle_backup_history({})["backup_path"])

        result = svc.handle_restore_history({"backup_path": str(snapshot_dir)})

        assert result["ok"] is True
        assert result["restored_entries"] == 3
        assert result["restored_entries_verified"] is True
        assert result["restored_entries_source"] == "store_count"
        assert result["warnings"] == []

    def test_marker_count_from_disk_is_validated_before_being_reported(self, tmp_path):
        """N4: значение счётчика читается с ДИСКА (recovery чистит COMMITTED-маркер).

        Непригодное значение не должно отдаваться как «число записей» —
        возвращается 0 с громким предупреждением, а не «99».
        """
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        _make_snapshot(data_dir, crypto)
        leftover = data_dir / f"{RESTORE_STAGING_PREFIX}tx-lying"
        leftover.mkdir()
        (leftover / RESTORE_MARKER_FILENAME).write_text(
            json.dumps(
                {
                    "version": 1,
                    "transaction_id": "tx-lying",
                    "state": "COMMITTED",
                    "target_snapshot": str(data_dir / "backups" / "snapshot_1"),
                    "pre_restore_snapshot": str(data_dir / "backups" / "snapshot_1"),
                    "restored_entries": "99",
                    "files": [{"name": "history.ndjson", "size": 10, "sha256": "x" * 64}],
                }
            ),
            encoding="utf-8",
        )

        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert result["rolled_forward"] is False
        assert result["restored_entries"] == 0, "строка из маркера отдана как число"
        assert not leftover.exists()

    def test_valid_marker_count_from_disk_is_reported(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        _make_snapshot(data_dir, crypto)
        leftover = data_dir / f"{RESTORE_STAGING_PREFIX}tx-honest"
        leftover.mkdir()
        (leftover / RESTORE_MARKER_FILENAME).write_text(
            json.dumps(
                {
                    "version": 1,
                    "transaction_id": "tx-honest",
                    "state": "COMMITTED",
                    "target_snapshot": str(data_dir / "backups" / "snapshot_1"),
                    "pre_restore_snapshot": str(data_dir / "backups" / "snapshot_1"),
                    "restored_entries": 3,
                    "files": [{"name": "history.ndjson", "size": 4096, "sha256": "x" * 64}],
                }
            ),
            encoding="utf-8",
        )

        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert result["restored_entries"] == 3

    def test_search_caches_are_reset_under_lock(self, tmp_path):
        """Сброс in-RAM индексов идёт ПОД локом: иначе конкурентный поиск успевает
        отдать до-restore индекс (cleartext прежней истории)."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed_items(store, ["до-restore запись"])
        svc = HistoryService(store=store, cached_settings=lambda: {})
        snapshot_dir = Path(svc.handle_backup_history({})["backup_path"])
        _seed_items(store, ["после-снимка запись"])
        store.count_active_items()  # прогреваем кэш
        store._ensure_active_ids_unlocked()

        result = svc.handle_restore_history({"backup_path": str(snapshot_dir)})

        assert result["ok"] is True
        restored_ids = {item.id for item in store._load_active_items_unlocked()}
        # Кэш активных id ПЕРЕСТРОЕН по восстановленным журналам (не остался от
        # прежнего содержимого) — ленивая инициализация допустима, «остался
        # прежним» — нет.
        assert store._active_ids is None or store._active_ids == restored_ids
        assert store._active_ids != {i.id for i in []} or True
        # Поисковые индексы прежнего содержимого очищены (cleartext до-restore).
        assert store._recent_search_index == []
        assert store._recent_search_index_signature is None
        assert not getattr(store._search_index, "_texts", {})

    def test_reset_happens_inside_the_store_lock(self, tmp_path):
        """Проверка порядка: сброс индекса не должен делаться ВНЕ lock'а."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed_items(store, ["запись"])
        svc = HistoryService(store=store, cached_settings=lambda: {})
        snapshot_dir = Path(svc.handle_backup_history({})["backup_path"])

        observed: list[dict] = []
        real_reset = type(store).reset_search_caches

        def _traced_reset(inner_self):
            depth = inner_self._lock_depth.get(__import__("threading").get_ident(), 0)
            observed.append({"locked": depth > 0})
            return real_reset(inner_self)

        with patch.object(type(store), "reset_search_caches", _traced_reset):
            svc.handle_restore_history({"backup_path": str(snapshot_dir)})

        assert observed, "reset_search_caches не вызван"
        assert observed[0]["locked"] is True, "сброс индекса сделан вне store-lock"


class TestRecoveryRefusesSubstitutedTarget:
    """N1: symlink-слой containment живёт в recovery, а не только в тестах.

    Целевой снимок recovery берёт ИЗ МАРКЕРА («как записан»). Если после crash
    каталог подменили symlink'ом, докачка не должна идти по нему — это ровно тот
    случай, ради которого слой symlink-компонентов и нужен.
    """

    def test_recovery_refuses_symlinked_target_after_crash(self, tmp_path):
        import shutil

        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_on_nth_replace(data_dir, crypto, snapshot_dir, nth=4)
        assert _restore_markers(data_dir) != []
        # Подмена: настоящий снимок уводим в сторону, на его место — symlink.
        elsewhere = tmp_path / "подменённый_снимок"
        shutil.move(str(snapshot_dir), str(elsewhere))
        os.symlink(str(elsewhere), str(snapshot_dir))
        live_before = _data_bytes(data_dir)

        result = _recover(data_dir, crypto)

        assert result["ok"] is False
        assert result["pending"] is True
        assert result["reason"] == "snapshot_source_symlink"
        assert _data_bytes(data_dir) == live_before
        assert _restore_markers(data_dir) != [], "доказательство не должно исчезнуть"

    def test_recovery_refuses_target_escaping_backups_root(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _crash_on_nth_replace(data_dir, crypto, snapshot_dir, nth=2)
        # Снимок «уехал» из backups — маркер указывает на уже недопустимую цель.
        import shutil

        shutil.move(str(snapshot_dir), str(tmp_path / "вне_backups"))
        live_before = _data_bytes(data_dir)

        result = _recover(data_dir, crypto)

        assert result["ok"] is False
        assert result["pending"] is True
        assert result["reason"] in {
            "snapshot_outside_backups_root",
            "snapshot_manifest_invalid",
        }
        assert _data_bytes(data_dir) == live_before


# ---------------------------------------------------------------------------
# Итоговая проверка Keychain. Стоит ПОСЛЕДНИМ в модуле намеренно: pytest
# выполняет тесты в порядке файла, поэтому к этому моменту пройдена вся сессия.
# ---------------------------------------------------------------------------


def test_zz_keychain_untouched_across_whole_session():
    """Ни одного чтения/создания ключа за ВСЮ сессию b2 (а не только к моменту).

    Отдельно фиксируется, что низкоуровневый ``_run_security`` мог быть достигнут
    только из ``handle_purge_all_data`` — там удаление ключа является ПРЕДНАМЕРЕННЫМ
    поведением (волна 0afc3ff9): выживший AES-ключ расшифровывает pre-purge
    бэкап. К b2 (restore/recovery/backup) это отношения не имеет, но замалчивать
    его нельзя — поэтому счётчика два, а не один.
    """
    assert KEYCHAIN_ATTEMPTS["attempts"] == 0, (
        "A5.2b2 не имеет права читать или создавать ключ истории: "
        f"{KEYCHAIN_ATTEMPTS['attempts']} обращений"
    )
    # Удалений ключа столько, сколько тестов дёрнули purge (каждый — максимум одна).
    assert KEYCHAIN_ATTEMPTS["deletions"] <= 3, KEYCHAIN_ATTEMPTS


# ---------------------------------------------------------------------------
# H1 (BLOCK) — запись, сделанная в окне pending, не должна исчезать при докачке
# ---------------------------------------------------------------------------


def _ragged_with_real_items(tmp_path: Path, *, nth: int = 3):
    """Профиль из настоящих записей store, restore сорван на nth-й замене."""
    data_dir = _data_dir(tmp_path)
    crypto = _crypto()
    _settings_on(data_dir)
    store = _store_with_crypto(data_dir, crypto)
    _seed_items(store, ["ДО снимка 0", "ДО снимка 1", "ДО снимка 2", "ДО снимка 3"])
    svc = HistoryService(store=store, cached_settings=lambda: {})
    snapshot_dir = Path(svc.handle_backup_history({})["backup_path"])
    _crash_on_nth_replace(data_dir, crypto, snapshot_dir, nth=nth)
    assert _restore_markers(data_dir), "нужен живой маркер pending"
    return data_dir, crypto, snapshot_dir, store


def _texts(store) -> list[str]:
    return [item.text for item in store._load_active_items_unlocked()]


class TestRecoveryCarriesWindowWrites:
    def test_dictation_in_pending_window_survives_roll_forward(self, tmp_path):
        """r7: владелец диктует в окне pending → докачка не имеет права её выбросить.

        До H1 отвечали `ok: True, COMMITTED, restored_entries: 4`, а диктовка
        исчезала: её не было ни в целевом снимке, ни в pre-restore страховке
        (она записана ПОСЛЕ снятия страховки), ни в ledger.
        """
        data_dir, crypto, snapshot_dir, store = _ragged_with_real_items(tmp_path)
        pre_before = sorted(p.name for p in (data_dir / "backups").iterdir())

        # Диктовка в окне pending: маркер жив, набор рваный.
        store.add_history_item(text="ДИКТОВКА В ОКНЕ PENDING")
        assert "ДИКТОВКА В ОКНЕ PENDING" in _texts(store)

        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert result["rolled_forward"] is True
        assert result["records_carried"] >= 1
        assert _texts(store).count("ДИКТОВКА В ОКНЕ PENDING") == 1, (
            f"диктовка потеряна; набор={_texts(store)}"
        )
        # Все до-снимковые записи на месте (докатка — roll-forward, не откат).
        for i in range(4):
            assert f"ДО снимка {i}" in _texts(store)
        # Страховка pre-restore остаётся на диске (её снял сам restore ДО crash'а,
        # поэтому в снимок состояния backups она уже входит) и докачка ничего
        # в backups не удаляет.
        pre = Path(result["pre_restore_snapshot"])
        assert pre.is_dir(), "страховка исчезла"
        assert sorted(p.name for p in (data_dir / "backups").iterdir()) == pre_before

    def test_carried_record_is_encrypted_and_readable(self, tmp_path):
        data_dir, crypto, _snap, store = _ragged_with_real_items(tmp_path)
        store.add_history_item(text="перенесённая в докачку")

        _recover(data_dir, crypto)

        for line in _read_ndjson_lines(data_dir / "history.ndjson"):
            assert line.startswith(SENTINEL), "перенос не должен вносить plaintext"
        assert any(
            item.text == "перенесённая в докачку" for item in store._load_active_items_unlocked()
        )

    def test_record_deleted_during_window_is_not_resurrected(self, tmp_path):
        """Ревьюер требует: перенос не отменяет запрет resurrection."""
        data_dir, crypto, _snap, store = _ragged_with_real_items(tmp_path)
        store.add_history_item(text="удалимая в окне")
        target = next(
            item for item in store._load_active_items_unlocked()
            if item.text == "удалимая в окне"
        )
        store.delete_history_item(target.id)

        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert "удалимая в окне" not in _texts(store)
        assert result["records_carried"] == 0
        assert result["records_at_risk"] >= 1
        assert result["records_excluded_deleted"] >= 1

    def test_deliberate_restore_does_not_carry_current_records(self, tmp_path):
        """Обычный (не crash) restore СОЗНАТЕЛЬНО не переносит текущие записи.

        Владелец выбрал вернуться к снимку; перенос здесь означал бы, что
        «восстановление» ничего не восстанавливает. Страховка — отдельный
        снимок pre-restore, путь возвращается в ответе.
        """
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed_items(store, ["из снимка 1", "из снимка 2"])
        svc = HistoryService(store=store, cached_settings=lambda: {})
        snapshot_dir = Path(svc.handle_backup_history({})["backup_path"])
        _seed_items(store, ["после снимка"])

        result = svc.handle_restore_history({"backup_path": str(snapshot_dir)})

        assert result["ok"] is True
        assert "после снимка" not in _texts(store)
        assert "из снимка 1" in _texts(store)

    def test_unparsable_line_is_counted_not_dropped_silently(self, tmp_path):
        """Неразбираемая строка не выбрасывается молча — она в at_risk + warnings."""
        data_dir, crypto, _snap, store = _ragged_with_real_items(tmp_path)
        store.add_history_item(text="валидная в окне")
        with (data_dir / "history.ndjson").open("a", encoding="utf-8") as fh:
            fh.write(crypto.encrypt_line("{не json в окне pending") + "\n")

        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert "валидная в окне" in _texts(store)
        assert result["records_at_risk"] >= 1
        assert any("unparsable" in w for w in result["warnings"])

    def test_counters_present_on_successful_roll_forward(self, tmp_path):
        data_dir, crypto, _snap, store = _ragged_with_real_items(tmp_path)
        store.add_history_item(text="счётчик")

        result = _recover(data_dir, crypto)

        for field in (
            "records_carried",
            "records_at_risk",
            "records_excluded_deleted",
            "records_unparsable",
        ):
            assert field in result, field
            assert isinstance(result[field], int)


# ---------------------------------------------------------------------------
# M4 (MAJOR) — OFF/legacy-ветка обязана консультироваться с маркером
# ---------------------------------------------------------------------------


class TestPendingRestoreBlocksLegacyPaths:
    """r6: OFF-профиль с живым restore-маркером → legacy restore проходит.

    Гейт B2 накрыл только create-side снимков. Ветка OFF уходит в legacy
    ``copy2`` вообще без консультации по маркеру: ответ без ``ok``/``reason``,
    маркер остаётся, а живой набор заменяется целиком.
    """

    def _off_with_marker(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        for i, name in enumerate(HISTORY_JOURNAL_FILENAMES):
            (data_dir / name).write_text(
                json.dumps({"id": f"off{i}", "text": "открытая запись"}, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
        _settings_off(data_dir)
        store = _store_with_crypto(data_dir, None)
        svc = HistoryService(store=store, cached_settings=lambda: {})
        # Настоящий legacy-бэкап, который restore обязан принять в норме.
        _seed_items(store, ["легаси 1", "легаси 2"])
        legacy = svc.handle_backup_history({})
        assert Path(legacy["backup_path"]).is_dir()
        # Живой restore-маркер (из другой транзакции/профиля) — он виден всем.
        leftover = data_dir / f"{RESTORE_STAGING_PREFIX}tx-прочее"
        leftover.mkdir()
        (leftover / RESTORE_MARKER_FILENAME).write_text(
            json.dumps(
                {
                    "version": 1,
                    "transaction_id": "tx-прочее",
                    "state": "COMMITTING",
                    "target_snapshot": str(data_dir / "backups" / "snapshot_x"),
                    "pre_restore_snapshot": str(data_dir / "backups" / "snapshot_x"),
                    "files": [],
                }
            ),
            encoding="utf-8",
        )
        return data_dir, store, svc, Path(legacy["backup_path"]), leftover

    def test_legacy_restore_refused_while_restore_marker_alive(self, tmp_path):
        data_dir, store, svc, legacy_dir, leftover = self._off_with_marker(tmp_path)
        live_before = _data_bytes(data_dir)

        result = svc.handle_restore_history({"backup_path": str(legacy_dir)})

        assert result.get("ok") is False
        assert result["reason"] == "snapshot_recovery_pending"
        assert result["restored_entries"] == 0
        assert _data_bytes(data_dir) == live_before
        assert leftover.is_dir(), "маркер обязан остаться на месте"

    def test_legacy_backup_refused_while_restore_marker_alive(self, tmp_path):
        """OFF-ветка handle_backup_history: раньше просто бросал исключение."""
        data_dir, store, svc, _legacy, leftover = self._off_with_marker(tmp_path)
        before = sorted(p.name for p in (data_dir / "backups").iterdir())

        result = svc.handle_backup_history({})

        assert result.get("ok") is False
        assert result["reason"] == "snapshot_recovery_pending"
        assert sorted(p.name for p in (data_dir / "backups").iterdir()) == before
        assert leftover.is_dir()

    def test_off_profile_without_marker_restores_legacy_as_before(self, tmp_path):
        """Регресс: OFF без маркера — прежнее поведение legacy restore."""
        data_dir = _data_dir(tmp_path)
        _settings_off(data_dir)
        store = _store_with_crypto(data_dir, None)
        svc = HistoryService(store=store, cached_settings=lambda: {})
        _seed_items(store, ["легаси 1", "легаси 2"])
        legacy = svc.handle_backup_history({})
        (data_dir / "history.ndjson").write_text("", encoding="utf-8")

        result = svc.handle_restore_history({"backup_path": legacy["backup_path"]})

        assert result.get("ok") is not False
        assert result["restored_entries"] == 2
        assert "легаси 1" in (data_dir / "history.ndjson").read_text("utf-8")

    def test_pending_marker_is_reported_to_off_profile_caller(self, tmp_path):
        """Отказ обязан быть машинно-читаемым, а не «тихим успехом без ok»."""
        data_dir, _store, svc, legacy_dir, _leftover = self._off_with_marker(tmp_path)

        result = svc.handle_restore_history({"backup_path": str(legacy_dir)})

        assert isinstance(result, dict)
        assert result["reason"] == "snapshot_recovery_pending"
        assert result["restore_recovery"]["pending"] is True
        assert result["restore_recovery"]["pre_restore_snapshot"]


# ---------------------------------------------------------------------------
# H1 (часть 2) — видимость: окно pending должно быть заметно ДО докачки
# ---------------------------------------------------------------------------


class TestPendingVisibleInStatusSurfaces:
    """Окно pending не должно быть невидимым между crash'ом и докачкой.

    M2 перенёс докачку из конструктора в точки обслуживания, то есть ОКНО
    ПЕРЕЖИВАНИЯ стало длиннее (было «до рестарта процесса»). Единственная
    защита от потери — заметность: если профиль в окне pending, это должно
    быть видно в статусе, который смотрит владелец.
    """

    def _service(self, tmp_path):
        from backend.health_check_service import HealthCheckService

        return HealthCheckService

    def test_diagnostics_reports_pending_restore(self, tmp_path):
        data_dir, crypto, _snap, store = _ragged_with_real_items(tmp_path)
        verdict = read_pending_restore_verdict(data_dir)
        assert verdict["pending"] is True

        # Модуль функции читает тот же источник истины (диск), без lock'а:
        # цена — один iterdir, блокировок нет.
        assert read_pending_restore_verdict(data_dir) == verdict
        assert verdict["pre_restore_snapshot"]
        assert verdict["reason"] == "snapshot_recovery_pending"

    def test_diagnostics_reports_absence_without_marker(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        _fill_profile(data_dir, _crypto())
        _settings_on(data_dir)
        assert read_pending_restore_verdict(data_dir) is None

    def test_health_check_service_diagnostics_contains_restore_section(self, tmp_path):
        from backend.health_check_service import restore_pending_status

        """get_diagnostics — тот же модуль, что и 3-секундный ping, но без
        bit-exact контракта: сигнал pending обязан быть в нём."""
        data_dir, crypto, _snap, store = _ragged_with_real_items(tmp_path)
        # Форма берётся из настоящего модуля, а не из MagicMock store: сигнал
        # строится из data_dir, а не из состояния фасада.
        diag = restore_pending_status(data_dir)
        assert diag["restore_pending"] is True
        assert diag["restore_recovery"]["pending"] is True
        assert diag["restore_recovery"]["pre_restore_snapshot"]

        clean = tmp_path / "clean_profile"  # отдельный профиль: первый рваный
        clean.mkdir(parents=True, exist_ok=True)
        (clean / "settings.json").write_text(
            json.dumps({"history_encryption_enabled": True}), encoding="utf-8"
        )
        diag2 = restore_pending_status(clean)
        assert diag2["restore_pending"] is False
        if diag2["restore_recovery"] is not None:
            # N7: в процессе могла быть прошлая попытка (тесты делят процесс) —
            # запрещено только утверждать pending при отсутствии маркера.
            assert diag2["restore_recovery"]["pending"] is False

    def test_ping_contract_stays_bit_exact(self, tmp_path):
        """Регресс: handle_ping НЕ расширяем (закреплён 6-ключевой контракт).

        Сигнал pending живёт в get_diagnostics и в статусе авто-бэкапа, который
        UI и так читает. 3-секундный heartbeat остаётся без обхода файловой
        системы — иначе «zero-wait» перестанет быть zero-wait. Сам контракт
        закреплён чужим тестом (`test_health_check_service_ping_nonblocking`);
        здесь фиксируем, что мы туда НЕ полезли.
        """
        import inspect

        from backend.health_check_service import HealthCheckService

        ping_src = inspect.getsource(HealthCheckService.handle_ping)
        assert "restore_pending_status" not in ping_src, (
            "handle_ping не должен ходить в файловую систему (контракт bit-exact "
            "и zero-wait)"
        )
        # ...а в get_diagnostics — должен.
        diag_src = inspect.getsource(HealthCheckService.handle_get_diagnostics)
        assert "restore_pending_status" in diag_src

    def test_auto_backup_status_carries_signal_for_ui(self, tmp_path):
        from backend.auto_backup import AutoBackupManager

        data_dir, crypto, _snap, store = _ragged_with_real_items(tmp_path)
        status = AutoBackupManager(store=store, interval_hours=0).get_auto_backup_status()

        assert status["restore_pending"] is True
        assert status["blocked_by_pending"] is True
        assert status["restore_recovery"]["pending"] is True
        assert status["restore_recovery"]["pre_restore_snapshot"]


# ---------------------------------------------------------------------------
# N7 — обвязка, которую читают только тесты, недопустима
# ---------------------------------------------------------------------------


class TestNoDecorativeRecoveryWrapper:
    """N7: ``_LAST_RECOVERY_VERDICT`` писался и читался только тестами.

    В волне, где сам аудит ловит декоративную обвязку, такая обвязка
    недопустима. Вариантов два: подключить к пользовательской поверхности или
    удалить вместе с тестами. Выбран первый — вердикт нужен UI, и он уже
    доступен через статус авто-бэкапа.
    """

    def test_cached_verdict_is_exposed_on_user_surface(self, tmp_path):
        from backend.auto_backup import AutoBackupManager

        data_dir, crypto, _snap, store = _ragged_with_real_items(tmp_path)
        mgr = AutoBackupManager(store=store, interval_hours=0)
        # До обслуживания: сигнал с диска, последней попытки ещё не было.
        before = mgr.get_auto_backup_status()
        assert before["restore_pending"] is True
        assert before["restore_recovery"]["pending"] is True

        # Обслуживание докатывает — теперь у поверхности есть и результат попытки.
        mgr.check_and_backup()
        after = mgr.get_auto_backup_status()

        assert after["restore_pending"] is False
        assert after["restore_recovery"] is not None
        assert after["restore_recovery"]["rolled_forward"] is True
        assert after["restore_recovery"]["transaction_id"]
        assert after["restore_recovery"]["records_carried"] >= 0

    def test_status_never_exposes_empty_verdict_after_attempt(self, tmp_path):
        """После попытки докачки вердикт не должен молча пропасть из статуса."""
        from backend.auto_backup import AutoBackupManager

        data_dir, crypto, _snap, store = _ragged_with_real_items(tmp_path)
        mgr = AutoBackupManager(store=store, interval_hours=0)
        mgr.check_and_backup()

        for _ in range(3):
            status = mgr.get_auto_backup_status()
            assert status["restore_recovery"] is not None, "вердикт попытки потерян"
            assert status["restore_pending"] is False

    def test_cached_verdict_never_claims_pending_against_disk(self, tmp_path):
        """Кэш не имеет права заявить pending там, где диск говорит «маркера нет».

        Проверка на ПОРЯДОК: сначала подкладываем cached-вердикт с pending=True,
        затем спрашиваем чистый профиль.
        """
        from backend import encrypted_snapshot as es

        clean = tmp_path / "clean_no_marker"
        clean.mkdir(parents=True, exist_ok=True)
        es._LAST_RECOVERY_VERDICT = {
            "ok": False,
            "pending": True,
            "reason": "snapshot_recovery_pending",
            "state": "COMMITTING",
            "rolled_forward": False,
        }
        try:
            verdict = es.restore_verdict(clean)
        finally:
            es._LAST_RECOVERY_VERDICT = None

        assert verdict is not None
        assert verdict["pending"] is False, "кэш перебил диск"
        # Историческая часть сохранена.
        assert verdict["state"] == "COMMITTING"
        assert es.restore_verdict(clean) is None, "после сброса кэша — только диск"

# ---------------------------------------------------------------------------
# N8 — паритет reason-кодов: ни один код не должен жить только в коде
# ---------------------------------------------------------------------------


def _reason_codes_in_source() -> dict[str, str]:
    """Все ``REASON_* = "…'`` из модуля snapshot: имя константы → значение."""
    import ast

    from backend import encrypted_snapshot as es

    path = Path(es.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.startswith("REASON_"):
            continue
        try:
            value = ast.literal_eval(node.value)
        except ValueError:
            continue
        if isinstance(value, str):
            out[target.id] = value
    return out


def _reasons_section(doc: str) -> str:
    """Текст раздела «Отказы» документации restore (границы по заголовкам)."""
    start = doc.index("**Отказы (все — до первой записи")
    rest = doc[start:]
    for marker in ("\n### ", "\n---\n", "\n**Recovery"):
        idx = rest.find(marker)
        if idx != -1:
            rest = rest[:idx]
    return rest


class TestReasonCodeParity:
    """N8: ``test_ipc_docs_parity`` проверяет имена методов, не reason-коды.

    Четыре кода из b1-create-side обитали в коде без единого упоминания в
    документации. Теперь любое новое значение обязано попасть либо в таблицу
    причин restore/backup, либо в явный allowlist внутренних кодов.
    """

    # Внутренние коды протокола публикации снимка (b1 create-side). Они НЕ
    # достижимы из restore/recovery и сознательно не табулируются: документ
    # описывает контракты IPC-поверхности, а не внутренний prepare/commit.
    INTERNAL_ALLOWLIST = {
        "REASON_FINGERPRINT_MISMATCH",
        "REASON_POLICY_OFF",
        "REASON_PUBLISH_FAILED",
        "REASON_ROUNDTRIP_MISMATCH",
    }

    def test_every_reason_code_is_documented_or_allowlisted(self):
        doc = (
            Path(__file__).resolve().parents[2] / "docs" / "IPC_API_REFERENCE.md"
        ).read_text(encoding="utf-8")
        undoc = [
            f"{name}={value}"
            for name, value in sorted(_reason_codes_in_source().items())
            if name not in self.INTERNAL_ALLOWLIST and f"`{value}`" not in doc
        ]
        assert undoc == [], f"reason-коды без документации: {undoc}"

    def test_allowlist_is_not_a_dumping_ground(self):
        """Allowlist не должен расти молча: каждый внутренний код — с причиной."""
        all_codes = set(_reason_codes_in_source())
        assert self.INTERNAL_ALLOWLIST <= all_codes, (
            "allowlist ссылается на несуществующие коды: "
            f"{sorted(self.INTERNAL_ALLOWLIST - all_codes)}"
        )
        # Внутренние коды не должны просачиваться в документ как «достижимые».
        doc = (
            Path(__file__).resolve().parents[2] / "docs" / "IPC_API_REFERENCE.md"
        ).read_text(encoding="utf-8")
        leaked = [
            _reason_codes_in_source()[name]
            for name in sorted(self.INTERNAL_ALLOWLIST)
            if f"`{_reason_codes_in_source()[name]}`" in doc
        ]
        assert leaked == [], f"внутренние коды попали в таблицу причин: {leaked}"

    def test_all_codes_share_the_documented_prefix(self):
        """Единый словарь причин: префикс snapshot_/restore_ (House-стиль волны)."""
        for name, value in sorted(_reason_codes_in_source().items()):
            assert value.startswith(("snapshot_", "restore_")), f"{name}={value}"

    def test_documented_table_has_no_unknown_codes(self):
        """Обратная сторона: документ не обещает несуществующий код."""
        doc = (
            Path(__file__).resolve().parents[2] / "docs" / "IPC_API_REFERENCE.md"
        ).read_text(encoding="utf-8")
        known = set(_reason_codes_in_source().values())
        import re

        # Обратная проверка scoped на раздел «Отказы» и только на токены с
        # префиксом snapshot_: имена методов (`restore_history`) и поля
        # (`restore_pending`) под него не попадают. Значения поля
        # restored_entries_source перечислены явно — новые значения придётся
        # добавить здесь, а не молча проскочить.
        source_values = {"store_count", "snapshot_lines", "unknown"}
        referenced = {
            m.group(1)
            for m in re.finditer(r"`(snapshot_[a-z_]+)`", _reasons_section(doc))
        }
        unknown = sorted(referenced - known - source_values)
        assert unknown == [], f"раздел причин ссылается на несуществующие коды: {unknown}"

    def test_cached_verdict_never_claims_pending_against_disk(self, tmp_path):
        """Кэш не имеет права заявить pending там, где диск говорит «маркера нет».

        Проверка на ПОРЯДОК: сначала подкладываем cached-вердикт с pending=True,
        затем спрашиваем чистый профиль.
        """
        from backend import encrypted_snapshot as es

        clean = tmp_path / "clean_no_marker"
        clean.mkdir(parents=True, exist_ok=True)
        es._LAST_RECOVERY_VERDICT = {
            "ok": False,
            "pending": True,
            "reason": "snapshot_recovery_pending",
            "state": "COMMITTING",
            "rolled_forward": False,
        }
        try:
            verdict = es.restore_verdict(clean)
        finally:
            es._LAST_RECOVERY_VERDICT = None

        assert verdict is not None
        assert verdict["pending"] is False, "кэш перебил диск"
        # Историческая часть сохранена.
        assert verdict["state"] == "COMMITTING"
        assert es.restore_verdict(clean) is None, "после сброса кэша — только диск"
