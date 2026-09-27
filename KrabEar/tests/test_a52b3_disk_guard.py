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
import threading
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import auto_backup as auto_backup_mod
from backend import encrypted_snapshot as snap
from backend.encrypted_snapshot import SnapshotOperationRefused
from backend.history_crypto import HistoryCrypto
from backend.state_store import HISTORY_JOURNAL_FILENAMES, StateStore, history_flock

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


def _read_ndjson_lines(path: Path) -> list[str]:
    text = path.read_text("utf-8")
    if text == "":
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


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


def _without_stamp(report: dict | None) -> dict | None:
    """Отчёт без `checked_at` (метка времени вызова, а не состояние)."""
    if report is None:
        return None
    return {k: v for k, v in report.items() if k != "checked_at"}


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
    raise OSError("disk usage недоступен")


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

    def test_estimate_is_byte_exact_for_every_shape_of_set(self, tmp_path):
        """Оценка обязана СОВПАДАТЬ с реально записанным снимком (NIT-2, измерение).

        Никакого «коэффициента на глаз»: если оценка разойдётся с `_encrypt_journal`
        хоть на байт, гард либо врёт об отказе, либо пропускает ENOSPC. Проверяются
        все формы набора: смесь ENC1/plaintext, пустые строки, отсутствующие
        журналы, файл без хвостового перевода строки.
        """
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        _seed(_store_with_crypto(data_dir, crypto), ["одна", "две"])
        # Смешанный набор: дописываем plaintext-строку и пустую строку вручную.
        history = data_dir / "history.ndjson"
        history.write_bytes(
            history.read_bytes()
            + json.dumps({"id": "plain-1", "text": "открытая строка"}, ensure_ascii=False).encode("utf-8")
            + b"\n"
            + b"\n"
        )
        # Журнал без хвостового перевода строки + «микро»-строки (худший случай
        # роста: база64 фиксированных 28 байт на строку).
        (data_dir / "history_tags.ndjson").write_text(
            '{"id":"t1"}\n{"id":"t2"}', encoding="utf-8"
        )

        estimate = snap.estimate_snapshot_bytes(directory=data_dir)
        result = snap.create_encrypted_snapshot(
            data_dir=data_dir,
            backup_dir=data_dir / "backups" / "snapshot_est",
            crypto=crypto,
            transaction_id="tx-est",
            policy_on=True,
        )

        assert estimate["bytes"] == result["size_bytes"]
        assert estimate["plaintext_lines"] >= 3  # дописанная + пустая + tags
        assert estimate["enc1_lines"] >= 1

    def test_fully_encrypted_set_needs_its_own_size_not_twice(self, tmp_path, monkeypatch):
        """NIT-2: ENC1-строки переносятся байт-в-байт — места нужно ровно 1.0×.

        Прежний порог 2.1× отказывал при 1.05/1.2/1.5/2.0× свободного места, то
        есть блокировал бэкапы в нормальном состоянии диска (проба ревьюера).
        """
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        _seed(_store_with_crypto(data_dir, crypto), [f"запись-{i} " + "текст " * 50 for i in range(300)])
        journal_bytes = _journal_bytes(data_dir)
        need = snap.required_bytes(
            journal_bytes=snap.estimate_snapshot_bytes(directory=data_dir)["bytes"], copies=1
        )
        # Запас 10% — и никаких «2.1× от размера журналов».
        assert need < int(journal_bytes * 1.2)
        free = int(journal_bytes * 1.2)

        _install_usage(monkeypatch, lambda _p: free)
        result = snap.create_encrypted_snapshot(
            data_dir=data_dir,
            backup_dir=data_dir / "backups" / "snapshot_1",
            crypto=crypto,
            transaction_id="tx-a52b3-enc1-fit",
            policy_on=True,
        )

        assert result["state"] == snap.STATE_COMMITTED

    def test_mixed_set_needs_more_than_encrypted_one(self, tmp_path, monkeypatch):
        """NIT-2: смешанный/открытый набор действительно дороже — и это видно в отказе."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, [f"запись-{i} " + "текст " * 50 for i in range(300)])
        # Половину history.ndjson делаем открытой (как недомигрированный пробор).
        history = data_dir / "history.ndjson"
        lines = _read_ndjson_lines(history)
        history.write_text(
            "\n".join([crypto.decrypt_line(ln) for ln in lines[: len(lines) // 2]]) + "\n",
            encoding="utf-8",
        )
        journal_bytes = _journal_bytes(data_dir)  # ПОСЛЕ правки: иначе доля «обманывает»
        mixed_need = snap.required_bytes(
            journal_bytes=snap.estimate_snapshot_bytes(directory=data_dir)["bytes"], copies=1
        )
        free = int(journal_bytes * 1.2)
        # Рост настоящий, а не «оценка на глаз»: смешанному набору нужно больше
        # места, чем занимает он сам (и больше, чем ENC1-профилю на том же диске).
        assert snap.estimate_snapshot_bytes(directory=data_dir)["bytes"] > journal_bytes
        assert mixed_need > free
        _install_usage(monkeypatch, lambda _p: free)

        with pytest.raises(SnapshotOperationRefused) as exc:
            snap.create_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=data_dir / "backups" / "snapshot_1",
                crypto=crypto,
                transaction_id="tx-a52b3-mixed",
                policy_on=True,
            )

        assert exc.value.reason == snap.REASON_INSUFFICIENT_SPACE
        # Отказ называет РЕАЛЬНОЕ требуемое число, а не «×2.1 от журналов».
        assert str(mixed_need) in str(exc.value)
        assert "65536" not in str(exc.value)  # пол не выдаётся за требование

    def test_refusal_separates_garage_floor_from_real_requirement(self, tmp_path, monkeypatch):
        """NIT-2: пол 64 КБ — это пол, а не «потребность»; текст обязан это сказать."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        _seed(_store_with_crypto(data_dir, crypto), ["одна"])
        real = snap.required_bytes(
            journal_bytes=snap.estimate_snapshot_bytes(directory=data_dir)["bytes"], copies=1
        )
        assert real == snap.DISK_GUARD_MIN_REQUIRED_BYTES  # набор крошечный → сработал пол
        _install_usage(monkeypatch, lambda _p: 1024)

        with pytest.raises(SnapshotOperationRefused) as exc:
            snap.create_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=data_dir / "backups" / "snapshot_1",
                crypto=crypto,
                transaction_id="tx-a52b3-floor",
                policy_on=True,
            )

        message = str(exc.value)
        assert "пол гарда" in message.lower()
        assert str(snap.DISK_GUARD_MIN_REQUIRED_BYTES) in message
        # И сразу виден реальный расчёт по набору, а не только пол.
        assert f"реальный расчёт по набору — {int(snap.estimate_snapshot_bytes(directory=data_dir)['bytes'] * snap.DISK_GUARD_SAFETY_FACTOR)} байт" in message

    def test_safety_margin_is_ten_percent_not_a_multiple(self, tmp_path, monkeypatch):
        """Граница запаса: ровно need — проходит, need-1 — отказ (сравнение «<»)."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        _seed(_store_with_crypto(data_dir, crypto), [f"запись-{i} " + "текст " * 50 for i in range(300)])
        bundle = snap.estimate_snapshot_bytes(directory=data_dir)["bytes"]
        need = snap.required_bytes(journal_bytes=bundle, copies=1)
        assert need == pytest.approx(bundle * snap.DISK_GUARD_SAFETY_FACTOR, rel=0.01)

        _install_usage(monkeypatch, lambda _p: need - 1)
        with pytest.raises(SnapshotOperationRefused):
            snap.create_encrypted_snapshot(
                data_dir=data_dir,
                backup_dir=data_dir / "backups" / "snapshot_1",
                crypto=crypto,
                transaction_id="tx-a52b3-edge",
                policy_on=True,
            )

        _install_usage(monkeypatch, lambda _p: need)
        result = snap.create_encrypted_snapshot(
            data_dir=data_dir,
            backup_dir=data_dir / "backups" / "snapshot_2",
            crypto=crypto,
            transaction_id="tx-a52b3-edge-ok",
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

    def test_manual_snapshots_have_own_budget_not_max_copies(self, tmp_path, monkeypatch):
        """Продуктовое решение владельца: у ручных снимков СВОЙ бюджет.

        `max_copies` описывает авто-цикл; применять его к ручным бэкапам значило бы
        тихо менять ручное хранение вслед за настройкой авто (семантическая
        перегрузка). Ручная семья живёт по `max(3 × max_copies, 21)`.
        """
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        _install_usage(monkeypatch, _plenty)
        for i in range(5):
            _fake_snapshot_dir(data_dir, crypto, f"snapshot_20200101_00000{i}", f"tx-man-{i}")
        for i in range(4):
            _fake_snapshot_dir(data_dir, crypto, f"auto_snapshot_20200101_00000{i}", f"tx-auto-{i}")

        _mgr(store, max_copies=1).check_and_backup()

        # Ручные целы все (бюджет 21 при max_copies=1), авто-семья порезана до 1 + новая.
        manual = _family(data_dir / "backups", "manual")
        assert len(manual) == 5, manual
        auto = _family(data_dir / "backups", "auto")
        # окно keep = max_copies и ВКЛЮЧАЕТ только что созданный снимок
        assert len(auto) == 1, auto
        assert auto[0].startswith("auto_snapshot_2026") and "20200101" not in auto[0]

    def test_manual_budget_formula(self):
        """Формула бюджета ручных снимок закреплена тестом (пол 21, кратно 3)."""
        assert snap.manual_snapshot_keep(max_copies=0) == 21
        assert snap.manual_snapshot_keep(max_copies=1) == 21
        assert snap.manual_snapshot_keep(max_copies=7) == 21
        assert snap.manual_snapshot_keep(max_copies=10) == 30
        assert snap.manual_snapshot_keep(max_copies=100) == 300
        # Авто-лимит и ручной не связаны ни в одну сторону.
        assert snap.manual_snapshot_keep(max_copies=2) != 2

    def test_manual_budget_is_enforced(self, tmp_path, monkeypatch):
        """Бюджет применяется, а не только объявлен: 23 ручных → 21."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        _install_usage(monkeypatch, _plenty)
        # Каталоги-заглушки с манифестом: кандидат Retention'а определяется именем
        # и наличием манифеста (содержимое он намеренно не читает — счёт дешёвый),
        # поэтому для проверки БЮДЖЕТА реальные снимки не нужны.
        for i in range(23):
            stub = data_dir / "backups" / f"snapshot_20260101_{i:06d}"
            stub.mkdir(parents=True)
            (stub / snap.SNAPSHOT_MANIFEST_FILENAME).write_text("{}", encoding="utf-8")

        snap.prune_snapshot_family(
            backups_root=data_dir / "backups", data_dir=data_dir, max_copies=2
        )

        left = _family(data_dir / "backups", "manual")
        assert len(left) == snap.manual_snapshot_keep(max_copies=2) == 21, len(left)
        assert left[0] == "snapshot_20260101_000002"  # два самых старых удалены
        assert left[-1] == "snapshot_20260101_000022"  # самый новый цел

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


