"""A5.2b3 — превентивный disk-guard + retention снимков нового формата.

Спека `docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md` §5.1
(«проверить … наличие незавершённой операции») и §6 (auto-backup gate ДО
retention/pruning) + карточка
`docs/superpowers/plans/2026-09-26-a52b3-disk-guard.md`.

Только synthetic tmp-профили и СЛУЧАЙНЫЙ тестовый ключ (`os.urandom(32)`).
Системный Keychain не трогается, и это доказывается ДВУМЯ независимыми
способами:

  1. счётчики на патчах `crypto_keystore._run_security` /
     `get_or_create_history_key` / `history_crypto.build_history_crypto`
     (фикстура `_forbid_keychain` + `test_keychain_attempts_stay_zero`);
  2. OS-шим: сессионная фикстура `_forbid_security_cli` кладёт на PATH
     поддельный исполняемый `security`, который ТОЛЬКО пишет маркер вызова.
     Ни один subprocess в этой сессии не может дойти до настоящего Keychain
     незаметно; `test_keychain_never_reached_at_os_level` требует, чтобы
     маркер не появился (путь фиксированный — тем же маркером проверяется
     весь прогон гейтов, см. отчёт волны).
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import auto_backup as auto_backup_mod
from backend import encrypted_snapshot as snap
from backend.encrypted_snapshot import SnapshotOperationRefused
from backend.history_crypto import HistoryCrypto
from backend.state_store import HISTORY_JOURNAL_FILENAMES, StateStore

# Фиксированный путь OS-шима: одинаков для всего прогона pytest-сессии, чтобы
# факт обращения к `security(1)` можно было проверить и ПОСЛЕ прогона.
SECURITY_SHIM_MARKER = Path(tempfile.gettempdir()) / "krab_a52b3_security_invocations.log"

# ДВА счётчика (приём b1/b2): чтение/создание ключа — ноль по определению;
# удаление ключа в privacy purge — отдельный счётчик (в этой волне purge не
# вызывается, поэтому и он обязан остаться нулём).
KEYCHAIN_ATTEMPTS = {"attempts": 0, "deletions": 0}


def _key() -> bytes:
    return os.urandom(32)


def _crypto(key: bytes | None = None) -> HistoryCrypto:
    return HistoryCrypto(key or _key())


def _data_dir(tmp_path: Path, name: str = "data") -> Path:
    base = tmp_path / name
    base.mkdir(parents=True, exist_ok=True)
    return base


def _settings(data_dir: Path, payload: dict) -> None:
    (data_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")


def _settings_on(data_dir: Path) -> None:
    _settings(data_dir, {"history_encryption_enabled": True})


def _settings_off(data_dir: Path) -> None:
    _settings(data_dir, {"history_encryption_enabled": False})


def _policy_on(data_dir: Path) -> bool:
    from backend.history_encryption_policy import data_dir_policy_reader

    return data_dir_policy_reader(data_dir)


def _store_with_crypto(data_dir: Path, crypto) -> StateStore:
    store = StateStore(data_dir)
    store._get_history_crypto = lambda: crypto  # инъекция ключа БЕЗ Keychain
    return store


def _seed(store, texts: list[str]) -> None:
    for text in texts:
        store.add_history_item(text=text)


def _data_bytes(data_dir: Path) -> dict[str, bytes]:
    return {
        name: (data_dir / name).read_bytes()
        for name in HISTORY_JOURNAL_FILENAMES
        if (data_dir / name).is_file()
    }


def _journal_bytes(data_dir: Path) -> int:
    """Суммарный размер управляемых журналов на диске (как считает гард)."""
    total = 0
    for name in HISTORY_JOURNAL_FILENAMES:
        path = data_dir / name
        if path.is_file():
            total += path.stat().st_size
    return total


def _make_snapshot(
    data_dir: Path, crypto: HistoryCrypto, name: str = "snapshot_1", txid: str = "tx-a52b3-0001"
) -> Path:
    snapshot_dir = data_dir / "backups" / name
    result = snap.create_encrypted_snapshot(
        data_dir=data_dir,
        backup_dir=snapshot_dir,
        crypto=crypto,
        transaction_id=txid,
        policy_on=True,
    )
    assert result["state"] == snap.STATE_COMMITTED
    return snapshot_dir


def _dirs(base: Path, prefix: str = "") -> list[str]:
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and p.name.startswith(prefix))


# ---------------------------------------------------------------------------
# Подмена файловой системы: сколько «свободно» и КАК именно спросили
# ---------------------------------------------------------------------------


def _usage(value: int) -> SimpleNamespace:
    return SimpleNamespace(total=value * 4, used=value * 3, free=value)


def _install_usage(monkeypatch, resolver) -> list[str]:
    """Подменяет единственную точку опроса ФС; возвращает список запрошенных путей.

    ``resolver(path) -> int`` — свободные байты; исключение из resolver'а
    эмулирует отказ ФС (``disk_usage`` бросает, а не возвращает «0 свободно»).
    """
    seen: list[str] = []

    def _fake(target):
        seen.append(str(target))
        return _usage(resolver(Path(target)))

    monkeypatch.setattr(snap, "_filesystem_usage", _fake)
    return seen


def _plenty(_path: Path) -> int:
    return 1 << 40


def _nothing(_path: Path) -> int:
    return 0


def _under(prefix: Path, free: int, fallback: int = 0):
    """Свободно только внутри ``prefix`` (доказывает «спросили тот том»)."""

    def _resolve(path: Path) -> int:
        return free if str(path).startswith(str(prefix)) else fallback

    return _resolve


def _boom(_path: Path) -> int:
    raise OSError(errno.EACCES, "disk usage недоступен")


# ---------------------------------------------------------------------------
# Фикстуры: ни один тест не должен дотянуться до Keychain
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _forbid_keychain(monkeypatch):
    """Счётчик обращений к Keychain (патчи уровня модулей, как в b1/b2)."""
    import backend.crypto_keystore as ks
    import backend.history_crypto as hc

    def _counted(*_a, **_k):
        KEYCHAIN_ATTEMPTS["attempts"] += 1
        raise AssertionError("A5.2b3 не должен обращаться к системному Keychain")

    def _counted_deletion(*_a, **_k):
        KEYCHAIN_ATTEMPTS["deletions"] += 1
        raise AssertionError("Keychain недоступен в тестах (счётчик удалений)")

    monkeypatch.setattr(ks, "_run_security", _counted_deletion)
    monkeypatch.setattr(ks, "get_or_create_history_key", _counted)
    monkeypatch.setattr(hc, "build_history_crypto", _counted)
    yield


@pytest.fixture(scope="session", autouse=True)
def _forbid_security_cli(tmp_path_factory):
    """OS-шим на `security`: настоящий бинарь недостижим из этой сессии.

    Доказательство вторым способом: любой `subprocess.run(["security", …])`
    попадёт в подмену, которая ничего не делает, кроме записи маркера.
    """
    bin_dir = tmp_path_factory.mktemp("a52b3_shim_bin")
    script = bin_dir / "security"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{SECURITY_SHIM_MARKER}"\n'
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    SECURITY_SHIM_MARKER.unlink(missing_ok=True)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
    try:
        yield SECURITY_SHIM_MARKER
    finally:
        os.environ["PATH"] = old_path


# ---------------------------------------------------------------------------
# Task 1 — превентивный disk-guard (создание + restore)
# ---------------------------------------------------------------------------


class TestDiskGuardRefusesBeforeAnyWrite:
    """Отказ ДО первой записи: ни staging, ни pre-restore снимка, ни замены."""

    def test_snapshot_refused_before_staging_is_created(self, tmp_path, monkeypatch):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        _seed(_store_with_crypto(data_dir, crypto), ["одна", "две", "три"])
        before = _data_bytes(data_dir)
        _install_usage(monkeypatch, _nothing)

        with pytest.raises(SnapshotOperationRefused) as exc:
            snap.create_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=data_dir / "backups" / "snapshot_1",
                crypto=crypto,
                transaction_id="tx-a52b3-1",
                policy_on=True,
            )

        assert exc.value.reason == snap.REASON_INSUFFICIENT_SPACE
        # Отказ ≠ «начали и упали»: на диске не осталось НИЧЕГО нового.
        assert exc.value.pending is False
        assert not (data_dir / "backups").exists()
        assert _data_bytes(data_dir) == before

    def test_restore_refused_before_pre_restore_snapshot(self, tmp_path, monkeypatch):
        """Ключевой случай волны: «лишний полный снимок при заполнении диска»."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        _seed(_store_with_crypto(data_dir, crypto), ["до снимка"])
        snapshot_dir = _make_snapshot(data_dir, crypto)
        _seed(_store_with_crypto(data_dir, crypto), ["после снимка"])
        before = _data_bytes(data_dir)
        _install_usage(monkeypatch, _nothing)

        with pytest.raises(SnapshotOperationRefused) as exc:
            snap.restore_encrypted_snapshot(
                data_dir=data_dir,
                backups_root=data_dir / "backups",
                snapshot_dir=snapshot_dir,
                crypto=crypto,
                policy_read=_policy_on(data_dir),
            )

        assert exc.value.reason == snap.REASON_INSUFFICIENT_SPACE
        assert exc.value.pending is False
        # Ни страховки, ни маркера, ни замены журналов.
        assert _dirs(data_dir / "backups", "snapshot_prerestore_") == []
        assert _dirs(data_dir, snap.RESTORE_STAGING_PREFIX) == []
        assert list(data_dir.glob(f"*{snap.RESTORE_TMP_SUFFIX}*")) == []
        assert _data_bytes(data_dir) == before

    def test_enough_space_still_allows_snapshot_and_restore(self, tmp_path, monkeypatch):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        _seed(_store_with_crypto(data_dir, crypto), ["одна", "две"])
        _install_usage(monkeypatch, _plenty)

        snapshot_dir = _make_snapshot(data_dir, crypto)
        result = snap.restore_encrypted_snapshot(
            data_dir=data_dir,
            backups_root=data_dir / "backups",
            snapshot_dir=snapshot_dir,
            crypto=crypto,
            policy_read=_policy_on(data_dir),
        )

        assert result["state"] == snap.RESTORE_STATE_COMMITTED
        assert Path(result["pre_restore_snapshot"]).is_dir()
        assert _seed(_store_with_crypto(data_dir, crypto), ["третья"]) is None

    def test_space_is_measured_on_target_volume_not_root(self, tmp_path, monkeypatch):
        """`backups` — symlink на другой том: спрашивается ИМЕННО тот том."""
        data_dir = _data_dir(tmp_path)
        other_volume = tmp_path / "other_volume"
        other_volume.mkdir()
        crypto = _crypto()
        _settings_on(data_dir)
        _seed(_store_with_crypto(data_dir, crypto), ["одна"])
        (data_dir / "backups").symlink_to(other_volume, target_is_directory=True)
        seen = _install_usage(monkeypatch, _under(other_volume, free=1 << 40))

        result = snap.create_encrypted_snapshot(
            data_dir=data_dir,
            backup_dir=data_dir / "backups" / "snapshot_1",
            crypto=crypto,
            transaction_id="tx-a52b3-symlink",
            policy_on=True,
        )

        assert result["state"] == snap.STATE_COMMITTED
        assert seen, "гард вообще не спросил про место"
        assert all(str(p).startswith(str(other_volume)) for p in seen), seen
        assert all(p != "/" for p in seen), "спросили корень вместо тома backups"

    def test_symlinked_target_volume_is_the_one_that_can_refuse(self, tmp_path, monkeypatch):
        """Обратная сторона: пустой том backups отказывает, даже если `/` полон."""
        data_dir = _data_dir(tmp_path)
        other_volume = tmp_path / "other_volume"
        other_volume.mkdir()
        crypto = _crypto()
        _settings_on(data_dir)
        _seed(_store_with_crypto(data_dir, crypto), ["одна"])
        (data_dir / "backups").symlink_to(other_volume, target_is_directory=True)
        _install_usage(monkeypatch, _under(other_volume, free=0, fallback=1 << 40))

        with pytest.raises(SnapshotOperationRefused) as exc:
            snap.create_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=data_dir / "backups" / "snapshot_1",
                crypto=crypto,
                transaction_id="tx-a52b3-symlink-full",
                policy_on=True,
            )

        assert exc.value.reason == snap.REASON_INSUFFICIENT_SPACE
        assert _dirs(other_volume) == []

    def test_room_for_snapshot_but_not_for_safety_net_is_refused(self, tmp_path, monkeypatch):
        """Страховка — не опция: без неё цена неудачного restore выше отказа."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        _seed(_store_with_crypto(data_dir, crypto), ["одна"])
        journal_bytes = _journal_bytes(data_dir)
        assert journal_bytes > 0
        one_copy = int(journal_bytes * snap.ENC1_EXPANSION_FACTOR)
        need = snap.required_bytes(journal_bytes=journal_bytes, copies=1)
        assert one_copy < need  # запас реально существует, а не фикция

        _install_usage(monkeypatch, lambda _p: one_copy)
        with pytest.raises(SnapshotOperationRefused) as exc:
            snap.create_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=data_dir / "backups" / "snapshot_1",
                crypto=crypto,
                transaction_id="tx-a52b3-tight",
                policy_on=True,
            )
        assert exc.value.reason == snap.REASON_INSUFFICIENT_SPACE

        # Ровно порог — проходит (сравнение «<», не «<=»).
        _install_usage(monkeypatch, lambda _p: need)
        result = snap.create_encrypted_snapshot(
            data_dir=data_dir,
            backup_dir=data_dir / "backups" / "snapshot_2",
            crypto=crypto,
            transaction_id="tx-a52b3-exact",
            policy_on=True,
        )
        assert result["state"] == snap.STATE_COMMITTED

    def test_filesystem_query_failure_is_fail_closed(self, tmp_path, monkeypatch):
        """Ров recurring-класс «fail-open в except-ветке safety-проверки»."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        _seed(_store_with_crypto(data_dir, crypto), ["одна"])
        _install_usage(monkeypatch, _boom)

        with pytest.raises(SnapshotOperationRefused) as exc:
            snap.create_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=data_dir / "backups" / "snapshot_1",
                crypto=crypto,
                transaction_id="tx-a52b3-boom",
                policy_on=True,
            )

        assert exc.value.reason == snap.REASON_INSUFFICIENT_SPACE
        assert not (data_dir / "backups").exists()

    def test_other_refusals_win_and_disk_is_never_queried(self, tmp_path, monkeypatch):
        """Сценарии не смешиваются: живой restore-маркер важнее вопроса про место."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        _seed(_store_with_crypto(data_dir, crypto), ["одна"])
        _make_snapshot(data_dir, crypto, name="snapshot_1", txid="tx-1")
        marker_dir = data_dir / f"{snap.RESTORE_STAGING_PREFIX}restore-1"
        marker_dir.mkdir()
        (marker_dir / snap.RESTORE_MARKER_FILENAME).write_text(
            json.dumps({"version": snap.RESTORE_MARKER_VERSION, "state": "COMMITTING"}),
            encoding="utf-8",
        )
        seen = _install_usage(monkeypatch, _plenty)

        with pytest.raises(SnapshotOperationRefused) as exc:
            snap.create_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=data_dir / "backups" / "snapshot_2",
                crypto=crypto,
                transaction_id="tx-a52b3-pending",
                policy_on=True,
            )

        assert exc.value.reason == snap.REASON_RECOVERY_PENDING
        assert exc.value.pending is True
        assert seen == []


def test_keychain_attempts_stay_zero(tmp_path, monkeypatch):
    """Сквозная проверка счётчика: гард ключ не создаёт и не читает."""
    data_dir = _data_dir(tmp_path)
    crypto = _crypto()
    _settings_on(data_dir)
    _seed(_store_with_crypto(data_dir, crypto), ["одна", "две"])
    _install_usage(monkeypatch, _plenty)
    snapshot_dir = _make_snapshot(data_dir, crypto)
    snap.restore_encrypted_snapshot(
        data_dir=data_dir,
        backups_root=data_dir / "backups",
        snapshot_dir=snapshot_dir,
        crypto=crypto,
        policy_read=_policy_on(data_dir),
    )
    assert KEYCHAIN_ATTEMPTS["attempts"] == 0
    assert KEYCHAIN_ATTEMPTS["deletions"] == 0


def test_keychain_never_reached_at_os_level(_forbid_security_cli):
    """Второе доказательство: поддельный `security` ни разу не был вызван."""
    assert _forbid_security_cli == SECURITY_SHIM_MARKER
    assert not SECURITY_SHIM_MARKER.exists(), SECURITY_SHIM_MARKER.read_text(encoding="utf-8")


def test_shim_actually_blocks_the_real_binary(tmp_path):
    """Сам шим работает: иначе «нулевой счётчик» ничего не доказывает.

    Проба идёт через `/bin/sh -c`: `conftest._neutralize_keychain_security`
    (репозиторный OS-шим) дивертит только прямые вызовы `subprocess.run` с
    argv[0] == "security", а реальное разрешение `security` в PATH делает уже
    shell — то есть проверяется именно тень на PATH, а не патч.
    """
    shim = Path(os.environ["PATH"].split(os.pathsep)[0]) / "security"
    marker = tmp_path / "shim.log"
    original = shim.read_text(encoding="utf-8")
    shim.write_text(f'#!/bin/sh\necho probe >> "{marker}"\nexit 1\n', encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", "security find-generic-password"],
            capture_output=True,
            text=True,
        )
    finally:
        shim.write_text(original, encoding="utf-8")
        shim.chmod(shim.stat().st_mode)
    assert result.returncode == 1
    assert marker.read_text(encoding="utf-8").strip() == "probe"
    assert shutil.which("security") == str(shim)


# ---------------------------------------------------------------------------
# Task 2 — retention только для новых форматов
# ---------------------------------------------------------------------------

def _fake_snapshot_dir(data_dir: Path, crypto, name: str, txid: str) -> Path:
    """Настоящий COMMITTED-снимок под заданным именем (тот же протокол, что в проде)."""
    return _make_snapshot(data_dir, crypto, name=name, txid=txid)


def _plain_dir(parent: Path, name: str) -> Path:
    """Каталог-заглушка (legacy-копия/постороннее имя) — без протокола снимка."""
    path = parent / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "history.ndjson").write_text('{"id":"legacy"}\n', encoding="utf-8")
    return path


def _mgr(store, *, max_copies: int, interval_hours: float = 0.0):
    return auto_backup_mod.AutoBackupManager(
        store=store, interval_hours=interval_hours, max_copies=max_copies
    )


def _live_restore_marker(data_dir: Path, *, pre_restore: Path, target: Path) -> Path:
    """Живой restore-маркер в data_dir (сорванный restore — как после crash)."""
    staging = data_dir / f"{snap.RESTORE_STAGING_PREFIX}restore-20260101T000000Z-abcd"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / snap.RESTORE_MARKER_FILENAME).write_text(
        json.dumps(
            {
                "version": snap.RESTORE_MARKER_VERSION,
                "state": "COMMITTING",
                "transaction_id": "restore-20260101T000000Z-abcd",
                "target_snapshot": str(target),
                "pre_restore_snapshot": str(pre_restore),
            }
        ),
        encoding="utf-8",
    )
    return staging


def _family(backups_root: Path, family: str) -> list[str]:
    """Имена каталогов СЕМЕЙСТВА по классификатору модуля (не по префиксу в тесте)."""
    if not backups_root.is_dir():
        return []
    return sorted(
        p.name for p in backups_root.iterdir() if p.is_dir() and snap.snapshot_family(p.name) == family
    )


class TestRetentionCapsNewFormats:
    def test_auto_snapshots_capped_by_max_copies_keeping_newest(self, tmp_path, monkeypatch):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        _install_usage(monkeypatch, _plenty)
        for i in range(3):
            _fake_snapshot_dir(data_dir, crypto, f"auto_snapshot_20200101_00000{i}", f"tx-old-{i}")

        result = _mgr(store, max_copies=2).check_and_backup()

        assert result["backed_up"] is True
        left = _family(data_dir / "backups", "auto")
        assert len(left) == 2, left
        # Из трёх старых остался самый новый + только что созданный.
        assert "auto_snapshot_20200101_000000" not in left
        assert "auto_snapshot_20200101_000002" in left

    def test_manual_snapshots_capped_in_their_own_family(self, tmp_path, monkeypatch):
        """Отдельная семья со своим лимитом: ручной бэкап не вытесняется авто."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        _install_usage(monkeypatch, _plenty)
        for i in range(3):
            _fake_snapshot_dir(data_dir, crypto, f"snapshot_20200101_00000{i}", f"tx-man-{i}")
        _fake_snapshot_dir(data_dir, crypto, "auto_snapshot_20200101_000000", "tx-auto-0")

        _mgr(store, max_copies=2).check_and_backup()

        manual = _family(data_dir / "backups", "manual")
        assert len(manual) == 2, manual
        assert "snapshot_20200101_000002" in manual
        # Авто-семья своим лимитом не тронута (1 старая + 1 новая = 2 при max_copies=2).
        auto = _family(data_dir / "backups", "auto")
        assert len(auto) == 2, auto
        assert "auto_snapshot_20200101_000000" in auto

    def test_prerestore_snapshots_capped(self, tmp_path, monkeypatch):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        _install_usage(monkeypatch, _plenty)
        for i in range(6):
            _fake_snapshot_dir(
                data_dir, crypto, f"snapshot_prerestore_20200101T00000{i}Z-0000", f"tx-pre-{i}"
            )

        _mgr(store, max_copies=7).check_and_backup()

        left = _family(data_dir / "backups", "prerestore")
        assert len(left) == snap.PRERESTORE_KEEP, left
        assert left == sorted(left)[-snap.PRERESTORE_KEEP:]

    def test_legacy_backups_never_deleted_at_on(self, tmp_path, monkeypatch):
        """Инвентаризация legacy plaintext — A5.2c и решение владельца."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        _install_usage(monkeypatch, _plenty)
        legacy = [_plain_dir(data_dir / "backups", f"backup_20200101_00000{i}") for i in range(3)]
        legacy += [
            _plain_dir(data_dir / "backups", f"auto_backup_20200101_00000{i}") for i in range(3)
        ]

        result = _mgr(store, max_copies=1).check_and_backup()

        assert result["backed_up"] is True
        for path in legacy:
            assert path.is_dir(), f"{path.name} удалён retention'ом"
        status = _mgr(store, max_copies=1).get_auto_backup_status()
        assert status["total_backups"] == 3

    def test_off_profile_keeps_legacy_prune_and_touches_nothing_new(self, tmp_path, monkeypatch):
        """OFF-профиль не меняется ни в чём (бит-в-бит прежнее поведение)."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_off(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        _install_usage(monkeypatch, _plenty)
        for i in range(3):
            _plain_dir(data_dir / "backups", f"auto_backup_20200101_00000{i}")
        snapshot_like = _plain_dir(data_dir / "backups", "snapshot_20200101_000000")

        result = _mgr(store, max_copies=2).check_and_backup()

        assert result["backed_up"] is True
        assert result.get("backup_path", "").find("auto_backup_") != -1
        left = _dirs(data_dir / "backups", "auto_backup_")
        assert len(left) == 2, left
        assert snapshot_like.is_dir()  # retention новых форматов при OFF не вызывается

    def test_pending_restore_blocks_retention_entirely(self, tmp_path, monkeypatch):
        """Ничего не удаляется, пока жив pending-маркер (в т.ч. его снимки)."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        _install_usage(monkeypatch, _plenty)
        for i in range(5):
            _fake_snapshot_dir(
                data_dir, crypto, f"snapshot_prerestore_20200101T00000{i}Z-0000", f"tx-pre-{i}"
            )
        target = _fake_snapshot_dir(data_dir, crypto, "snapshot_20200101_000000", "tx-target")
        pre = data_dir / "backups" / "snapshot_prerestore_20200101T000000Z-0000"
        _live_restore_marker(data_dir, pre_restore=pre, target=target)
        before = _dirs(data_dir / "backups")

        result = snap.prune_snapshot_family(
            backups_root=data_dir / "backups", data_dir=data_dir, max_copies=1
        )

        assert result["removed"] == []
        assert result["skipped_reason"] == snap.REASON_RECOVERY_PENDING
        assert _dirs(data_dir / "backups") == before
        # Каталог, на который ссылается живой маркер, цел — это страховка.
        assert pre.is_dir() and target.is_dir()

    def test_retention_runs_only_after_a_successful_snapshot(self, tmp_path, monkeypatch):
        """Отказ цикла не должен удалять ничего: prune — только ПОСЛЕ успеха."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        for i in range(4):
            _fake_snapshot_dir(data_dir, crypto, f"auto_snapshot_20200101_00000{i}", f"tx-o-{i}")
        before = _dirs(data_dir / "backups", "auto_snapshot_")
        _install_usage(monkeypatch, _nothing)  # место кончилось → снимок не создастся

        result = _mgr(store, max_copies=1).check_and_backup()

        assert result["backed_up"] is False
        assert result["skipped_reason"] == snap.REASON_INSUFFICIENT_SPACE
        assert _dirs(data_dir / "backups", "auto_snapshot_") == before

    def test_prune_leaves_no_temp_dirs_and_counters_agree(self, tmp_path, monkeypatch):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        _install_usage(monkeypatch, _plenty)
        for i in range(3):
            _fake_snapshot_dir(data_dir, crypto, f"auto_snapshot_20200101_00000{i}", f"tx-o-{i}")

        mgr = _mgr(store, max_copies=2)
        mgr.check_and_backup()
        status = mgr.get_auto_backup_status()

        backups_root = data_dir / "backups"
        assert sorted(p.name for p in backups_root.iterdir() if p.is_dir() and p.name.startswith(".")) == [
            ".staging"
        ]
        assert status["encrypted_snapshots"] == len(_dirs(backups_root, "auto_snapshot_"))
        assert status["max_copies"] == 2