# ---------------------------------------------------------------------------
# Task 3 — наблюдаемость: причина пропуска + свободное место
# ---------------------------------------------------------------------------


class TestSpaceObservability:
    def test_status_shows_reason_and_free_space_after_space_refusal(self, tmp_path, monkeypatch):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        free = 4096
        seen = _install_usage(monkeypatch, lambda _p: free)

        mgr = _mgr(store, max_copies=2)
        result = mgr.check_and_backup()
        status = mgr.get_auto_backup_status()

        assert result["backed_up"] is False
        assert result["skipped_reason"] == snap.REASON_INSUFFICIENT_SPACE
        assert status["skipped_reason"] == snap.REASON_INSUFFICIENT_SPACE
        assert status["last_refusal_reason"] == snap.REASON_INSUFFICIENT_SPACE
        assert status["encryption_operation_unavailable"] is True
        # Свободное место видно по обоим целевым каталогам.
        assert status["disk_space"]["targets"]["backups"]["free_bytes"] == free
        assert status["disk_space"]["targets"]["data"]["free_bytes"] == free
        assert seen, "статус не спросил про место вовсе"

    def test_refusal_reason_survives_restart_of_manager(self, tmp_path, monkeypatch):
        """Причина переживает новый процесс (sidecar A5.2b1), место — живое."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        _install_usage(monkeypatch, _nothing)
        assert _mgr(store, max_copies=2).check_and_backup()["backed_up"] is False

        _install_usage(monkeypatch, lambda _p: 1 << 40)
        fresh = _mgr(store, max_copies=2).get_auto_backup_status()

        assert fresh["last_refusal_reason"] == snap.REASON_INSUFFICIENT_SPACE
        # Свободное место показывается АКТУАЛЬНОЕ, а не застывшее с прошлого цикла.
        assert fresh["disk_space"]["targets"]["backups"]["free_bytes"] == 1 << 40

    def test_successful_backup_reports_no_false_refusal(self, tmp_path, monkeypatch):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        _install_usage(monkeypatch, _nothing)
        mgr = _mgr(store, max_copies=2)
        mgr.check_and_backup()  # отказ по месту
        _install_usage(monkeypatch, lambda _p: 1 << 40)

        assert mgr.check_and_backup()["backed_up"] is True
        status = mgr.get_auto_backup_status()

        # Успешный бэкап НЕ сообщает ложный отказ (сценарии разведены).
        assert status["skipped_reason"] is None
        assert status["last_refusal_reason"] is None
        assert status["encryption_operation_unavailable"] is False
        assert status["last_backup_kind"] == "encrypted_snapshot"
        # Свободное место при этом остаётся видно: это сведения, а не отказ.
        assert status["disk_space"]["targets"]["data"]["free_bytes"] == 1 << 40

    def test_space_report_covers_both_target_volumes(self, tmp_path, monkeypatch):
        data_dir = _data_dir(tmp_path)
        other_volume = tmp_path / "other_volume"
        other_volume.mkdir()
        (data_dir / "backups").symlink_to(other_volume, target_is_directory=True)
        _install_usage(monkeypatch, _under(other_volume, free=111, fallback=999))

        report = snap.snapshot_space_report(data_dir=data_dir)

        assert report["targets"]["backups"]["free_bytes"] == 111
        assert report["targets"]["data"]["free_bytes"] == 999
        assert report["targets"]["backups"]["path"].endswith("backups")
        assert report["targets"]["backups"]["queried"].startswith(str(other_volume))

    def test_space_report_is_fail_closed_when_filesystem_lies(self, tmp_path, monkeypatch):
        data_dir = _data_dir(tmp_path)
        _install_usage(monkeypatch, _boom)

        report = snap.snapshot_space_report(data_dir=data_dir)

        # «Неизвестно» — это null + имя типа ошибки, а НЕ 0 свободного места.
        for target in report["targets"].values():
            assert target["free_bytes"] is None
            assert target["total_bytes"] is None
            assert target["error"] == "OSError"
        assert report["journals_bytes"] >= 0
        # Требование в отчёте НЕ выдумывается дешёвой арифметикой (NIT-2): его
        # точная величина живёт в тексте отказа, а здесь только дешёвые числа.
        assert "required_bytes_one_copy" not in report

    def test_diagnostics_exposes_space_report(self, tmp_path, monkeypatch):
        """NIT-5: тест ассертит сам (без импорта чужих тестовых модулей)."""
        from backend.health_check_service import disk_space_status

        data_dir = _data_dir(tmp_path)
        _install_usage(monkeypatch, lambda _p: 777)
        diag = _diag_service(data_dir).handle_get_diagnostics({})

        assert "disk_space" in diag
        # checked_at — метка времени ВЫЗОВА, поэтому содержательную часть
        # сравниваем отдельно (приём b2, `_without_stamp`).
        assert _without_stamp(diag["disk_space"]) == _without_stamp(
            disk_space_status(data_dir)
        )
        assert diag["disk_space"]["targets"]["data"]["free_bytes"] == 777
        # Контракт соседнего блока не тронут: restore отдаёт ровно свои 2 поля.
        assert len(diag["restore"]) == 2

    def test_diagnostics_space_never_raises(self, tmp_path, monkeypatch):
        from backend import health_check_service as hcs

        data_dir = _data_dir(tmp_path)
        _install_usage(monkeypatch, _boom)

        report = hcs.disk_space_status(data_dir)

        assert {t["free_bytes"] for t in report["targets"].values()} == {None}

    def test_diagnostics_space_survives_broken_report_builder(self, tmp_path, monkeypatch):
        """Крайний предохранитель: даже поломка отчёта не роняет диагностику."""
        from backend import health_check_service as hcs

        def _boom(*_a, **_k):
            raise RuntimeError("отчёт взорвался")

        monkeypatch.setattr(snap, "snapshot_space_report", _boom)

        report = hcs.disk_space_status(_data_dir(tmp_path))

        assert report["targets"] == {}
        assert report["checked_at"] is None

    def test_reason_code_is_documented_with_protocol_prefix(self):
        doc = (
            Path(__file__).resolve().parents[2] / "docs" / "IPC_API_REFERENCE.md"
        ).read_text(encoding="utf-8")
        assert snap.REASON_INSUFFICIENT_SPACE.startswith("snapshot_")
        assert f"`{snap.REASON_INSUFFICIENT_SPACE}`" in doc


# ---------------------------------------------------------------------------
# NIT-1 — retention сериализован с restore (probe ревьюера p3b + окно без маркера)
# ---------------------------------------------------------------------------


def _restore_staging_dir(data_dir: Path, txid: str = "restore-20260101T000000Z-abcd") -> Path:
    """Приватный staging restore. Без маркера — окно ЧТЕНИЯ снимка (см. ниже)."""
    staging = data_dir / f"{snap.RESTORE_STAGING_PREFIX}{txid}"
    staging.mkdir(parents=True, exist_ok=True)
    return staging


class TestRetentionSerialisedWithRestore:
    """Снимок, который restore читает прямо сейчас, удалять нельзя.

    Окно, о котором указал ревью: `restore_encrypted_snapshot` читает и верифицирует
    снимок ДВАжды (до lock'а и под ним), а маркер пишется только в
    `_apply_verified_snapshot_locked` — то есть уже после чтения. Значит «снимок
    занят restore» нельзя определять по маркеру.
    """

    def test_restore_staging_without_marker_blocks_retention(self, tmp_path, monkeypatch):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        _install_usage(monkeypatch, _plenty)
        for i in range(5):
            _fake_snapshot_dir(data_dir, crypto, f"snapshot_20260101_00000{i}", f"tx-n1-{i}")
        reading = data_dir / "backups" / "snapshot_20260101_000000"  # его читает restore
        _restore_staging_dir(data_dir)  # БЕЗ restore_marker.json
        before = _dirs(data_dir / "backups")

        result = snap.prune_snapshot_family(
            backups_root=data_dir / "backups", data_dir=data_dir, max_copies=1
        )

        assert result["removed"] == []
        assert result["skipped_reason"] == snap.REASON_RECOVERY_PENDING
        assert _dirs(data_dir / "backups") == before
        assert reading.is_dir()

    def test_retention_resumes_once_restore_finished(self, tmp_path, monkeypatch):
        """Регресс нормального поведения: staging убран — retention работает."""
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        _install_usage(monkeypatch, _plenty)
        for i in range(5):
            _fake_snapshot_dir(
                data_dir, crypto, f"auto_snapshot_20260101_00000{i}", f"tx-n1-ok-{i}"
            )
        staging = _restore_staging_dir(data_dir)
        assert (
            snap.prune_snapshot_family(
                backups_root=data_dir / "backups", data_dir=data_dir, max_copies=1
            )["removed"]
            == []
        )
        shutil.rmtree(staging)  # restore завершён, staging убран (как в b2)

        result = snap.prune_snapshot_family(
            backups_root=data_dir / "backups", data_dir=data_dir, max_copies=1
        )

        assert result["removed_count"] == 4
        assert (data_dir / "backups" / "auto_snapshot_20260101_000004").is_dir()
        assert not (data_dir / "backups" / "auto_snapshot_20260101_000000").exists()

    def test_cycle_holds_history_lock_while_pruning(self, tmp_path, monkeypatch):
        """Проба p3b: при УДЕРЖИВАЕМОМ history.lock цикл не удаляет ничего.

        Retention обязан идти под тем же flock'ом, что и restore (`history.lock`),
        иначе окно «restore читает снимок — retention его сносит» остаётся.
        """
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        _install_usage(monkeypatch, _plenty)
        for i in range(4):
            _fake_snapshot_dir(data_dir, crypto, f"auto_snapshot_20260101_00000{i}", f"tx-lock-{i}")
        target = data_dir / "backups" / "auto_snapshot_20260101_000000"
        mgr = _mgr(store, max_copies=1)

        acquired = threading.Event()
        release = threading.Event()
        outcome: dict = {}

        def _holder():
            with history_flock(data_dir):
                acquired.set()
                release.wait(10)

        def _cycle():
            # Именно retention, а не весь цикл: создание снимка и так берёт store-lock,
            # поэтому через check_and_backup() тест проходил бы и без фикса.
            try:
                outcome["result"] = mgr._prune_snapshot_family()
            except BaseException as exc:  # noqa: BLE001 — пробрасываем в тест
                outcome["error"] = exc

        holder = threading.Thread(target=_holder, daemon=True)
        holder.start()
        assert acquired.wait(10), "холдер не взял history.lock"
        worker = threading.Thread(target=_cycle, daemon=True)
        worker.start()
        try:
            # Пока lock держит «restore», цикл обязан стоять на ожидании, а не
            # резать снимки. Даём ему время дойти до flock'а.
            worker.join(0.5)
            assert worker.is_alive(), "цикл прошёл, не дождавшись lock'а"
            assert target.is_dir(), "retention удалил снимок под удерживаемым lock'ом"
            assert "result" not in outcome
        finally:
            release.set()
            holder.join(10)
            worker.join(30)
        assert "error" not in outcome, outcome.get("error")
        assert outcome["result"]["removed_count"] == 3
        # После освобождения lock'а retention отрабатывает полностью.
        assert not target.exists()


class TestRetentionCandidateFilterAndOrder:
    """NIT-3 (манифест вместо имени) и NIT-7 (строгий oldest-first)."""

    def _profile(self, tmp_path, monkeypatch, *, count: int = 4, prefix: str = "auto_snapshot_20260101_00000"):
        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _settings_on(data_dir)
        store = _store_with_crypto(data_dir, crypto)
        _seed(store, ["одна"])
        _install_usage(monkeypatch, _plenty)
        for i in range(count):
            _fake_snapshot_dir(data_dir, crypto, f"{prefix}{i}", f"tx-f-{i}")
        return data_dir, crypto, store

    def test_user_directory_named_like_snapshot_survives(self, tmp_path, monkeypatch):
        """NIT-3 (проба p12): каталог владельца с именем `snapshot_*` — не снимок.

        `classify_backup_dir` честно называл такой каталог `unsupported`, а
        retention его удалял: имя совпадало, манифеста не было.

        Настоящих ручных снимок больше бюджета (21), иначе «каталог уцелел» ничего
        не доказывало бы — ничего бы не удалялось вовсе.
        """
        data_dir, crypto_obj, _store = self._profile(
            tmp_path, monkeypatch, count=24, prefix="snapshot_20260101_00000"
        )
        assert snap.manual_snapshot_keep(1) < 24
        owner_dir = data_dir / "backups" / "snapshot_20260101_000000_МОИ_ЗАМЕТКИ"
        owner_dir.mkdir()
        (owner_dir / "readme.md").write_text("важно", encoding="utf-8")
        assert snap.classify_backup_dir(owner_dir) == "unsupported"

        result = snap.prune_snapshot_family(
            backups_root=data_dir / "backups", data_dir=data_dir, max_copies=1
        )

        # Рядом с каталогом владельца удаляются настоящие снимки (фильтр работает),
        # а сам каталог — нет.
        assert result["removed_count"] == 3, result["removed"]
        assert owner_dir.is_dir(), "каталог владельца удалён retention'ом"
        assert str(owner_dir) not in result["removed"]

    def test_real_snapshot_is_still_a_candidate(self, tmp_path, monkeypatch):
        """Обратная сторона NIT-3: настоящий снимок (с манифестом) режется как надо."""
        data_dir, _crypto_obj, _store = self._profile(tmp_path, monkeypatch)

        result = snap.prune_snapshot_family(
            backups_root=data_dir / "backups", data_dir=data_dir, max_copies=1
        )

        assert result["removed_count"] == 3
        assert (data_dir / "backups" / "auto_snapshot_20260101_000003").is_dir()

    def test_failed_removal_does_not_delete_newer_instead(self, tmp_path, monkeypatch):
        """NIT-7 (проба p10b): сбой rmtree на самом старом — СТОП, а не «удалить следующий».

        Обе вариации оставляют больше данных, чем лимит, но порядок «старые → новые»
        должен быть строгим: иначе битый старый снимок живёт вечно, а свежие
        исчезают по одному.
        """
        data_dir, _crypto_obj, _store = self._profile(tmp_path, monkeypatch)
        oldest = data_dir / "backups" / "auto_snapshot_20260101_000000"
        real_rmtree = shutil.rmtree

        def _flaky(target, *args, **kwargs):
            if Path(target) == oldest:
                raise OSError(errno.EACCES, "имитация сбоя удаления")
            return real_rmtree(target, *args, **kwargs)

        monkeypatch.setattr(snap.shutil, "rmtree", _flaky)

        result = snap.prune_snapshot_family(
            backups_root=data_dir / "backups", data_dir=data_dir, max_copies=2
        )

        assert result["removed"] == [], "свежие снимки удалены вместо битого старого"
        assert oldest.is_dir()
        for i in range(4):
            assert (data_dir / "backups" / f"auto_snapshot_20260101_00000{i}").is_dir()


# ---------------------------------------------------------------------------
# NIT-5: локальные фейки для HealthCheckService (без межмодульных импортов)
# ---------------------------------------------------------------------------


class _DiagStore:
    """Минимальный StateStore для get_diagnostics."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def count_active_items(self, lock_timeout_sec=None, nowait: bool = False) -> int:
        return 3


class _DiagSettings:
    _cache_ttl = 5
    _cache: dict = {}

    def cached_settings(self, nowait: bool = False) -> dict:
        return {}


def _diag_service(data_dir: Path):
    """HealthCheckService с пустыми коллабораторами (все опциональны)."""
    from backend.health_check_service import HealthCheckService

    return HealthCheckService(
        store=_DiagStore(data_dir),
        health_checker=None,
        startup_diagnostics=None,
        integrity_checker=None,
        llm_probe=None,
        metrics_collector=None,
        transcriber=None,
        llm_rewriter=None,
        settings_svc=_DiagSettings(),
        start_time=0.0,
        app_version="a52b3-test",
        recorder=None,
        last_stt_engine_ref=["mlx-whisper"],
    )
