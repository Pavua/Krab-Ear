"""A5.3 карточка A, slice 1 — типизированный policy snapshot и центральный commit.

Контракт: спека ``docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md``
§7.1–7.3 + ``A53_STRONG_MODEL_HANDOFF.md`` п.3, 4, 8, 9, 10, 13, 14.

Что здесь проверяется (и только это — grants/receipts/epoch НЕ входят в slice 1):
  * ``StateStore.read_plaintext_policy_snapshot()`` отдаёт ТИПИЗИРОВАННЫЙ снимок:
    ``KNOWN_OFF`` только для явно валидного ``false``, ``KNOWN_ON`` только для
    явно валидного ``true``, всё остальное — ``UNKNOWN`` с машинной причиной.
    Отсутствующий ``settings.json`` НЕ «свежий профиль OFF».
  * UNKNOWN-триггеры §7.1 (corrupt/non-object/duplicate keys/missing key/
    non-bool/missing+invalid revision/non-regular/oversize/unstable) —
    ни один не бросает исключение наружу.
  * fingerprint из §7.3 стабилен при повторном чтении и меняется после
    atomic replace тех же bool с новой ревизией.
  * центральный commit §7.3: ревизия генерируется на КАЖДОЙ поддержанной
    записи, входное значение игнорируется, reject-путь не трогает файл байтом.

Все проверки поведенческие: без AST/source-inspection, без дубликата
реализации, без реальной истории/ENC1/Keychain/шифрования, без multiprocessing
(последнее — slice 2/3). OFF-профиль — временный каталог.
"""

from __future__ import annotations

import contextlib
import functools
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

from backend.ipc_errors import IpcOperationalError
from backend.models import DEFAULT_SETTINGS
from backend.plaintext_export_authorization import (
    MAX_POLICY_BYTES,
    POLICY_REVISION_KEY,
    REASON_UNKNOWN_DUPLICATE_KEYS,
    REASON_UNKNOWN_INVALID_REVISION,
    REASON_UNKNOWN_MISSING_KEY,
    REASON_UNKNOWN_MISSING_REVISION,
    REASON_UNKNOWN_MISSING_SETTINGS,
    REASON_UNKNOWN_NON_BOOL,
    REASON_UNKNOWN_NON_REGULAR,
    REASON_UNKNOWN_NOT_OBJECT,
    REASON_UNKNOWN_OVERSIZE,
    REASON_UNKNOWN_PROVIDER_ERROR,
    REASON_UNKNOWN_UNREADABLE,
    REASON_UNKNOWN_UNSTABLE,
    PolicyState,
)
from backend.state_store import (
    STARTUP_POLICY_ALREADY_KNOWN,
    STARTUP_POLICY_INITIALIZED,
    STARTUP_POLICY_LEFT_UNKNOWN,
    STARTUP_POLICY_MIGRATED_LEGACY,
    StateStore,
    StateStoreLockTimeout,
    StateStoreLockUpgradeError,
    StateStoreSettingsCorruptError,
)

ENCRYPTION_KEY = "history_encryption_enabled"
PRIVACY_KEY = "privacy_mode_enabled"

_OMIT = object()

#: Валидная по контракту ревизия = ``uuid4().hex``: ровно 32 lowercase-hex символа
#: (единственный формат, который пишет центральный commit).
VALID_REVISION = "0123456789abcdef0123456789abcdef"
VALID_REVISION_ALT = "fedcba9876543210fedcba9876543210"


def _profile(
    encryption: object = False,
    privacy: object = False,
    revision: object = VALID_REVISION,
) -> dict:
    """Валидный по форме payload settings.json (bool-флаги + ревизия).

    ``revision=_OMIT`` убирает ключ целиком; явный ``None`` кладёт JSON ``null``.
    """
    payload: dict = {ENCRYPTION_KEY: encryption, PRIVACY_KEY: privacy}
    if revision is not _OMIT:
        payload[POLICY_REVISION_KEY] = revision
    return payload


def _seed_known_profile(
    data_dir: Path,
    *,
    encryption: bool = False,
    privacy: bool = False,
    revision: object = VALID_REVISION,
) -> None:
    """Кладёт SUPPORTED-профиль (оба exact-bool + валидная ревизия) на диск.

    Единственный источник «валидных» байтов профиля для тестов записи: писать
    на отсутствующий settings.json у существующего каталога запрещено
    долговечным guard'ом, и без явного сида такой тест проверял бы не свой
    инвариант, а отказ.
    """
    (data_dir / "settings.json").write_text(
        json.dumps(_profile(encryption, privacy, revision)),
        encoding="utf-8",
    )


class _PolicySnapshotFixture(unittest.TestCase):
    """Общий fixtures: временный профиль без шифрования и без реальной истории."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="a53-policy-")
        self.data_dir = Path(self._tmp.name)
        self.store = StateStore(self.data_dir)

    def tearDown(self) -> None:
        self.store = None
        self._tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────────

    def write_raw_settings(self, raw: bytes) -> Path:
        self.settings_path = self.data_dir / "settings.json"
        self.settings_path.write_bytes(raw)
        return self.settings_path

    def write_settings(self, payload: dict) -> Path:
        return self.write_raw_settings(json.dumps(payload).encode("utf-8"))

    def drop_settings(self) -> Path:
        path = self.data_dir / "settings.json"
        if path.exists() or path.is_symlink():
            path.unlink()
        return path

    def assert_unknown(self, snapshot, reason: str) -> None:
        """UNKNOWN-снимок: причина машинная, состояние не кодируется bool-флагом."""
        self.assertIs(snapshot.state, PolicyState.UNKNOWN, snapshot)
        self.assertEqual(snapshot.reason, reason, snapshot)
        self.assertIsNone(snapshot.privacy_mode_enabled, snapshot)
        self.assertIsNone(snapshot.internal_revision, snapshot)
        self.assertIsNone(snapshot.fingerprint, snapshot)

    def seed_known_profile(
        self,
        *,
        encryption: bool = False,
        privacy: bool = False,
        revision: object = VALID_REVISION,
    ) -> None:
        """Готовит SUPPORTED-профиль (оба exact-bool + валидная ревизия)."""
        _seed_known_profile(
            self.data_dir,
            encryption=encryption,
            privacy=privacy,
            revision=revision,
        )


class TestPolicySnapshotUnknownTriggers(_PolicySnapshotFixture):
    """RED-кейсы §7.1: каждый триггер обязан дать UNKNOWN, а не исключение."""

    def test_missing_settings_file_is_unknown_not_fresh_profile_off(self):
        """Отсутствующий settings.json НЕ «свежий профиль OFF» (контракт п.4)."""
        self.drop_settings()
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assert_unknown(snapshot, REASON_UNKNOWN_MISSING_SETTINGS)

    def test_corrupt_json_is_unknown(self):
        self.write_raw_settings(b'{"history_encryption_enabled": false,')
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assert_unknown(snapshot, REASON_UNKNOWN_UNREADABLE)

    def test_not_an_object_is_unknown(self):
        self.write_raw_settings(b"[1, 2, 3]")
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assert_unknown(snapshot, REASON_UNKNOWN_NOT_OBJECT)

    def test_duplicate_policy_keys_are_unknown(self):
        """Дубли policy-ключей при разборе → UNKNOWN (ambiguous source of truth)."""
        self.write_raw_settings(
            b'{"history_encryption_enabled": false, "privacy_mode_enabled": false, '
            b'"history_encryption_enabled": true, '
            b'"_plaintext_export_policy_revision": "rev-dup"}'
        )
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assert_unknown(snapshot, REASON_UNKNOWN_DUPLICATE_KEYS)

    def test_missing_privacy_key_is_unknown(self):
        payload = {ENCRYPTION_KEY: False, POLICY_REVISION_KEY: "rev-no-privacy"}
        self.write_settings(payload)
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assert_unknown(snapshot, REASON_UNKNOWN_MISSING_KEY)

    def test_missing_encryption_key_is_unknown(self):
        payload = {PRIVACY_KEY: False, POLICY_REVISION_KEY: "rev-no-encryption"}
        self.write_settings(payload)
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assert_unknown(snapshot, REASON_UNKNOWN_MISSING_KEY)

    def test_non_bool_flags_are_unknown(self):
        """`1`/`"true"`/`None` — не exact-bool: truthy-интерпретация запрещена."""
        cases = [
            (0, False),
            (1, False),
            ("true", False),
            (None, False),
            (False, 0),
            (False, 1),
            (False, "true"),
            (False, None),
        ]
        for encryption, privacy in cases:
            with self.subTest(encryption=encryption, privacy=privacy):
                self.write_settings(_profile(encryption, privacy))
                snapshot = self.store.read_plaintext_policy_snapshot()
                self.assert_unknown(snapshot, REASON_UNKNOWN_NON_BOOL)

    def test_missing_revision_is_unknown(self):
        self.write_settings(_profile(revision=_OMIT))
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assert_unknown(snapshot, REASON_UNKNOWN_MISSING_REVISION)

    def test_invalid_revision_is_unknown(self):
        for revision in ("", 123, None, True, ["rev"]):
            with self.subTest(revision=revision):
                self.write_settings(_profile(revision=revision))
                snapshot = self.store.read_plaintext_policy_snapshot()
                self.assert_unknown(snapshot, REASON_UNKNOWN_INVALID_REVISION)

    def test_fifo_settings_path_is_unknown_without_hanging(self):
        """FIFO на пути settings.json: open O_NONBLOCK + fstat → UNKNOWN, без зависания."""
        path = self.drop_settings()
        os.mkfifo(path)
        snapshot = self._read_in_bounded_thread()
        self.assert_unknown(snapshot, REASON_UNKNOWN_NON_REGULAR)

    def test_directory_settings_path_is_unknown(self):
        path = self.data_dir / "settings.json"
        path.mkdir()
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assert_unknown(snapshot, REASON_UNKNOWN_NON_REGULAR)

    def test_oversize_settings_is_unknown_without_reading_it_all(self):
        """st_size > 16 MiB → UNKNOWN до чтения содержимого (только sparse-файл)."""
        path = self.write_settings(_profile())
        with path.open("r+b") as fh:
            fh.truncate(MAX_POLICY_BYTES + 1)
        self.assertGreater(path.stat().st_size, MAX_POLICY_BYTES)
        snapshot = self._read_in_bounded_thread()
        self.assert_unknown(snapshot, REASON_UNKNOWN_OVERSIZE)

    def test_unstable_post_read_fstat_is_unknown(self):
        """Подмена файла между fstat-до и fstat-после чтения → UNSTABLE, без retry."""
        path = self.write_settings(_profile())
        target_ino = path.stat().st_ino
        real_fstat = os.fstat
        calls = {"n": 0}

        def flaky_fstat(fd):
            st = real_fstat(fd)
            if st.st_ino == target_ino:
                calls["n"] += 1
                if calls["n"] == 2:
                    return _perturbed_stat(st, st_size=st.st_size + 1)
            return st

        with mock.patch("os.fstat", side_effect=flaky_fstat):
            snapshot = self.store.read_plaintext_policy_snapshot()
        self.assertEqual(calls["n"], 2, "fstat до и после чтения обязаны быть")
        self.assert_unknown(snapshot, REASON_UNKNOWN_UNSTABLE)

    def test_unstable_final_lstat_is_unknown(self):
        """Финальный lstat не совпал с пред-чтением → UNSTABLE (замена после чтения)."""
        path = self.write_settings(_profile())
        real_lstat = os.lstat

        def flaky_lstat(target, *args, **kwargs):
            st = real_lstat(target, *args, **kwargs)
            if os.fspath(target) == os.fspath(path):
                return _perturbed_stat(st, st_mtime_ns=st.st_mtime_ns + 1_000_000)
            return st

        with mock.patch("os.lstat", side_effect=flaky_lstat):
            snapshot = self.store.read_plaintext_policy_snapshot()
        self.assert_unknown(snapshot, REASON_UNKNOWN_UNSTABLE)

    def test_unreadable_settings_is_unknown(self):
        """OSError на пути (нет прав) → UNREADABLE, а не KNOWN_OFF и не исключение."""
        if os.geteuid() == 0:
            self.skipTest("root обходит биты доступа — кейс не воспроизводим")
        path = self.write_settings(_profile())
        os.chmod(path, 0o000)
        try:
            snapshot = self.store.read_plaintext_policy_snapshot()
        finally:
            os.chmod(path, 0o600)
        self.assert_unknown(snapshot, REASON_UNKNOWN_UNREADABLE)

    # ── bounded-read helper ────────────────────────────────────────────────

    def _read_in_bounded_thread(self):
        """Читает snapshot в daemon-треде: зависание = провал теста, а не hung CI."""
        box: dict = {}

        def runner():
            try:
                box["snapshot"] = self.store.read_plaintext_policy_snapshot()
            except BaseException as exc:  # noqa: BLE001 — тест должен увидеть причину
                box["error"] = exc

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join(timeout=10.0)
        if thread.is_alive():
            self.fail("read_plaintext_policy_snapshot() завис на нерегулярном/oversize пути")
        if "error" in box:
            raise box["error"]
        return box["snapshot"]


def _perturbed_stat(st, **overrides):
    """Копия ``os.stat_result`` с изменёнными полями (fault injection для тестов)."""
    fake = types.SimpleNamespace()
    for attr in dir(st):
        if attr.startswith("st_"):
            setattr(fake, attr, getattr(st, attr))
    for name, value in overrides.items():
        setattr(fake, name, value)
    return fake


class TestPolicySnapshotKnownStates(_PolicySnapshotFixture):
    """GREEN-кейсы: только явно валидные bool дают KNOWN_OFF/KNOWN_ON."""

    def test_valid_off_profile_is_known_off(self):
        for privacy in (False, True):
            with self.subTest(privacy=privacy):
                self.write_settings(_profile(False, privacy))
                snapshot = self.store.read_plaintext_policy_snapshot()
                self.assertIs(snapshot.state, PolicyState.KNOWN_OFF, snapshot)
                self.assertIsNone(snapshot.reason, snapshot)
                self.assertIs(snapshot.privacy_mode_enabled, privacy, snapshot)
                self.assertEqual(snapshot.internal_revision, VALID_REVISION, snapshot)
                self.assertIsNotNone(snapshot.fingerprint, snapshot)

    def test_valid_on_profile_is_known_on(self):
        self.write_settings(_profile(True, False))
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, PolicyState.KNOWN_ON, snapshot)
        self.assertIsNone(snapshot.reason, snapshot)
        self.assertIs(snapshot.privacy_mode_enabled, False, snapshot)
        self.assertIsNotNone(snapshot.fingerprint, snapshot)

    def test_fingerprint_covers_every_contract_field(self):
        """Fingerprint = профиль + dev/ino/size/mtime/ctime + SHA256 + ревизия."""
        path = self.write_settings(_profile(False, False, VALID_REVISION_ALT))
        snapshot = self.store.read_plaintext_policy_snapshot()
        fp = snapshot.fingerprint
        self.assertIsNotNone(fp)
        st = path.stat()
        self.assertEqual(fp.profile_identity, str(self.data_dir.resolve()))
        self.assertEqual(fp.st_dev, st.st_dev)
        self.assertEqual(fp.st_ino, st.st_ino)
        self.assertEqual(fp.st_size, st.st_size)
        self.assertEqual(fp.st_mtime_ns, st.st_mtime_ns)
        self.assertEqual(fp.st_ctime_ns, st.st_ctime_ns)
        self.assertEqual(fp.internal_revision, VALID_REVISION_ALT)
        self.assertEqual(len(fp.content_sha256), 64)
        # profile_identity — путь профиля (НЕ хеш секрета и НЕ содержимое файла).
        # Смысловая проверка: identity выводится из пути, поэтому совпадает с
        # НЕзависимо вычисленным путём, а не с произвольной строкой.
        self.assertEqual(fp.profile_identity, str(Path(self.data_dir).resolve()))
        self.assertTrue(Path(fp.profile_identity).is_absolute())

    def test_profile_identity_resolves_symlinked_data_dir(self):
        """Один и тот же профиль через symlink-путь даёт ТОТ ЖЕ identity.

        На macOS TemporaryDirectory живёт под /var → /private/var; без resolve()
        один профиль имел бы два разных identity, и валидный grant, выданный
        до перезапуска, отвалился бы только из-за пути.
        """
        real_dir = self.data_dir
        link_dir = real_dir.parent / f"{real_dir.name}-link"
        self.addCleanup(lambda: link_dir.unlink(missing_ok=True))
        os.symlink(real_dir, link_dir)
        via_link = StateStore(link_dir)
        self.write_settings(_profile(False, False, VALID_REVISION))
        first = self.store.read_plaintext_policy_snapshot()
        second = via_link.read_plaintext_policy_snapshot()
        self.assertEqual(
            first.fingerprint.profile_identity,
            second.fingerprint.profile_identity,
        )
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_fingerprint_is_stable_across_repeated_reads(self):
        self.write_settings(_profile(False, False))
        first = self.store.read_plaintext_policy_snapshot()
        second = self.store.read_plaintext_policy_snapshot()
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_fingerprint_changes_after_atomic_replace_with_same_bools(self):
        """Same-bool atomic replacement консервативно меняет fingerprint (§7.3)."""
        self.write_settings(_profile(False, False, VALID_REVISION))
        before = self.store.read_plaintext_policy_snapshot()
        self.store.save_settings({ENCRYPTION_KEY: False, PRIVACY_KEY: False})
        after = self.store.read_plaintext_policy_snapshot()
        self.assertEqual(before.state, PolicyState.KNOWN_OFF)
        self.assertEqual(after.state, PolicyState.KNOWN_OFF)
        self.assertNotEqual(before.internal_revision, after.internal_revision)
        self.assertNotEqual(before.fingerprint, after.fingerprint)


class TestCentralSettingsCommit(_PolicySnapshotFixture):
    """§7.3 / контракт п.9–10: ревизия живёт в центральном settings commit."""

    def test_every_supported_save_generates_new_revision(self):
        """Даже при тех же значениях флагов ревизия новая на КАЖДОЙ записи."""
        # SUPPORTED-профиль: долговечный guard отказывает на отсутствующем
        # settings.json у существующего каталога, поэтому инвариант «ревизия
        # минтится на каждой поддержанной записи» проверяется на валидном
        # профиле (сам отказ — отдельный тест в TestCreatedDataDir...).
        self.seed_known_profile()
        revisions = []
        for _ in range(3):
            saved = self.store.save_settings(
                {ENCRYPTION_KEY: False, PRIVACY_KEY: False}
            )
            revisions.append(saved[POLICY_REVISION_KEY])
        self.assertEqual(len(set(revisions)), 3, revisions)
        for revision in revisions:
            self.assertEqual(uuid.UUID(hex=revision).version, 4)
        # Та же ревизия обязана быть видна в snapshot, прочитанном с диска.
        on_disk = json.loads(
            (self.data_dir / "settings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(on_disk[POLICY_REVISION_KEY], revisions[-1])
        self.assertEqual(
            self.store.read_plaintext_policy_snapshot().internal_revision,
            revisions[-1],
        )

    def test_incoming_revision_is_never_persisted(self):
        """Входное/backup-значение ревизии игнорируется (контракт п.9)."""
        self.seed_known_profile()
        hostile = "0" * 32
        saved = self.store.save_settings(
            {ENCRYPTION_KEY: False, PRIVACY_KEY: False, POLICY_REVISION_KEY: hostile}
        )
        self.assertNotEqual(saved[POLICY_REVISION_KEY], hostile)
        on_disk = json.loads(
            (self.data_dir / "settings.json").read_text(encoding="utf-8")
        )
        self.assertNotEqual(on_disk[POLICY_REVISION_KEY], hostile)
        self.assertEqual(
            self.store.read_plaintext_policy_snapshot().internal_revision,
            on_disk[POLICY_REVISION_KEY],
        )

    def test_on_off_on_transitions_each_rotate_revision(self):
        """ON→OFF→ON между процессами ловится новой ревизией каждого commit."""
        self.seed_known_profile()
        seen = []
        for encryption in (False, True, False):
            saved = self.store.save_settings(
                {ENCRYPTION_KEY: encryption, PRIVACY_KEY: False}
            )
            seen.append(saved[POLICY_REVISION_KEY])
            snapshot = self.store.read_plaintext_policy_snapshot()
            self.assertIs(
                snapshot.state,
                PolicyState.KNOWN_ON if encryption else PolicyState.KNOWN_OFF,
            )
            self.assertEqual(snapshot.internal_revision, seen[-1])
        self.assertEqual(len(set(seen)), 3, seen)

    def test_revision_key_is_not_part_of_default_settings(self):
        """Ключ не объявлен в DEFAULT_SETTINGS — exact-dict схема настроек не меняется."""
        self.assertNotIn(POLICY_REVISION_KEY, dict(DEFAULT_SETTINGS))
        self.assertEqual(POLICY_REVISION_KEY, "_plaintext_export_policy_revision")

    def test_reject_path_does_not_modify_settings_bytes(self):
        """Reject-путь (StateStoreSettingsCorruptError) не трогает файл НИ БАЙТОМ."""
        self.write_raw_settings(b"[1, 2, 3]")
        before = (self.data_dir / "settings.json").read_bytes()
        with self.assertRaises(StateStoreSettingsCorruptError):
            self.store.save_settings({ENCRYPTION_KEY: False, PRIVACY_KEY: False})
        after = (self.data_dir / "settings.json").read_bytes()
        self.assertEqual(after, before)
        # Побочные .tmp-артефакты на reject-пути тоже не появляются.
        leftovers = [p.name for p in self.data_dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [], leftovers)

    def test_commit_uses_atomic_replace_without_shared_tmp_name(self):
        """Единственная запись settings.json — atomic replace, без .json.tmp гонки."""
        self.seed_known_profile()
        self.store.save_settings({ENCRYPTION_KEY: False, PRIVACY_KEY: False})
        leftovers = sorted(p.name for p in self.data_dir.iterdir() if p.name.endswith(".tmp"))
        self.assertEqual(leftovers, [], leftovers)

    def test_save_settings_signature_is_preserved(self):
        """Сигнатура save_settings не менялась: (dict) -> normalized dict."""
        self.seed_known_profile()
        saved = self.store.save_settings(
            {ENCRYPTION_KEY: False, PRIVACY_KEY: False, "cloud_rewriter_enabled": True}
        )
        self.assertIsInstance(saved, dict)
        self.assertTrue(saved["cloud_rewriter_enabled"])
        touched = {ENCRYPTION_KEY, PRIVACY_KEY, "cloud_rewriter_enabled"}
        for key, value in dict(DEFAULT_SETTINGS).items():
            if key in touched:
                continue
            self.assertEqual(saved[key], value, key)


class TestSnapshotReadLockDiscipline(_PolicySnapshotFixture):
    """Snapshot — чистое чтение под shared lock, без вложенного EX (SH→EX запрещён)."""

    def test_read_under_held_shared_lock_does_not_raise_upgrade_error(self):
        self.write_settings(_profile(False, False))
        with self.store._lock(shared=True):
            snapshot = self.store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, PolicyState.KNOWN_OFF, snapshot)

    def test_snapshot_read_does_not_write_anything(self):
        self.write_settings(_profile(False, False, VALID_REVISION_ALT))
        # Прогреваем создание history.lock (его создаёт сам механизм _lock(),
        # а не reader), чтобы отличить его от записи snapshot-читалки.
        self.store.read_plaintext_policy_snapshot()
        before = sorted(p.name for p in self.data_dir.iterdir())
        mtime_ns = (self.data_dir / "settings.json").stat().st_mtime_ns
        self.store.read_plaintext_policy_snapshot()
        after = sorted(p.name for p in self.data_dir.iterdir())
        self.assertEqual(after, before)
        self.assertEqual((self.data_dir / "settings.json").stat().st_mtime_ns, mtime_ns)

    def test_public_wrapper_accepts_timeout_and_nowait(self):
        self.write_settings(_profile(False, False))
        self.assertIs(
            self.store.read_plaintext_policy_snapshot(timeout_sec=5.0).state,
            PolicyState.KNOWN_OFF,
        )
        self.assertIs(
            self.store.read_plaintext_policy_snapshot(nowait=True).state,
            PolicyState.KNOWN_OFF,
        )

    def test_snapshot_types_are_frozen_and_singleton_valued(self):
        """Типизация обязана быть frozen: подмена snapshot.state невозможна."""
        self.write_settings(_profile(False, False))
        snapshot = self.store.read_plaintext_policy_snapshot()
        with self.assertRaises(Exception):
            snapshot.state = PolicyState.KNOWN_ON  # type: ignore[misc]
        self.assertIsNotNone(snapshot.fingerprint)
        with self.assertRaises(Exception):
            snapshot.fingerprint.st_size = -1  # type: ignore[misc]

    def test_policy_state_values_are_exactly_three(self):
        """Ровно три состояния — ни одного лишнего/недокументированного."""
        self.assertEqual(
            sorted(state.value for state in PolicyState),
            ["KNOWN_OFF", "KNOWN_ON", "UNKNOWN"],
        )


# ═══════════════════════════════════════════════════════════════════════════
# B1 (CRITICAL, adversarial review): startup-save отмывал UNKNOWN → KNOWN_OFF
# ═══════════════════════════════════════════════════════════════════════════


class _StartupPolicyFixture(unittest.TestCase):
    """Фикстура startup-пути: НЕ BackendService (тяжёлые потоки), а ровно та
    инициализация настроек, которую делает ``build_service``.

    ``StateStore.initialize_startup_plaintext_policy`` — тот самый метод, который
    вызывает startup; проверка идёт через него и через реальный ``build_service``
    там, где это нужно доказать впрямую.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="a53-startup-")
        self.data_dir = Path(self._tmp.name) / "data"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ── disk-state helpers ─────────────────────────────────────────────────

    def reset_profile(self, *, prefix: str = "a53-startup-") -> None:
        """Чистый профиль на КАЖДЫЙ subtest (без вложенных TemporaryDirectory)."""
        self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory(prefix=prefix)
        self.data_dir = Path(self._tmp.name) / "data"

    def seed_settings(self, payload) -> None:
        """Готовит существующий профиль с заданным settings.json."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "settings.json"
        if isinstance(payload, bytes):
            path.write_bytes(payload)
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")

    def seed_empty_profile(self) -> None:
        """Существующий каталог профиля БЕЗ settings.json (не новый профиль)."""
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def startup(self, *, new_profile: bool):
        """Прогоняет startup-инициализацию политики. Возвращает (outcome, store)."""
        store = StateStore(self.data_dir)
        outcome = store.initialize_startup_plaintext_policy(
            new_profile=new_profile,
        )
        return outcome, store

    def on_disk(self) -> dict:
        return json.loads(
            (self.data_dir / "settings.json").read_text(encoding="utf-8")
        )

    def assert_revision_is_uuid4(self, revision) -> None:
        self.assertIsInstance(revision, str)
        self.assertEqual(uuid.UUID(hex=revision).version, 4)


class TestStartupDoesNotWashUnknownIntoKnown(_StartupPolicyFixture):
    """п.4/п.8 контракта: UNKNOWN обязан ПЕРЕЖИТЬ startup без действия юзера.

    До фикса B1 ``build_service`` безусловно писал
    ``save_settings(load_settings() or DEFAULT_SETTINGS)`` на КАЖДЫЙ старт, а
    центральный commit минтил ревизию. Так legacy/неполный профиль САМ СЕБЕ
    становился KNOWN_OFF — ровно тот UNKNOWN→ON класс, который искало ревью.
    """

    def test_legacy_migration_preserves_user_settings_and_extension_keys(self):
        """Добавление policy revision не сбрасывает существующий профиль."""
        original = {
            ENCRYPTION_KEY: False,
            PRIVACY_KEY: False,
            "llm_model": "synthetic-owner-model",
            "llm_rewriter_enabled": True,
            "future_extension": {"languages": ["ru", "es"], "enabled": False},
        }
        self.seed_settings(original)

        outcome, store = self.startup(new_profile=False)

        self.assertEqual(outcome, STARTUP_POLICY_MIGRATED_LEGACY)
        persisted = self.on_disk()
        for key, expected in original.items():
            with self.subTest(key=key):
                self.assertEqual(persisted.get(key), expected)
        self.assert_revision_is_uuid4(persisted[POLICY_REVISION_KEY])
        before_second_start = store.settings_path.read_bytes()
        self.assertEqual(
            store.initialize_startup_plaintext_policy(new_profile=False),
            STARTUP_POLICY_ALREADY_KNOWN,
        )
        self.assertEqual(store.settings_path.read_bytes(), before_second_start)

    def test_legacy_valid_bools_without_revision_migrates_to_known(self):
        """п.3: legacy с обоими валидными bool, но без ревизии → KNOWN + ревизия.

        Это РАЗРЕШЁНная миграция (п.8): флаги достоверны, профиль становится
        известным, но grant не выдаётся.
        """
        for encryption, expected in ((False, PolicyState.KNOWN_OFF), (True, PolicyState.KNOWN_ON)):
            with self.subTest(encryption=encryption):
                self.reset_profile(prefix="a53-legacy-")
                self.seed_settings({
                    ENCRYPTION_KEY: encryption,
                    PRIVACY_KEY: False,
                })
                outcome, store = self.startup(new_profile=False)
                self.assertEqual(outcome, STARTUP_POLICY_MIGRATED_LEGACY)
                snapshot = store.read_plaintext_policy_snapshot()
                self.assertIs(snapshot.state, expected, snapshot)
                self.assertIs(snapshot.privacy_mode_enabled, False, snapshot)
                self.assertIsNone(snapshot.reason, snapshot)
                self.assert_revision_is_uuid4(snapshot.internal_revision)
                # Миграция сохранила флаги и добавила ревизию — и только.
                on_disk = self.on_disk()
                self.assertIs(on_disk[ENCRYPTION_KEY], encryption)
                self.assertIs(on_disk[PRIVACY_KEY], False)
                self.assert_revision_is_uuid4(on_disk[POLICY_REVISION_KEY])
                # Никаких других ключей не потеряно/не выдумано.
                self.assertEqual(
                    set(self.on_disk()) - set(DEFAULT_SETTINGS),
                    {POLICY_REVISION_KEY},
                )

    def test_settings_without_policy_flags_stays_unknown_after_startup(self):
        """ГЛАВНЫЙ RED-кейс B1: `{"foo": 1}` → UNKNOWN, а НЕ KNOWN_OFF."""
        self.seed_settings({"foo": 1})
        outcome, store = self.startup(new_profile=False)
        self.assertEqual(outcome, STARTUP_POLICY_LEFT_UNKNOWN)
        snapshot = store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, PolicyState.UNKNOWN, snapshot)
        self.assertEqual(snapshot.reason, REASON_UNKNOWN_MISSING_KEY, snapshot)
        self.assertIsNone(snapshot.internal_revision, snapshot)
        # Startup НЕ дописал дефолты и НЕ смintил ревизию.
        self.assertEqual(set(self.on_disk()), {"foo"})
        self.assertNotIn(POLICY_REVISION_KEY, self.on_disk())

    def test_missing_privacy_flag_stays_unknown_after_startup(self):
        self.seed_settings({ENCRYPTION_KEY: False})
        outcome, store = self.startup(new_profile=False)
        self.assertEqual(outcome, STARTUP_POLICY_LEFT_UNKNOWN)
        self.assertEqual(
            store.read_plaintext_policy_snapshot().reason,
            REASON_UNKNOWN_MISSING_KEY,
        )

    def test_missing_encryption_flag_stays_unknown_after_startup(self):
        self.seed_settings({PRIVACY_KEY: False})
        outcome, store = self.startup(new_profile=False)
        self.assertEqual(outcome, STARTUP_POLICY_LEFT_UNKNOWN)
        self.assertEqual(
            store.read_plaintext_policy_snapshot().reason,
            REASON_UNKNOWN_MISSING_KEY,
        )

    def test_non_bool_flag_stays_unknown_after_startup(self):
        """Не-bool флаг обязан остаться UNKNOWN (никакой truthy-интерпретации)."""
        for bad in (1, 0, "true", None, [], {}):
            with self.subTest(bad=bad):
                self.reset_profile(prefix="a53-nonbool-")
                self.seed_settings({ENCRYPTION_KEY: bad, PRIVACY_KEY: False})
                outcome, store = self.startup(new_profile=False)
                self.assertEqual(outcome, STARTUP_POLICY_LEFT_UNKNOWN)
                snapshot = store.read_plaintext_policy_snapshot()
                self.assertIs(snapshot.state, PolicyState.UNKNOWN, snapshot)
                self.assertEqual(snapshot.reason, REASON_UNKNOWN_NON_BOOL, snapshot)
                self.assertNotIn(POLICY_REVISION_KEY, self.on_disk())

    def test_corrupt_json_stays_unknown_after_startup(self):
        self.seed_settings(b'{"history_encryption_enabled": false,')
        outcome, store = self.startup(new_profile=False)
        self.assertEqual(outcome, STARTUP_POLICY_LEFT_UNKNOWN)
        snapshot = store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, PolicyState.UNKNOWN, snapshot)
        self.assertEqual(snapshot.reason, REASON_UNKNOWN_UNREADABLE, snapshot)

    def test_not_object_settings_stays_unknown_after_startup(self):
        self.seed_settings(b"[1, 2, 3]")
        outcome, store = self.startup(new_profile=False)
        self.assertEqual(outcome, STARTUP_POLICY_LEFT_UNKNOWN)
        snapshot = store.read_plaintext_policy_snapshot()
        self.assertEqual(snapshot.reason, REASON_UNKNOWN_NOT_OBJECT, snapshot)

    def test_non_utf8_settings_stays_unknown_after_startup(self):
        self.seed_settings(b"\xff\xfe\x00garbage")
        outcome, store = self.startup(new_profile=False)
        self.assertEqual(outcome, STARTUP_POLICY_LEFT_UNKNOWN)
        self.assertEqual(
            store.read_plaintext_policy_snapshot().reason,
            REASON_UNKNOWN_UNREADABLE,
        )

    def test_already_known_profile_is_left_untouched(self):
        """Валидный профиль с ревизией: startup НЕ пишет (нет ревизии- churn)."""
        self.seed_settings(_profile(False, False, VALID_REVISION))
        before_bytes = (self.data_dir / "settings.json").read_bytes()
        outcome, store = self.startup(new_profile=False)
        self.assertEqual(outcome, STARTUP_POLICY_ALREADY_KNOWN)
        self.assertEqual((self.data_dir / "settings.json").read_bytes(), before_bytes)
        snapshot = store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, PolicyState.KNOWN_OFF, snapshot)
        self.assertEqual(snapshot.internal_revision, VALID_REVISION)

    def test_existing_profile_without_settings_file_is_not_declared_new(self):
        """Существующий каталог БЕЗ settings.json — НЕ «достоверно новый профиль»."""
        self.seed_empty_profile()
        outcome, store = self.startup(new_profile=False)
        self.assertEqual(outcome, STARTUP_POLICY_LEFT_UNKNOWN)
        self.assertFalse((self.data_dir / "settings.json").exists())
        self.assertEqual(
            store.read_plaintext_policy_snapshot().reason,
            REASON_UNKNOWN_MISSING_SETTINGS,
        )

    def test_new_profile_without_settings_is_explicitly_initialized(self):
        """п.7: достоверно новый профиль → явная инициализация + ревизия."""
        outcome, store = self.startup(new_profile=True)
        self.assertEqual(outcome, STARTUP_POLICY_INITIALIZED)
        snapshot = store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, PolicyState.KNOWN_OFF, snapshot)
        self.assertIsNone(snapshot.reason, snapshot)
        self.assertIs(snapshot.privacy_mode_enabled, False, snapshot)
        self.assert_revision_is_uuid4(snapshot.internal_revision)
        on_disk = self.on_disk()
        self.assertIs(on_disk[ENCRYPTION_KEY], False)
        self.assertIs(on_disk[PRIVACY_KEY], False)
        self.assert_revision_is_uuid4(on_disk[POLICY_REVISION_KEY])

    def test_new_profile_flag_never_overwrites_existing_settings(self):
        """Даже с ``new_profile=True`` существующий файл не перетирается."""
        self.seed_settings(_profile(True, True, VALID_REVISION))
        before = (self.data_dir / "settings.json").read_bytes()
        outcome, store = self.startup(new_profile=True)
        self.assertEqual(outcome, STARTUP_POLICY_ALREADY_KNOWN)
        self.assertEqual((self.data_dir / "settings.json").read_bytes(), before)
        snapshot = store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, PolicyState.KNOWN_ON, snapshot)
        self.assertIs(snapshot.privacy_mode_enabled, True, snapshot)

    def test_startup_never_upgrades_shared_lock_to_exclusive(self):
        """п.7: SH→EX upgrade запрещён — под held shared он обязан ГРОМКО упасть."""
        store = StateStore(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with store._lock(shared=True):
            with self.assertRaises(StateStoreLockUpgradeError):
                store.initialize_startup_plaintext_policy(new_profile=True)

    def test_startup_never_saves_pre_reacquire_snapshot(self):
        """Решение принимается по перечитанным ПОСЛЕ захвата данным.

        Если бы инициализатор сохранял снимок, прочитанный ДО reacquire, подмена
        settings.json другим «процессом» между чтением и записью дала бы запись
        чужих значений. Здесь файл подменяется в момент входа в эксклюзивный
        лок — корректный результат обязан опираться на ПОСЛЕДНЮЮ версию.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        settings_path = self.data_dir / "settings.json"
        settings_path.write_text(
            json.dumps({ENCRYPTION_KEY: False, PRIVACY_KEY: False}),
            encoding="utf-8",
        )
        store = StateStore(self.data_dir)
        real_lock = store._lock
        swapped = {"done": False}

        @contextlib.contextmanager
        def swapping_lock(*args, **kwargs):
            with real_lock(*args, **kwargs):
                if not swapped["done"]:
                    swapped["done"] = True
                    # «Другой процесс» успел записать свои значения, пока мы
                    # ждали эксклюзивный лок.
                    settings_path.write_text(
                        json.dumps({
                            ENCRYPTION_KEY: True,
                            PRIVACY_KEY: True,
                            POLICY_REVISION_KEY: VALID_REVISION,
                        }),
                        encoding="utf-8",
                    )
                yield

        with mock.patch.object(store, "_lock", swapping_lock):
            outcome = store.initialize_startup_plaintext_policy(new_profile=False)
        self.assertEqual(outcome, STARTUP_POLICY_ALREADY_KNOWN)
        on_disk = self.on_disk()
        self.assertIs(on_disk[ENCRYPTION_KEY], True)
        self.assertIs(on_disk[PRIVACY_KEY], True)
        self.assertEqual(on_disk[POLICY_REVISION_KEY], VALID_REVISION)


class TestBuildServiceStartupWiring(_StartupPolicyFixture):
    """B1 на РЕАЛЬНОМ startup-пути: ``build_service`` обязан звать инициализатор.

    Именно этот путь ревью назвало источником отмывания: безусловный
    ``save_settings(load_settings() or DEFAULT_SETTINGS)``.
    """

    def _run_build_service(self):
        from backend.service import build_service

        svc = build_service(self.data_dir)
        try:
            return StateStore(self.data_dir).read_plaintext_policy_snapshot()
        finally:
            # Обязателен close(): иначе фоновые демон-треды роняют чанк тестов.
            svc.close()

    def test_build_service_keeps_flagless_profile_unknown(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "settings.json").write_text(
            json.dumps({"foo": 1}), encoding="utf-8"
        )
        snapshot = self._run_build_service()
        self.assertIs(snapshot.state, PolicyState.UNKNOWN, snapshot)
        self.assertEqual(snapshot.reason, REASON_UNKNOWN_MISSING_KEY, snapshot)

    def test_build_service_keeps_corrupt_profile_unknown(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "settings.json").write_bytes(b"[1, 2, 3]")
        snapshot = self._run_build_service()
        self.assertIs(snapshot.state, PolicyState.UNKNOWN, snapshot)
        self.assertEqual(snapshot.reason, REASON_UNKNOWN_NOT_OBJECT, snapshot)

    def test_build_service_migrates_legacy_valid_bools(self):
        self.seed_settings({ENCRYPTION_KEY: False, PRIVACY_KEY: True})
        snapshot = self._run_build_service()
        self.assertIs(snapshot.state, PolicyState.KNOWN_OFF, snapshot)
        self.assertIs(snapshot.privacy_mode_enabled, True, snapshot)
        self.assert_revision_is_uuid4(snapshot.internal_revision)

    def test_build_service_initializes_brand_new_profile(self):
        """Каталог не существует → достоверно новый профиль → KNOWN_OFF."""
        self.assertFalse(self.data_dir.exists())
        snapshot = self._run_build_service()
        self.assertIs(snapshot.state, PolicyState.KNOWN_OFF, snapshot)
        self.assertIsNone(snapshot.reason, snapshot)
        self.assert_revision_is_uuid4(snapshot.internal_revision)

    def test_build_service_creates_settings_file_for_new_profile(self):
        """Существующий инвариант startup: settings.json создаётся на новом профиле."""
        self.assertFalse(self.data_dir.exists())
        self._run_build_service()
        self.assertTrue((self.data_dir / "settings.json").exists())


# ═══════════════════════════════════════════════════════════════════════════
# B2: публичное чтение обязано быть exception-total
# ═══════════════════════════════════════════════════════════════════════════


class TestPublicSnapshotReadIsExceptionTotal(_PolicySnapshotFixture):
    """§7.1/п.12: отказ захвата лока → UNKNOWN, а НЕ исключение наружу.

    Раньше ``_lock(...)`` стоял ВНЕ try, поэтому ``StateStoreLockTimeout`` /
    ``StateStoreLockUpgradeError`` вылетали из публичного read, и любой вызывающий
    обязан был оборачивать его в try/except — или, что хуже, получить исключение
    вместо fail-closed UNKNOWN.
    """

    def test_lock_acquire_timeout_returns_unknown_not_exception(self):
        """Детерминированно: ``_lock`` патчится на выброс timeout."""
        self.write_settings(_profile(False, False))
        with mock.patch.object(
            self.store,
            "_lock",
            side_effect=StateStoreLockTimeout("held by another process"),
        ):
            snapshot = self.store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, PolicyState.UNKNOWN, snapshot)
        self.assertEqual(snapshot.reason, REASON_UNKNOWN_PROVIDER_ERROR, snapshot)

    def test_lock_upgrade_error_returns_unknown_not_exception(self):
        self.write_settings(_profile(False, False))
        with mock.patch.object(
            self.store,
            "_lock",
            side_effect=StateStoreLockUpgradeError("SH->EX"),
        ):
            snapshot = self.store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, PolicyState.UNKNOWN, snapshot)
        self.assertEqual(snapshot.reason, REASON_UNKNOWN_PROVIDER_ERROR, snapshot)

    def test_injected_body_exception_returns_provider_error(self):
        """Причина константы ПИНЧИТСЯ: раньше она вообще не импортировалась."""
        self.write_settings(_profile(False, False))
        with mock.patch.object(
            self.store,
            "_plaintext_policy_snapshot_locked",
            side_effect=RuntimeError("provider blew up"),
        ):
            snapshot = self.store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, PolicyState.UNKNOWN, snapshot)
        self.assertEqual(snapshot.reason, REASON_UNKNOWN_PROVIDER_ERROR, snapshot)

    def test_real_cross_thread_lock_contention_returns_unknown(self):
        """Bounded-детерминированный кейс: НАСТОЯЩИЙ flock держит другой поток.

        Механизм — ``nowait=True`` (ровно одна неблокирующая попытка), поэтому
        тест не ждёт и не засыпает: исход детерминирован по построению.
        """
        self.write_settings(_profile(False, False))
        holding = threading.Event()
        release = threading.Event()
        holder_state: dict = {}

        def holder():
            try:
                with self.store._lock():
                    holder_state["acquired"] = True
                    holding.set()
                    release.wait(timeout=10.0)
            except BaseException as exc:  # noqa: BLE001
                holder_state["error"] = exc

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        try:
            self.assertTrue(holding.wait(timeout=10.0), "holder не взял лок")
            snapshot = self.store.read_plaintext_policy_snapshot(nowait=True)
        finally:
            release.set()
            thread.join(timeout=10.0)
        self.assertNotIn("error", holder_state, holder_state)
        self.assertFalse(thread.is_alive())
        self.assertIs(snapshot.state, PolicyState.UNKNOWN, snapshot)
        self.assertEqual(snapshot.reason, REASON_UNKNOWN_PROVIDER_ERROR, snapshot)

    def test_provider_error_reason_is_pinned_constant(self):
        """Константа причины — ровно эта строка (машинный reason-код)."""
        self.assertEqual(REASON_UNKNOWN_PROVIDER_ERROR, "unknown_provider_error")

    def test_read_never_falls_back_to_previous_snapshot(self):
        """После неудачи нет обращения к «прошлому» снимку (никакого кэша)."""
        self.write_settings(_profile(False, False))
        good = self.store.read_plaintext_policy_snapshot()
        self.assertIs(good.state, PolicyState.KNOWN_OFF, good)
        with mock.patch.object(
            self.store,
            "_plaintext_policy_snapshot_locked",
            side_effect=RuntimeError("boom"),
        ):
            after = self.store.read_plaintext_policy_snapshot()
        self.assertIs(after.state, PolicyState.UNKNOWN, after)
        self.assertIsNone(after.fingerprint, after)
        self.assertIsNone(after.internal_revision, after)


# ═══════════════════════════════════════════════════════════════════════════
# T1/T2/T3 + прочие правки ревью
# ═══════════════════════════════════════════════════════════════════════════


class TestReaderHardening(_PolicySnapshotFixture):
    """Ветки, которые раньше были мертвы для сьюта (false-green)."""

    def test_symlink_at_settings_path_is_unreadable(self):
        """T3: симлинк на месте settings.json → UNREADABLE (пинит O_NOFOLLOW).

        Без ``O_NOFOLLOW`` open() прошёл бы по ссылке и вернул бы KNOWN_* —
        то есть сьют молча перестал бы ловить подмену пути.
        """
        path = self.drop_settings()
        target = self.data_dir / "elsewhere.json"
        target.write_text(json.dumps(_profile(False, False)), encoding="utf-8")
        os.symlink(target, path)
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assert_unknown(snapshot, REASON_UNKNOWN_UNREADABLE)

    def test_symlink_to_non_regular_target_is_unreadable(self):
        path = self.drop_settings()
        fifo = self.data_dir / "pipe"
        os.mkfifo(fifo)
        os.symlink(fifo, path)
        snapshot = self._read_in_bounded_thread()
        self.assert_unknown(snapshot, REASON_UNKNOWN_UNREADABLE)

    # ── bounded-read helper (общий для hardening-кейсов) ──────────────────

    def _read_in_bounded_thread(self):
        """Читает snapshot в daemon-треде: зависание = провал теста, не hung CI."""
        box: dict = {}

        def runner():
            try:
                box["snapshot"] = self.store.read_plaintext_policy_snapshot()
            except BaseException as exc:  # noqa: BLE001 — тест должен увидеть причину
                box["error"] = exc

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join(timeout=10.0)
        if thread.is_alive():
            self.fail("read_plaintext_policy_snapshot() завис на нерегулярном пути")
        if "error" in box:
            raise box["error"]
        return box["snapshot"]

    def test_missing_nofollow_flag_fails_closed_to_unknown(self):
        """T1: ``O_NOFOLLOW`` — HARD-require, а не ``getattr(..., 0)``.

        Если платформа/окружение не даёт флага, путь обязан дать UNKNOWN, а не
        молча слинковаться.
        """
        self.write_settings(_profile(False, False))
        with mock.patch.object(os, "O_NOFOLLOW", 0, create=True):
            snapshot = self.store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, PolicyState.UNKNOWN, snapshot)
        self.assertIn(
            snapshot.reason,
            (REASON_UNKNOWN_UNREADABLE, REASON_UNKNOWN_PROVIDER_ERROR),
        )

    def test_growth_between_fstat_and_read_is_oversize(self):
        """T2: файл вырос между fstat и read → OVERSIZE (ветка была мертва).

        Размер на диске всё ещё МАЛЕНЬКИЙ (fstat его пропускает), но read
        возвращает больше капа — именно это и означает «вырос между fstat и
        чтением». Ветка обязана срабатывать, а не молча парсить обрезанный буфер.
        """
        self.write_settings(_profile(False, False))
        size_on_disk = (self.data_dir / "settings.json").stat().st_size
        self.assertLess(size_on_disk, MAX_POLICY_BYTES)
        real_read = os.read
        grew = {"done": False}

        def growing_read(fd, size):
            # Один раз отдаём больше капа: это и есть «файл вырос после fstat».
            if not grew["done"] and size > 0:
                grew["done"] = True
                return b"x" * (size + 1)
            return real_read(fd, size)

        with mock.patch("os.read", side_effect=growing_read):
            snapshot = self.store.read_plaintext_policy_snapshot()
        self.assertTrue(grew["done"], "подмена os.read не была вызвана")
        self.assert_unknown(snapshot, REASON_UNKNOWN_OVERSIZE)

    def test_non_utf8_bytes_are_unreadable(self):
        self.write_raw_settings(b"\xff\xfe\x00\x80garbage")
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assert_unknown(snapshot, REASON_UNKNOWN_UNREADABLE)

    def test_truncated_utf8_sequence_is_unreadable(self):
        self.write_raw_settings('{"privacy_mode_enabled": true, "x": "'.encode() + b"\xc3")
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assert_unknown(snapshot, REASON_UNKNOWN_UNREADABLE)


class TestRevisionValidation(_PolicySnapshotFixture):
    """Ревизия — не «любая непустая строка», а канонический 32-hex формат."""

    def test_valid_uuid4_hex_revision_is_accepted(self):
        for revision in (VALID_REVISION, VALID_REVISION_ALT, uuid.uuid4().hex):
            with self.subTest(revision=revision):
                self.write_settings(_profile(False, False, revision))
                snapshot = self.store.read_plaintext_policy_snapshot()
                self.assertIs(snapshot.state, PolicyState.KNOWN_OFF, snapshot)
                self.assertEqual(snapshot.internal_revision, revision)

    def test_non_hex_non_empty_revision_is_invalid(self):
        """Старые фикстуры вида ``rev-fixture`` обязаны отвергаться."""
        for revision in (
            "rev-fixture",
            "not-a-revision",
            "zz" * 16,
            "0123456789abcdef0123456789abcdeZ",
            "0123456789ABCDEF0123456789ABCDEF",
            "0" * 31,
            "0" * 33,
            " " * 32,
            VALID_REVISION + " ",
            " " + VALID_REVISION,
        ):
            with self.subTest(revision=revision):
                self.write_settings(_profile(False, False, revision))
                snapshot = self.store.read_plaintext_policy_snapshot()
                self.assert_unknown(snapshot, REASON_UNKNOWN_INVALID_REVISION)

    def test_non_string_revision_is_invalid(self):
        for revision in (123, None, True, ["rev"], {"rev": 1}, 1.5):
            with self.subTest(revision=revision):
                self.write_settings(_profile(False, False, revision))
                snapshot = self.store.read_plaintext_policy_snapshot()
                self.assert_unknown(snapshot, REASON_UNKNOWN_INVALID_REVISION)

    def test_revision_generated_by_commit_always_reads_back_as_valid(self):
        """Round-trip: то, что пишет commit, обязано читаться как KNOWN."""
        self.seed_known_profile()
        for _ in range(5):
            saved = self.store.save_settings({ENCRYPTION_KEY: False, PRIVACY_KEY: False})
            snapshot = self.store.read_plaintext_policy_snapshot()
            self.assertIs(snapshot.state, PolicyState.KNOWN_OFF, snapshot)
            self.assertEqual(snapshot.internal_revision, saved[POLICY_REVISION_KEY])


# ═══════════════════════════════════════════════════════════════════════════
# BLOCKER-1 (два независимых ревью → BLOCK): точечные write-guards в save_settings
# НЕ ловят неполный профиль, поэтому ЛЮБОЙ посторонний поддержанный писатель
# отмывает UNKNOWN в KNOWN_OFF (мержит DEFAULT_SETTINGS + минтит ревизию).
# Проверяется РЕАЛЬНЫЙ путь отмывания: Swift main.swift зеркалит
# ``wake_word_enabled`` через ipc.call("set_settings", …) → handle_set_settings.
# ═══════════════════════════════════════════════════════════════════════════


class _ForeignSupportedWriteFixture(unittest.TestCase):
    """Существующий (не новый!) профиль + неполный settings.json на диске.

    Каталог создаётся ДО конструкции StateStore — поэтому ``_created_data_dir``
    обязан быть False, и запись на отсутствующий/неполный settings.json не
    является «инициализацией нового профиля».
    """

    #: Три формы неполного профиля, которые старые A5 write-guards НЕ ловили.
    INCOMPLETE_PROFILES = {
        "missing_privacy_flag": {ENCRYPTION_KEY: False},
        "non_bool_privacy_flag": {ENCRYPTION_KEY: False, PRIVACY_KEY: "yes"},
        "missing_encryption_flag": {PRIVACY_KEY: False},
    }

    #: Ровно та посторонняя запись, которую Swift-агент шлёт на каждом старте.
    FOREIGN_WRITE = {"wake_word_enabled": True}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="a53-foreign-")
        self.data_dir = Path(self._tmp.name) / "data"
        self.data_dir.mkdir(parents=True)
        self.settings_path = self.data_dir / "settings.json"
        self.store = StateStore(self.data_dir)

    def tearDown(self) -> None:
        self.store = None
        self._tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────────

    def seed_profile(self, payload) -> None:
        if isinstance(payload, bytes):
            self.settings_path.write_bytes(payload)
        else:
            self.settings_path.write_text(json.dumps(payload), encoding="utf-8")

    def seed_known_profile(
        self,
        *,
        encryption: bool = False,
        privacy: bool = False,
        revision: object = VALID_REVISION,
    ) -> None:
        """SUPPORTED-профиль для тестов, проверяющих поведение ЗАПИСИ."""
        _seed_known_profile(
            self.data_dir,
            encryption=encryption,
            privacy=privacy,
            revision=revision,
        )

    def real_settings_service(self):
        """Настоящий ``SettingsService`` над настоящим стором (никаких фейков).

        Бэкапы уходят в каталог ПРОФИЛЯ, а не в пользовательский каталог.
        """
        from backend.settings_backup import SettingsBackup
        from backend.settings_service import SettingsService

        return SettingsService(
            store=self.store,
            backup=SettingsBackup(self.data_dir / "backups"),
        )

    def on_disk(self) -> dict:
        return json.loads(self.settings_path.read_text(encoding="utf-8"))


class TestForeignSupportedWriteCannotWashUnknownIntoKnown(_ForeignSupportedWriteFixture):
    """A: посторонняя поддержанная запись НЕ делает профиль известным.

    Инвариант проверяется на РЕАЛЬНОМ пути (SettingsService → save_settings),
    а не прямым вызовом стора: именно этот путь нашло ревью.
    """

    def _assert_write_did_not_wash_profile(self, write) -> None:
        for case, payload in self.INCOMPLETE_PROFILES.items():
            with self.subTest(case=case):
                self.seed_profile(payload)
                before = self.settings_path.read_bytes()
                rejected = None
                try:
                    write()
                except IpcOperationalError as exc:
                    rejected = exc
                # Инвариант: что бы ни случилось, профиль НЕ стал известным.
                snapshot = self.store.read_plaintext_policy_snapshot()
                self.assertIs(snapshot.state, PolicyState.UNKNOWN, (case, snapshot))
                if rejected is not None:
                    # Отказ обязан быть громким и ТИПИЗИРОВАННЫМ, а байты файла
                    # — не тронутыми.
                    self.assertIsInstance(rejected, StateStoreSettingsCorruptError, case)
                    self.assertIn(
                        rejected.__class__.__name__,
                        ("StateStoreSettingsCorruptError",),
                        case,
                    )
                    self.assertEqual(self.settings_path.read_bytes(), before, case)

    def test_foreign_set_settings_write_cannot_make_incomplete_profile_known(self):
        def write():
            service = self.real_settings_service()
            return service.handle_set_settings(dict(self.FOREIGN_WRITE))

        self._assert_write_did_not_wash_profile(write)

    def test_foreign_profile_preset_write_cannot_make_incomplete_profile_known(self):
        """Второй посторонний писатель того же choke point (apply_profile_preset)."""

        def write():
            service = self.real_settings_service()
            return service.handle_apply_profile_preset({"profile": "meeting"})

        self._assert_write_did_not_wash_profile(write)

    def test_incomplete_profile_reports_machine_reason_in_rejection(self):
        """Причина отказа — машинный reason-код, без значений флагов/payload."""
        expected_reason = {
            "missing_privacy_flag": REASON_UNKNOWN_MISSING_KEY,
            "non_bool_privacy_flag": REASON_UNKNOWN_NON_BOOL,
            "missing_encryption_flag": REASON_UNKNOWN_MISSING_KEY,
        }
        for case, payload in self.INCOMPLETE_PROFILES.items():
            with self.subTest(case=case):
                self.seed_profile(payload)
                with self.assertRaises(StateStoreSettingsCorruptError) as ctx:
                    self.store.save_settings({"cloud_rewriter_enabled": True})
                message = str(ctx.exception)
                self.assertIn(expected_reason[case], message, case)
                # Ни значений флагов, ни «сырого» payload в тексте ошибки.
                for leaked in ("yes", "privacy_mode_enabled", "cloud_rewriter_enabled"):
                    self.assertNotIn(leaked, message, case)


class TestHotwordsAutoSeedPolicyGate(_StartupPolicyFixture):
    """B: авто-сид hotwords (supported write) подчиняется тому же гейту.

    На UNKNOWN профиле сида нет (иначе он отмыл бы профиль), на KNOWN_* —
    функциональность не сломана.
    """

    def _build(self):
        from backend.service import build_service

        service = build_service(self.data_dir)
        self.addCleanup(service.close)
        return service

    def test_auto_seed_is_skipped_on_unknown_profile(self):
        self.seed_settings({"foo": 1})
        before = (self.data_dir / "settings.json").read_bytes()
        self._build()
        self.assertEqual((self.data_dir / "settings.json").read_bytes(), before)
        self.assertNotIn("stt_hotwords", self.on_disk())
        snapshot = StateStore(self.data_dir).read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, PolicyState.UNKNOWN, snapshot)

    def test_auto_seed_still_works_on_known_profile(self):
        for encryption, expected in ((False, PolicyState.KNOWN_OFF), (True, PolicyState.KNOWN_ON)):
            with self.subTest(encryption=encryption):
                self.reset_profile(prefix="a53-seed-known-")
                self.seed_settings(_profile(encryption, False))
                self._build()
                seeded = self.on_disk()["stt_hotwords"]
                self.assertTrue(seeded, "авто-сид дефолтных hotwords не произошёл")
                snapshot = StateStore(self.data_dir).read_plaintext_policy_snapshot()
                self.assertIs(snapshot.state, expected, snapshot)


class TestValidatedRepairIsTheOnlyExitFromUnknown(_ForeignSupportedWriteFixture):
    """C: явный и УЗКИЙ escape hatch из UNKNOWN (п.8 validated repair)."""

    def test_validated_repair_writes_known_state_from_incomplete_profile(self):
        for case, payload in self.INCOMPLETE_PROFILES.items():
            with self.subTest(case=case):
                self.seed_profile(payload)
                saved = self.store.save_settings(
                    {"cloud_rewriter_enabled": True},
                    validated_repair=True,
                )
                snapshot = self.store.read_plaintext_policy_snapshot()
                self.assertIs(snapshot.state, PolicyState.KNOWN_OFF, (case, snapshot))
                self.assertIsNone(snapshot.reason, case)
                self.assertEqual(uuid.UUID(hex=snapshot.internal_revision).version, 4)
                self.assertNotEqual(snapshot.internal_revision, VALID_REVISION)
                self.assertEqual(saved[POLICY_REVISION_KEY], snapshot.internal_revision)
                self.assertIs(self.on_disk()[PRIVACY_KEY], False)

    def test_validated_repair_is_off_by_default(self):
        """Тот же профиль БЕЗ флага отказал — hatch не включён по умолчанию."""
        self.seed_profile(self.INCOMPLETE_PROFILES["missing_privacy_flag"])
        with self.assertRaises(StateStoreSettingsCorruptError):
            self.store.save_settings({"cloud_rewriter_enabled": True})
        self.assertIs(
            self.store.read_plaintext_policy_snapshot().state,
            PolicyState.UNKNOWN,
        )

    def test_validated_repair_is_keyword_only(self):
        """Hatch нельзя включить позиционным аргументом (обратная совместимость)."""
        self.seed_profile(self.INCOMPLETE_PROFILES["missing_privacy_flag"])
        before = self.settings_path.read_bytes()
        with self.assertRaises(TypeError):
            self.store.save_settings({"cloud_rewriter_enabled": True}, True)
        # Позиционная передача не должна была НИЧЕГО записать.
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.assertIs(
            self.store.read_plaintext_policy_snapshot().state,
            PolicyState.UNKNOWN,
        )

    def test_positional_call_still_works(self):
        """Старые позиционные вызовы (dict) -> dict остаются валидными."""
        self.seed_known_profile()
        saved = self.store.save_settings({"cloud_rewriter_enabled": True})
        self.assertIsInstance(saved, dict)
        self.assertTrue(saved["cloud_rewriter_enabled"])


class TestCreatedDataDirGatesMissingSettingsWrites(_ForeignSupportedWriteFixture):
    """D: признак «каталог создал этот startup» решает судьбу MISSING_SETTINGS."""

    def test_created_data_dir_is_true_only_when_store_created_the_directory(self):
        self.assertFalse(self.store._created_data_dir)
        fresh = Path(self._tmp.name) / "fresh"
        self.assertFalse(fresh.exists())
        fresh_store = StateStore(fresh)
        self.assertTrue(fresh_store._created_data_dir)
        self.assertTrue(fresh.is_dir())

    def test_store_created_profile_writes_defaults_and_becomes_known_off(self):
        fresh = Path(self._tmp.name) / "fresh-defaults"
        store = StateStore(fresh)
        store.save_settings({"cloud_rewriter_enabled": True})
        snapshot = store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, PolicyState.KNOWN_OFF, snapshot)
        self.assertIsNone(snapshot.reason, snapshot)
        self.assertEqual(uuid.UUID(hex=snapshot.internal_revision).version, 4)
        on_disk = json.loads((fresh / "settings.json").read_text(encoding="utf-8"))
        self.assertIs(on_disk[ENCRYPTION_KEY], False)
        self.assertIs(on_disk[PRIVACY_KEY], False)

    def test_existing_profile_with_removed_settings_refuses_and_stays_unknown(self):
        """«Удалённый settings.json у существующего профиля» НЕ самопочинивается."""
        self.seed_profile(_profile(False, False))
        self.drop_settings_file()
        with self.assertRaises(StateStoreSettingsCorruptError):
            self.store.save_settings({"cloud_rewriter_enabled": True})
        self.assertFalse(self.settings_path.exists())
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, PolicyState.UNKNOWN, snapshot)
        self.assertEqual(snapshot.reason, REASON_UNKNOWN_MISSING_SETTINGS, snapshot)

    def drop_settings_file(self) -> None:
        if self.settings_path.exists() or self.settings_path.is_symlink():
            self.settings_path.unlink()

    def test_every_other_unknown_reason_refuses_loudly(self):
        """missing-key/non-bool/not-object/corrupt/duplicate/oversize/non-regular."""
        cases = {
            "not_object": lambda: self.settings_path.write_bytes(b"[1, 2, 3]"),
            "corrupt_json": lambda: self.settings_path.write_bytes(
                b'{"history_encryption_enabled": false,'
            ),
            "unreadable_utf8": lambda: self.settings_path.write_bytes(b"\xff\xfe\x00garbage"),
            "duplicate_keys": lambda: self.settings_path.write_bytes(
                b'{"history_encryption_enabled": false, "history_encryption_enabled": true}'
            ),
            "oversize": lambda: self.settings_path.write_bytes(
                b"x" * (MAX_POLICY_BYTES + 1)
            ),
            "non_regular": self._settings_path_becomes_directory,
        }
        for case, seed in cases.items():
            with self.subTest(case=case):
                seed()
                before = self._settings_bytes_or_none()
                with self.assertRaises(StateStoreSettingsCorruptError):
                    self.store.save_settings({"cloud_rewriter_enabled": True})
                self.assertEqual(self._settings_bytes_or_none(), before, case)
        # Не-bool флаг — отдельный payload (после очистки каталога-кейса).
        self._settings_path_becomes_directory()
        self.settings_path.rmdir()
        self.seed_profile({ENCRYPTION_KEY: "yes", PRIVACY_KEY: False})
        with self.assertRaises(StateStoreSettingsCorruptError):
            self.store.save_settings({"cloud_rewriter_enabled": True})

    def _settings_path_becomes_directory(self) -> None:
        if self.settings_path.is_dir():
            self.settings_path.rmdir()
        elif self.settings_path.exists() or self.settings_path.is_symlink():
            self.settings_path.unlink()
        self.settings_path.mkdir()

    def _settings_bytes_or_none(self):
        """Байты settings.json либо None, если на пути нерегулярная запись."""
        if self.settings_path.is_dir():
            return None
        return self.settings_path.read_bytes()

    def test_legacy_missing_revision_profile_may_still_be_written(self):
        """п.8: оба exact-bool без ревизии — легаси, обычная запись разрешена."""
        self.seed_profile({ENCRYPTION_KEY: False, PRIVACY_KEY: True})
        before = self.store.read_plaintext_policy_snapshot()
        self.assertIs(before.state, PolicyState.UNKNOWN, before)
        self.assertEqual(before.reason, REASON_UNKNOWN_MISSING_REVISION, before)
        self.store.save_settings({"cloud_rewriter_enabled": True})
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, PolicyState.KNOWN_OFF, snapshot)
        self.assertEqual(uuid.UUID(hex=snapshot.internal_revision).version, 4)

    def test_legacy_profile_keeps_its_flags_on_a_real_supported_write(self):
        """«Сохранить те же флаги» п.8 на РЕАЛЬНОМ пути записи.

        Все production-писатели передают ``store.save_settings`` настройки,
        прочитанные с диска (``cached_settings``/``load_settings``), поэтому
        флаги доезжают в записи и privacy не молча деградирует до дефолта.
        """
        self.seed_profile({ENCRYPTION_KEY: False, PRIVACY_KEY: True})
        service = self.real_settings_service()
        service.handle_set_settings(dict(self.FOREIGN_WRITE))
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, PolicyState.KNOWN_OFF, snapshot)
        self.assertIs(snapshot.privacy_mode_enabled, True, snapshot)
        self.assertEqual(uuid.UUID(hex=snapshot.internal_revision).version, 4)
        self.assertTrue(self.on_disk()["wake_word_enabled"])


# ═══════════════════════════════════════════════════════════════════════════
# Slice 2 (карточка A): PlaintextExportAuthorizer — issue/validate/revoke.
#
# Контракт: A53_STRONG_MODEL_HANDOFF.md п.1,2,4,5,5a,5b,5c,12,15,16,18,19
# (п.4 — trust boundary, честный тест) + §7.4/§7.6 спеки.
# Только Python API (IPC — slice 4), только RAM, без sinks/redaction/Swift.
# Поведенческие тесты: без AST/source-inspection, без реальной истории/ENC1,
# без sleep (только Barrier/Event/wait с таймаутом). multiprocessing — только
# в 5a/5b/5c (+ subprocess-проба лёгкости импорта, это не multiprocessing).
# ═══════════════════════════════════════════════════════════════════════════


def _authorizer_module():
    """Ленивый импорт модуля authorizer (slice 2).

    Пока класса нет (RED), падают только slice-2 тесты, а не весь файл.
    """
    from backend import plaintext_export_authorization as module

    return module


def _child_preamble():
    """Гигиена spawn-детей 5a/5b/5c: лёгкая цепочка импорта без service/torch/mlx.

    Вызывается ПЕРВОЙ строкой каждого worker'а. Падение здесь означает, что
    ``backend.state_store``/``backend.plaintext_export_authorization`` потянули
    тяжёлые зависимости — чинить код, а не тест.
    """
    import sys

    for name in sys.modules:
        if name == "backend.service" or name.startswith("backend.service."):
            raise AssertionError("child импортировал backend.service: %s" % name)
    heavy = [
        name
        for name in sys.modules
        if name.split(".")[0] in ("torch", "mlx", "mlx_whisper")
    ]
    assert not heavy, "child потянул тяжёлые ML-зависимости: %r" % (heavy,)


class _AuthorizerFixture(_PolicySnapshotFixture):
    """Временный профиль + authorizer без IPC/BackendService."""

    SESSION_A = "11111111-1111-4111-8111-111111111111"
    SESSION_B = "22222222-2222-4222-8222-222222222222"
    SINK_MD = "history-md"

    def authorizer_module(self):
        return _authorizer_module()

    def make_authorizer(self, **overrides):
        module = self.authorizer_module()
        kwargs = {
            "read_snapshot": self.store.read_plaintext_policy_snapshot,
            "profile_identity": str(self.data_dir),
        }
        kwargs.update(overrides)
        return module.PlaintextExportAuthorizer(**kwargs)

    def seed_on(self, *, privacy=False, revision=VALID_REVISION):
        self.seed_known_profile(encryption=True, privacy=privacy, revision=revision)

    def seed_off(self, *, privacy=False, revision=VALID_REVISION):
        self.seed_known_profile(encryption=False, privacy=privacy, revision=revision)

    def save_flags(self, *, encryption, privacy=False):
        return self.store.save_settings(
            {ENCRYPTION_KEY: encryption, PRIVACY_KEY: privacy}
        )

    def issue_on(self, authorizer=None, session=None):
        """Сидит ON, выдаёт grant, проверяет базовый успех. Возвращает (az, grant)."""
        self.seed_on()
        authorizer = authorizer if authorizer is not None else self.make_authorizer()
        grant = authorizer.issue_grant(session or self.SESSION_A)
        self.assertTrue(grant.ok, grant)
        self.assertIsNone(grant.reason, grant)
        self.assertTrue(grant.capability, grant)
        self.assertIsInstance(grant.capability, str)
        return authorizer, grant


class TestGrantLifecycle(_AuthorizerFixture):
    """§7.4/§7.6: grant lifecycle — issue/validate/revoke, seq, receipt."""

    def test_issue_at_known_on_returns_capability_epoch_generation(self):
        authorizer, grant = self.issue_on()
        self.assertEqual(grant.epoch, authorizer.epoch)
        self.assertIsInstance(authorizer.epoch, bytes)
        self.assertEqual(len(authorizer.epoch), 32)
        self.assertEqual(grant.policy_generation, authorizer.policy_generation)
        self.assertIsInstance(authorizer.policy_generation, int)
        self.assertEqual(authorizer.active_grant_count, 1)

    def test_default_epoch_is_32_random_bytes(self):
        first = self.make_authorizer()
        second = self.make_authorizer()
        for authorizer in (first, second):
            self.assertIsInstance(authorizer.epoch, bytes)
            self.assertEqual(len(authorizer.epoch), 32)
        self.assertNotEqual(first.epoch, second.epoch)

    def test_explicit_epoch_is_used_verbatim(self):
        epoch = b"\x01" * 32
        authorizer = self.make_authorizer(epoch=epoch)
        self.assertEqual(authorizer.epoch, epoch)

    def test_validate_correct_tuple_returns_single_use_receipt(self):
        authorizer, grant = self.issue_on()
        validated = authorizer.validate(
            self.SESSION_A,
            authorizer.epoch,
            grant.capability,
            grant.policy_generation,
            1,
            self.SINK_MD,
        )
        self.assertTrue(validated.ok, validated)
        self.assertIsNone(validated.reason, validated)
        self.assertTrue(validated.receipt, validated)
        self.assertEqual(authorizer.active_receipt_count, 1)

    def test_validate_same_seq_replay_is_denied(self):
        module = self.authorizer_module()
        authorizer, grant = self.issue_on()
        first = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            grant.policy_generation, 1, self.SINK_MD,
        )
        self.assertTrue(first.ok, first)
        replay = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            grant.policy_generation, 1, self.SINK_MD,
        )
        self.assertFalse(replay.ok, replay)
        self.assertEqual(replay.reason, module.REASON_PLAINTEXT_SESSION_EXPIRED)
        self.assertIsNone(replay.receipt, replay)

    def test_validate_smaller_seq_is_denied(self):
        module = self.authorizer_module()
        authorizer, grant = self.issue_on()
        second = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            grant.policy_generation, 2, self.SINK_MD,
        )
        self.assertTrue(second.ok, second)
        smaller = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            grant.policy_generation, 1, self.SINK_MD,
        )
        self.assertFalse(smaller.ok, smaller)
        self.assertEqual(smaller.reason, module.REASON_PLAINTEXT_SESSION_EXPIRED)

    def test_operation_seq_grows_monotonically(self):
        authorizer, grant = self.issue_on()
        for seq in (1, 2, 3):
            validated = authorizer.validate(
                self.SESSION_A, authorizer.epoch, grant.capability,
                grant.policy_generation, seq, self.SINK_MD,
            )
            self.assertTrue(validated.ok, (seq, validated))

    def test_receipt_replay_is_denied(self):
        authorizer, grant = self.issue_on()
        validated = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            grant.policy_generation, 1, self.SINK_MD,
        )
        self.assertTrue(validated.ok, validated)
        self.assertTrue(authorizer.consume_receipt(validated.receipt))
        self.assertFalse(authorizer.consume_receipt(validated.receipt))
        self.assertFalse(authorizer.consume_receipt("forged-receipt"))
        self.assertEqual(authorizer.active_receipt_count, 0)

    def test_revoke_then_validate_is_denied_and_revoke_is_idempotent(self):
        module = self.authorizer_module()
        authorizer, grant = self.issue_on()
        self.assertTrue(
            authorizer.revoke(self.SESSION_A, authorizer.epoch, grant.capability)
        )
        self.assertEqual(authorizer.active_grant_count, 0)
        validated = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            grant.policy_generation, 1, self.SINK_MD,
        )
        self.assertFalse(validated.ok, validated)
        self.assertEqual(validated.reason, module.REASON_PLAINTEXT_SESSION_EXPIRED)
        # Идемпотентность: повторный revoke той же capability — успех, не ошибка.
        self.assertTrue(
            authorizer.revoke(self.SESSION_A, authorizer.epoch, grant.capability)
        )
        # Неизвестная capability — тоже успех-идемпотент, не ошибка.
        self.assertTrue(authorizer.revoke(self.SESSION_A, authorizer.epoch, "nope"))

    def test_revoke_one_session_leaves_other_untouched(self):
        authorizer, grant_a = self.issue_on(session=self.SESSION_A)
        grant_b = authorizer.issue_grant(self.SESSION_B)
        self.assertTrue(grant_b.ok, grant_b)
        self.assertNotEqual(grant_a.capability, grant_b.capability)
        self.assertTrue(
            authorizer.revoke(self.SESSION_A, authorizer.epoch, grant_a.capability)
        )
        self.assertEqual(authorizer.active_grant_count, 1)
        validated_b = authorizer.validate(
            self.SESSION_B, authorizer.epoch, grant_b.capability,
            grant_b.policy_generation, 1, self.SINK_MD,
        )
        self.assertTrue(validated_b.ok, validated_b)
        validated_a = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant_a.capability,
            grant_a.policy_generation, 1, self.SINK_MD,
        )
        self.assertFalse(validated_a.ok, validated_a)

    def test_validate_wrong_session_is_denied(self):
        module = self.authorizer_module()
        authorizer, grant = self.issue_on(session=self.SESSION_A)
        validated = authorizer.validate(
            self.SESSION_B, authorizer.epoch, grant.capability,
            grant.policy_generation, 1, self.SINK_MD,
        )
        self.assertFalse(validated.ok, validated)
        self.assertEqual(validated.reason, module.REASON_PLAINTEXT_SESSION_EXPIRED)

    def test_validate_unknown_capability_is_denied(self):
        module = self.authorizer_module()
        self.seed_on()
        authorizer = self.make_authorizer()
        validated = authorizer.validate(
            self.SESSION_A, authorizer.epoch, "forged-capability",
            authorizer.policy_generation, 1, self.SINK_MD,
        )
        self.assertFalse(validated.ok, validated)
        self.assertEqual(validated.reason, module.REASON_PLAINTEXT_SESSION_EXPIRED)
        self.assertIsNone(validated.receipt, validated)


class TestGenerationRevocation(_AuthorizerFixture):
    """Контракт п.5/9/10/15/16: revision rotate отзывает прежние grants."""

    def test_supported_save_rotates_revision_and_expires_old_grant(self):
        module = self.authorizer_module()
        authorizer, grant = self.issue_on()
        generation_then = grant.policy_generation
        self.save_flags(encryption=True)
        validated = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            generation_then, 1, self.SINK_MD,
        )
        self.assertFalse(validated.ok, validated)
        self.assertEqual(validated.reason, module.REASON_PLAINTEXT_SESSION_EXPIRED)
        self.assertEqual(authorizer.active_grant_count, 0)
        self.assertEqual(authorizer.policy_generation, generation_then + 1)

    def test_on_off_on_without_export_revokes_old_grant(self):
        """ON→OFF→ON без export между переходами: старый grant отозван (п.16)."""
        module = self.authorizer_module()
        authorizer, grant = self.issue_on()
        generation_then = grant.policy_generation
        self.save_flags(encryption=False)
        self.save_flags(encryption=True)
        validated = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            generation_then, 1, self.SINK_MD,
        )
        self.assertFalse(validated.ok, validated)
        self.assertEqual(validated.reason, module.REASON_PLAINTEXT_SESSION_EXPIRED)
        # Ровно один bump на детект, а не по числу переходов.
        self.assertEqual(authorizer.policy_generation, generation_then + 1)

    def test_new_grant_after_rotate_validates(self):
        """Новый sheet после rotate работает: generation продвинулась, consent свежий."""
        authorizer, grant = self.issue_on()
        self.save_flags(encryption=True)
        stale = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            grant.policy_generation, 1, self.SINK_MD,
        )
        self.assertFalse(stale.ok, stale)
        fresh = authorizer.issue_grant(self.SESSION_B)
        self.assertTrue(fresh.ok, fresh)
        self.assertGreater(fresh.policy_generation, grant.policy_generation)
        validated = authorizer.validate(
            self.SESSION_B, authorizer.epoch, fresh.capability,
            fresh.policy_generation, 1, self.SINK_MD,
        )
        self.assertTrue(validated.ok, validated)


class TestUnknownRevocation(_AuthorizerFixture):
    """Контракт п.4/5 (RED 6): UNKNOWN чистит grants, возврата старого нет."""

    def test_delete_settings_after_issue_gives_unavailable_and_stays_revoked(self):
        module = self.authorizer_module()
        authorizer, grant = self.issue_on()
        settings_path = self.data_dir / "settings.json"
        raw = settings_path.read_bytes()
        settings_path.unlink()
        try:
            validated = authorizer.validate(
                self.SESSION_A, authorizer.epoch, grant.capability,
                grant.policy_generation, 1, self.SINK_MD,
            )
            self.assertFalse(validated.ok, validated)
            self.assertEqual(
                validated.reason, module.REASON_PLAINTEXT_POLICY_UNAVAILABLE
            )
            self.assertEqual(authorizer.active_grant_count, 0)
            # Повторный validate не «воскрешает» grant: снова отказ, не успех.
            again = authorizer.validate(
                self.SESSION_A, authorizer.epoch, grant.capability,
                grant.policy_generation, 1, self.SINK_MD,
            )
            self.assertFalse(again.ok, again)
            self.assertEqual(
                again.reason, module.REASON_PLAINTEXT_POLICY_UNAVAILABLE
            )
        finally:
            settings_path.write_bytes(raw)

    def test_restore_same_bytes_old_grant_still_denied(self):
        """Возврат прежних байтов НЕ возвращает старое согласие (п.5)."""
        module = self.authorizer_module()
        authorizer, grant = self.issue_on()
        settings_path = self.data_dir / "settings.json"
        raw = settings_path.read_bytes()
        settings_path.unlink()
        dropped = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            grant.policy_generation, 1, self.SINK_MD,
        )
        self.assertFalse(dropped.ok, dropped)
        settings_path.write_bytes(raw)
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, module.PolicyState.KNOWN_ON, snapshot)
        revived = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            grant.policy_generation, 1, self.SINK_MD,
        )
        self.assertFalse(revived.ok, revived)
        self.assertEqual(revived.reason, module.REASON_PLAINTEXT_SESSION_EXPIRED)


class TestPrivacyGates(_AuthorizerFixture):
    """Контракт п.18: privacy true всегда deny, даже при KNOWN_OFF."""

    def test_issue_at_known_on_with_privacy_true_is_denied(self):
        module = self.authorizer_module()
        self.seed_on(privacy=True)
        authorizer = self.make_authorizer()
        grant = authorizer.issue_grant(self.SESSION_A)
        self.assertFalse(grant.ok, grant)
        self.assertEqual(grant.reason, module.REASON_PRIVACY_MODE_ACTIVE)
        self.assertIsNone(grant.capability, grant)
        self.assertEqual(authorizer.active_grant_count, 0)

    def test_privacy_flip_after_issue_denies_with_privacy_reason(self):
        module = self.authorizer_module()
        authorizer, grant = self.issue_on()
        self.save_flags(encryption=True, privacy=True)
        validated = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            grant.policy_generation, 1, self.SINK_MD,
        )
        self.assertFalse(validated.ok, validated)
        self.assertEqual(validated.reason, module.REASON_PRIVACY_MODE_ACTIVE)

    def test_issue_at_known_off_is_denied_without_grant(self):
        """KNOWN_OFF: grant не выдаётся (не нужен); путь — capabilityless validation."""
        module = self.authorizer_module()
        self.seed_off()
        authorizer = self.make_authorizer()
        grant = authorizer.issue_grant(self.SESSION_A)
        self.assertFalse(grant.ok, grant)
        self.assertEqual(
            grant.reason, module.REASON_PLAINTEXT_CONFIRMATION_REQUIRED
        )
        self.assertIsNone(grant.capability, grant)
        self.assertEqual(authorizer.active_grant_count, 0)

    def test_validate_without_capability_at_confirmed_off_is_allowed(self):
        """Подтверждённый OFF не требует capability (§7.6), но требует fresh validation."""
        authorizer = self.make_authorizer()
        self.seed_off()
        generation = authorizer.policy_generation
        validated = authorizer.validate(
            self.SESSION_A, authorizer.epoch, None, generation, 1, self.SINK_MD
        )
        self.assertTrue(validated.ok, validated)
        self.assertTrue(validated.receipt, validated)
        self.assertTrue(authorizer.consume_receipt(validated.receipt))
        self.assertFalse(authorizer.consume_receipt(validated.receipt))
        # Повтор seq и меньший seq — отказ и на OFF-пути.
        module = self.authorizer_module()
        replay = authorizer.validate(
            self.SESSION_A, authorizer.epoch, None, generation, 1, self.SINK_MD
        )
        self.assertFalse(replay.ok, replay)
        self.assertEqual(replay.reason, module.REASON_PLAINTEXT_SESSION_EXPIRED)

    def test_validate_without_capability_at_on_is_denied(self):
        module = self.authorizer_module()
        self.seed_on()
        authorizer = self.make_authorizer()
        validated = authorizer.validate(
            self.SESSION_A, authorizer.epoch, None,
            authorizer.policy_generation, 1, self.SINK_MD,
        )
        self.assertFalse(validated.ok, validated)
        self.assertEqual(
            validated.reason, module.REASON_PLAINTEXT_CONFIRMATION_REQUIRED
        )

    def test_privacy_true_at_off_denies_capabilityless_validate(self):
        module = self.authorizer_module()
        self.seed_off(privacy=True)
        authorizer = self.make_authorizer()
        validated = authorizer.validate(
            self.SESSION_A, authorizer.epoch, None,
            authorizer.policy_generation, 1, self.SINK_MD,
        )
        self.assertFalse(validated.ok, validated)
        self.assertEqual(validated.reason, module.REASON_PRIVACY_MODE_ACTIVE)
        grant = authorizer.issue_grant(self.SESSION_A)
        self.assertFalse(grant.ok, grant)
        self.assertEqual(grant.reason, module.REASON_PRIVACY_MODE_ACTIVE)


class TestEpochAndCrashSemantics(_AuthorizerFixture):
    """Контракт п.1/19 (RED 4): epoch на процесс, replay запрещён."""

    def test_two_authorizers_have_different_epochs(self):
        first = self.make_authorizer()
        second = self.make_authorizer()
        self.assertNotEqual(first.epoch, second.epoch)

    def test_grant_of_one_authorizer_is_rejected_by_other(self):
        module = self.authorizer_module()
        authorizer, grant = self.issue_on()
        other = self.make_authorizer()
        self.assertNotEqual(other.epoch, authorizer.epoch)
        # Чужой epoch (replay epoch запрещён) — отказ.
        replayed = other.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            grant.policy_generation, 1, self.SINK_MD,
        )
        self.assertFalse(replayed.ok, replayed)
        self.assertEqual(replayed.reason, module.REASON_PLAINTEXT_SESSION_EXPIRED)
        # Свой epoch, но чужая capability — тоже отказ.
        forged = other.validate(
            self.SESSION_A, other.epoch, grant.capability,
            other.policy_generation, 1, self.SINK_MD,
        )
        self.assertFalse(forged.ok, forged)

    def test_new_session_id_rejects_old_token(self):
        """Simulated crash: новый app_session_id не принимает старый token."""
        module = self.authorizer_module()
        authorizer, grant = self.issue_on(session=self.SESSION_A)
        validated = authorizer.validate(
            self.SESSION_B, authorizer.epoch, grant.capability,
            grant.policy_generation, 1, self.SINK_MD,
        )
        self.assertFalse(validated.ok, validated)
        self.assertEqual(validated.reason, module.REASON_PLAINTEXT_SESSION_EXPIRED)


class TestTrustBoundaryIsHonest(_AuthorizerFixture):
    """Контракт п.4: честная граница — Python API НЕ различает sheet и self-grant."""

    def test_same_uid_caller_can_issue_grant_directly(self):
        """Документирует границу, а НЕ утверждает «headless impersonation denied».

        Прямой вызов ``issue_grant`` из этого же процесса (тот же UID, тот же
        Python API, которым пользуется BackendService) технически выдаёт grant:
        backend НЕ отличит headless self-grant от Swift sheet (§7.6). Это защита
        от accidental export (supported Swift зовёт grant только после sheet,
        supported scripts grant автоматически не запрашивают — проверено поиском
        по коду и зафиксировано в отчёте, а не AST-тестом), НЕ запрет всех
        программных self-grant. Усиленная peer-auth гарантия — отдельный дизайн
        (BLOCK в контракте), не часть A5.3.
        """
        authorizer, grant = self.issue_on()
        self.assertTrue(grant.ok, grant)
        self.assertTrue(grant.capability, grant)


class TestLockOrdering(_AuthorizerFixture):
    """Контракт п.12: snapshot — ДО authorizer lock; contention → UNKNOWN, не hang."""

    def test_validate_under_contended_store_lock_with_nowait_gives_unknown_bounded(self):
        """Детерминированно через Event'ы, без sleep: holder держит EX store-lock,
        authorizer читает с nowait → UNKNOWN/provider-error → policy_unavailable."""
        module = self.authorizer_module()
        self.seed_on()
        authorizer = self.make_authorizer(
            read_snapshot=functools.partial(
                self.store.read_plaintext_policy_snapshot, nowait=True
            )
        )
        grant = authorizer.issue_grant(self.SESSION_A)
        self.assertTrue(grant.ok, grant)
        holding = threading.Event()
        release = threading.Event()
        holder_state: dict = {}

        def holder():
            try:
                with self.store._lock():
                    holding.set()
                    release.wait(timeout=15.0)
            except BaseException as exc:  # noqa: BLE001 — диагноз в тест
                holder_state["error"] = exc

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        try:
            self.assertTrue(holding.wait(timeout=15.0), "holder не взял store lock")
            validated = authorizer.validate(
                self.SESSION_A, authorizer.epoch, grant.capability,
                grant.policy_generation, 1, self.SINK_MD,
            )
        finally:
            release.set()
            thread.join(timeout=15.0)
        self.assertNotIn("error", holder_state, holder_state)
        self.assertFalse(thread.is_alive(), "store-lock holder завис")
        self.assertFalse(validated.ok, validated)
        self.assertEqual(
            validated.reason, module.REASON_PLAINTEXT_POLICY_UNAVAILABLE
        )
        self.assertEqual(authorizer.active_grant_count, 0)


class TestBackendServiceAuthorizerWiring(_AuthorizerFixture):
    """Wiring slice 2: создание в __init__ (БЕЗ IPC), новый epoch на сервис."""

    def _build(self, name):
        from backend.service import build_service

        target = Path(self._tmp.name) / name
        service = build_service(target)
        self.addCleanup(service.close)
        return service

    def test_two_fresh_backend_services_have_different_epochs(self):
        module = self.authorizer_module()
        first = self._build("svc-epoch-a")
        second = self._build("svc-epoch-b")
        for service in (first, second):
            authorizer = service._plaintext_export_authorizer
            self.assertIsInstance(authorizer, module.PlaintextExportAuthorizer)
            self.assertIsInstance(authorizer.epoch, bytes)
            self.assertEqual(len(authorizer.epoch), 32)
            self.assertEqual(
                authorizer.profile_identity, str(service.store.data_dir)
            )
        self.assertNotEqual(
            first._plaintext_export_authorizer.epoch,
            second._plaintext_export_authorizer.epoch,
        )

    def test_constructor_does_no_policy_io_on_known_profile(self):
        """Конструктор не делает policy-I/O сверх существующего: байты settings
        известного профиля не меняются (авто-сид пропускается — hotwords уже есть)."""
        from backend.service import build_service

        payload = _profile(False, False, VALID_REVISION)
        payload["stt_hotwords"] = ["seeded"]
        self.write_settings(payload)
        before = (self.data_dir / "settings.json").read_bytes()
        service = build_service(self.data_dir)
        self.addCleanup(service.close)
        self.assertEqual((self.data_dir / "settings.json").read_bytes(), before)
        snapshot = self.store.read_plaintext_policy_snapshot()
        self.assertIs(snapshot.state, module_state_off())

    def test_read_history_show_settings_status_create_no_grants(self):
        """Read/show/settings-status НЕ создают grants и НЕ мешают живому grant."""
        from backend.service import build_service

        self.seed_on()
        service = build_service(self.data_dir)
        self.addCleanup(service.close)
        authorizer = service._plaintext_export_authorizer
        grant = authorizer.issue_grant(self.SESSION_A)
        self.assertTrue(grant.ok, grant)
        responses = [
            service.handle_request(
                {"id": 1, "method": "get_history_page", "params": {"limit": 5}}
            ),
            service.handle_request({"id": 2, "method": "get_settings", "params": {}}),
            service.handle_request(
                {"id": 3, "method": "get_diagnostics", "params": {}}
            ),
        ]
        for response in responses:
            self.assertIsInstance(response, dict, response)
        self.assertEqual(authorizer.active_grant_count, 1)
        self.assertEqual(authorizer.active_receipt_count, 0)
        validated = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            grant.policy_generation, 1, self.SINK_MD,
        )
        self.assertTrue(validated.ok, validated)


def module_state_off():
    """Ленивый доступ к PolicyState.KNOWN_OFF (модуль slice 2/типизация)."""
    return _authorizer_module().PolicyState.KNOWN_OFF


class TestAuthorizerSerializationHygiene(_AuthorizerFixture):
    """Контракт п.15b (authorizer-сторона): capability/receipt/session — только RAM."""

    def test_settings_and_profile_contain_only_internal_revision(self):
        module = self.authorizer_module()
        authorizer, grant = self.issue_on()
        validated = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            grant.policy_generation, 1, self.SINK_MD,
        )
        self.assertTrue(validated.ok, validated)
        self.assertTrue(authorizer.consume_receipt(validated.receipt))
        settings_text = (self.data_dir / "settings.json").read_text(encoding="utf-8")
        parsed = json.loads(settings_text)
        for secret in (grant.capability, validated.receipt, self.SESSION_A):
            self.assertNotIn(secret, settings_text)
            self.assertNotIn(secret, json.dumps(parsed))
        # Персистентна только internal revision — НЕ secret/capability.
        self.assertIn(module.POLICY_REVISION_KEY, parsed)
        for name in os.listdir(self.data_dir):
            self.assertNotIn(self.SESSION_A, name)
        # repr'ы и сериализации НЕ содержат capability/secret.
        for obj in (
            authorizer,
            grant,
            validated,
            repr(authorizer),
            repr(grant),
            repr(validated),
        ):
            self.assertNotIn(grant.capability, repr(obj))
            self.assertNotIn(validated.receipt, repr(obj))


# ── Cross-process fixtures 5a/5b/5c (multiprocessing spawn, без sleep) ──


def _worker_5a_granter(data_dir_s, ready_evt, done_evt, result_q):
    """5a/A: явная инициализация профиля, ON, issue; validation после B."""
    from pathlib import Path

    _child_preamble()
    from backend.state_store import StateStore
    from backend import plaintext_export_authorization as AZ

    try:
        data_dir = Path(data_dir_s)
        store = StateStore(data_dir)
        outcome = store.initialize_startup_plaintext_policy(new_profile=True)
        authorizer = AZ.PlaintextExportAuthorizer(
            read_snapshot=store.read_plaintext_policy_snapshot,
            profile_identity=str(data_dir),
        )
        store.save_settings(
            {"history_encryption_enabled": True, "privacy_mode_enabled": False}
        )
        snapshot = store.read_plaintext_policy_snapshot()
        if snapshot.state is not AZ.PolicyState.KNOWN_ON:
            result_q.put({"error": "not_on_after_save"})
            return
        grant = authorizer.issue_grant("5a-session")
        if not grant.ok:
            result_q.put({"error": "issue_failed"})
            return
        generation_then = grant.policy_generation
        before = sorted(path.name for path in data_dir.iterdir())
        ready_evt.set()
        if not done_evt.wait(timeout=30.0):
            result_q.put({"error": "b_timeout"})
            return
        validated = authorizer.validate(
            "5a-session", authorizer.epoch, grant.capability,
            generation_then, 1, "history-md",
        )
        after = sorted(path.name for path in data_dir.iterdir())
        result_q.put({
            "denied": not validated.ok,
            "reason": validated.reason,
            "generation_then": generation_then,
            "generation_now": authorizer.policy_generation,
            "writes_stable": before == after,
            "startup_outcome": outcome,
        })
    except BaseException as exc:  # noqa: BLE001 — диагноз в parent
        result_q.put({"error": type(exc).__name__})


def _worker_5a_changer(data_dir_s, ready_evt, done_evt):
    """5a/B: поддержанные save OFF, затем ON (оба через центральный commit)."""
    from pathlib import Path

    _child_preamble()
    from backend.state_store import StateStore

    store = StateStore(Path(data_dir_s))
    if not ready_evt.wait(timeout=30.0):
        raise RuntimeError("5a: grant not ready")
    store.save_settings(
        {"history_encryption_enabled": False, "privacy_mode_enabled": False}
    )
    store.save_settings(
        {"history_encryption_enabled": True, "privacy_mode_enabled": False}
    )
    done_evt.set()


def _worker_5b_granter(data_dir_s, ready_evt, replaced_evt, ack_evt, recovered_evt, result_q):
    """5b/A: issue ON; validation после synthetic replace и после supported recovery."""
    from pathlib import Path

    _child_preamble()
    from backend.state_store import StateStore
    from backend import plaintext_export_authorization as AZ

    try:
        data_dir = Path(data_dir_s)
        store = StateStore(data_dir)
        authorizer = AZ.PlaintextExportAuthorizer(
            read_snapshot=store.read_plaintext_policy_snapshot,
            profile_identity=str(data_dir),
        )
        grant = authorizer.issue_grant("5b-session")
        if not grant.ok:
            result_q.put({"error": "issue_failed"})
            return
        generation_then = grant.policy_generation
        ready_evt.set()
        if not replaced_evt.wait(timeout=30.0):
            result_q.put({"error": "replace_timeout"})
            return
        first = authorizer.validate(
            "5b-session", authorizer.epoch, grant.capability,
            generation_then, 1, "history-md",
        )
        ack_evt.set()
        if not recovered_evt.wait(timeout=30.0):
            result_q.put({"error": "recovery_timeout"})
            return
        second = authorizer.validate(
            "5b-session", authorizer.epoch, grant.capability,
            generation_then, 2, "history-md",
        )
        result_q.put({
            "first_denied": not first.ok,
            "first_reason": first.reason,
            "second_denied": not second.ok,
            "second_reason": second.reason,
            "generation_then": generation_then,
            "generation_now": authorizer.policy_generation,
        })
    except BaseException as exc:  # noqa: BLE001 — диагноз в parent
        result_q.put({"error": type(exc).__name__})


def _worker_5b_changer(data_dir_s, ready_evt, replaced_evt, ack_evt, recovered_evt):
    """5b/B: synthetic побайтовая замена мимо commit, затем поддержанный save."""
    import os
    from pathlib import Path

    _child_preamble()
    from backend.state_store import StateStore

    data_dir = Path(data_dir_s)
    store = StateStore(data_dir)
    if not ready_evt.wait(timeout=30.0):
        raise RuntimeError("5b: grant not ready")
    settings_path = data_dir / "settings.json"
    raw = settings_path.read_bytes()
    tmp_path = data_dir / "settings.json.tmp-5b-synthetic"
    tmp_path.write_bytes(raw)
    os.replace(tmp_path, settings_path)
    replaced_evt.set()
    if not ack_evt.wait(timeout=30.0):
        raise RuntimeError("5b: phase-1 ack never came")
    store.save_settings(
        {"history_encryption_enabled": True, "privacy_mode_enabled": False}
    )
    recovered_evt.set()


def _worker_5c_validater(
    data_dir_s, grant_ready_evt, lock_held_evt, attempted_evt, committed_evt, result_q
):
    """5c/A: validation с bounded timeout под удерживаемым B локом → UNKNOWN."""
    from pathlib import Path

    _child_preamble()
    from backend.state_store import StateStore
    from backend import plaintext_export_authorization as AZ

    try:
        data_dir = Path(data_dir_s)
        store = StateStore(data_dir)
        bounded_read = functools.partial(
            store.read_plaintext_policy_snapshot, timeout_sec=5.0
        )
        authorizer = AZ.PlaintextExportAuthorizer(
            read_snapshot=bounded_read,
            profile_identity=str(data_dir),
        )
        grant = authorizer.issue_grant("5c-session")
        if not grant.ok:
            result_q.put({"error": "issue_failed"})
            return
        generation_then = grant.policy_generation
        grant_ready_evt.set()
        if not lock_held_evt.wait(timeout=30.0):
            result_q.put({"error": "lock_timeout"})
            return
        started = time.monotonic()
        first = authorizer.validate(
            "5c-session", authorizer.epoch, grant.capability,
            generation_then, 1, "history-md",
        )
        elapsed = time.monotonic() - started
        attempted_evt.set()
        if not committed_evt.wait(timeout=30.0):
            result_q.put({"error": "commit_timeout"})
            return
        fresh_grant = authorizer.issue_grant("5c-session-2")
        fresh_validated_ok = False
        if fresh_grant.ok:
            fresh_validated = authorizer.validate(
                "5c-session-2", authorizer.epoch, fresh_grant.capability,
                fresh_grant.policy_generation, 1, "history-md",
            )
            fresh_validated_ok = bool(fresh_validated.ok)
        result_q.put({
            "first_denied": not first.ok,
            "first_reason": first.reason,
            "first_elapsed_sec": round(elapsed, 2),
            "fresh_issue_ok": bool(fresh_grant.ok),
            "fresh_validate_ok": fresh_validated_ok,
            "generation_then": generation_then,
            "generation_now": authorizer.policy_generation,
        })
    except BaseException as exc:  # noqa: BLE001 — диагноз в parent
        result_q.put({"error": type(exc).__name__})


def _worker_5c_locker(
    data_dir_s, grant_ready_evt, lock_held_evt, attempted_evt, committed_evt
):
    """5c/B: долгий EX store-lock через Barrier/Event, затем commit + release."""
    from pathlib import Path

    _child_preamble()
    from backend.state_store import StateStore

    store = StateStore(Path(data_dir_s))
    if not grant_ready_evt.wait(timeout=30.0):
        raise RuntimeError("5c: grant not ready")
    with store._lock():
        lock_held_evt.set()
        if not attempted_evt.wait(timeout=30.0):
            raise RuntimeError("5c: validation attempt never came")
    store.save_settings(
        {"history_encryption_enabled": True, "privacy_mode_enabled": False}
    )
    committed_evt.set()


class TestCrossProcessAuthorizerFixtures(unittest.TestCase):
    """Контракт RED 5a/5b/5c: spawn-процессы, независимые StateStore+Authorizer.

    Дети импортируют ТОЛЬКО ``backend.state_store`` +
    ``backend.plaintext_export_authorization`` (+ stdlib): ``backend.service``
    запрещён (тяжёлые ML-зависимости → RAM). Синхронизация — Pipe/Event/Barrier
    (здесь — Event/Queue), никакого sleep. Явно ``spawn`` (macOS).
    terminate/join в finally — ТОЛЬКО свои fixture-PID.
    """

    EVT_WAIT_SEC = 60.0
    JOIN_SEC = 120.0

    def _run_pair(self, target_a, args_a, target_b, args_b):
        ctx = multiprocessing.get_context("spawn")
        proc_a = ctx.Process(target=target_a, args=args_a)
        proc_b = ctx.Process(target=target_b, args=args_b)
        proc_a.start()
        proc_b.start()
        try:
            proc_a.join(timeout=self.JOIN_SEC)
            proc_b.join(timeout=self.JOIN_SEC)
            self.assertFalse(proc_a.is_alive(), "worker A завис (join timeout)")
            self.assertFalse(proc_b.is_alive(), "worker B завис (join timeout)")
            self.assertEqual(proc_a.exitcode, 0, "worker A упал")
            self.assertEqual(proc_b.exitcode, 0, "worker B упал")
        finally:
            for proc in (proc_a, proc_b):
                if proc.is_alive():
                    proc.terminate()
                proc.join(timeout=10.0)
                proc.close()

    def test_5a_cross_process_supported_off_on_revokes_grant(self):
        """5a: A issue при ON; B supported OFF→ON; первая validation A →
        session_expired, 0 writes. Никаких общих Python store/mock и sleep."""
        module = _authorizer_module()
        with tempfile.TemporaryDirectory(prefix="a53-5a-") as raw:
            ctx = multiprocessing.get_context("spawn")
            ready = ctx.Event()
            done = ctx.Event()
            results = ctx.Queue()
            self._run_pair(
                _worker_5a_granter, (raw, ready, done, results),
                _worker_5a_changer, (raw, ready, done),
            )
            result = results.get(timeout=self.EVT_WAIT_SEC)
            self.assertNotIn("error", result, result)
            self.assertTrue(result["denied"], result)
            self.assertEqual(
                result["reason"], module.REASON_PLAINTEXT_SESSION_EXPIRED, result
            )
            self.assertGreater(
                result["generation_now"], result["generation_then"], result
            )
            self.assertTrue(result["writes_stable"], result)

    def test_5b_cross_process_synthetic_replace_and_supported_recovery_revoke(self):
        """5b: B атомарно заменяет settings теми же байтами мимо commit —
        A ловит новый fingerprint (ino/ctime) и отказывает; поддержанная
        recovery с новой ревизией — тоже revoke старого grant."""
        module = _authorizer_module()
        with tempfile.TemporaryDirectory(prefix="a53-5b-") as raw:
            data_dir = Path(raw)
            _seed_known_profile(data_dir, encryption=True, privacy=False)
            ctx = multiprocessing.get_context("spawn")
            ready = ctx.Event()
            replaced = ctx.Event()
            ack = ctx.Event()
            recovered = ctx.Event()
            results = ctx.Queue()
            self._run_pair(
                _worker_5b_granter, (raw, ready, replaced, ack, recovered, results),
                _worker_5b_changer, (raw, ready, replaced, ack, recovered),
            )
            result = results.get(timeout=self.EVT_WAIT_SEC)
            self.assertNotIn("error", result, result)
            self.assertTrue(result["first_denied"], result)
            self.assertEqual(
                result["first_reason"], module.REASON_PLAINTEXT_SESSION_EXPIRED,
                result,
            )
            self.assertTrue(result["second_denied"], result)
            self.assertEqual(
                result["second_reason"], module.REASON_PLAINTEXT_SESSION_EXPIRED,
                result,
            )
            self.assertGreater(
                result["generation_now"], result["generation_then"], result
            )

    def test_5c_cross_process_lock_hold_gives_unknown_then_consistent_snapshot(self):
        """5c: B держит EX store-lock — validation A с bounded timeout даёт
        UNKNOWN (не старый cache, не hang); после commit/release A читает
        новый consistent snapshot (новый grant валидируется)."""
        module = _authorizer_module()
        with tempfile.TemporaryDirectory(prefix="a53-5c-") as raw:
            data_dir = Path(raw)
            _seed_known_profile(data_dir, encryption=True, privacy=False)
            ctx = multiprocessing.get_context("spawn")
            grant_ready = ctx.Event()
            lock_held = ctx.Event()
            attempted = ctx.Event()
            committed = ctx.Event()
            results = ctx.Queue()
            self._run_pair(
                _worker_5c_validater,
                (raw, grant_ready, lock_held, attempted, committed, results),
                _worker_5c_locker,
                (raw, grant_ready, lock_held, attempted, committed),
            )
            result = results.get(timeout=self.EVT_WAIT_SEC)
            self.assertNotIn("error", result, result)
            self.assertTrue(result["first_denied"], result)
            self.assertEqual(
                result["first_reason"], module.REASON_PLAINTEXT_POLICY_UNAVAILABLE,
                result,
            )
            # Bounded, а не hang: timeout чтения 5 с + запас на spawn/flock.
            self.assertLess(result["first_elapsed_sec"], 30.0, result)
            self.assertTrue(result["fresh_issue_ok"], result)
            self.assertTrue(result["fresh_validate_ok"], result)
            self.assertGreater(
                result["generation_now"], result["generation_then"], result
            )

    def test_child_imports_stay_light_without_torch_mlx_or_service(self):
        """Дети 5a-c обязаны стартовать без torch/mlx/backend.service (RAM)."""
        marker = "A53_CHILD_IMPORT_PROBE"
        code = (
            "import sys, time; "
            "t0 = time.perf_counter(); "
            "import backend.state_store; "
            "import backend.plaintext_export_authorization; "
            "dt = time.perf_counter() - t0; "
            "heavy = sorted(m for m in sys.modules "
            "if m.split('.')[0] in ('torch', 'mlx', 'mlx_whisper') "
            "or m == 'backend.service' "
            "or m.startswith('backend.service.')); "
            "print('%s dt=%%.2f heavy=%%s modules=%%d' %% (dt, heavy, len(sys.modules))); "
            "sys.exit(1 if heavy else 0)"
        ) % marker
        env = dict(os.environ)
        krabear = str(Path.cwd() / "KrabEar")
        env["PYTHONPATH"] = krabear + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=120,
            env=env, cwd=str(Path.cwd()),
        )
        tail = (proc.stdout + proc.stderr)[-2000:]
        self.assertEqual(proc.returncode, 0, tail)
        self.assertIn(marker, proc.stdout, tail)


# ═══════════════════════════════════════════════════════════════════════════
# Review pins (reviewer + gate-security BLOCK): snapshot-first issue/consume,
# seq/sink/capability fail-closed, epoch strictness, fingerprint None guard,
# token length, single-revoke generation immutability, profile_identity wiring.
# Только behavioral, без AST/source-inspection, без BackendService.
# ═══════════════════════════════════════════════════════════════════════════


class TestReviewPinSeqTypes(_AuthorizerFixture):
    """FIX 3а: seq ∈ {0, -1, True, 1.0, "1", None} → denied (пины)."""

    def test_nonstandard_seq_variants_are_denied(self):
        module = self.authorizer_module()
        authorizer, grant = self.issue_on()
        for bad_seq in (0, -1, True, 1.0, "1", None, False, 0.0, [], {}):
            with self.subTest(seq=repr(bad_seq)):
                denied = authorizer.validate(
                    self.SESSION_A, authorizer.epoch, grant.capability,
                    grant.policy_generation, bad_seq, self.SINK_MD,
                )
                self.assertFalse(denied.ok, (bad_seq, denied))
                self.assertEqual(
                    denied.reason, module.REASON_PLAINTEXT_SESSION_EXPIRED,
                    (bad_seq, denied),
                )
                self.assertIsNone(denied.receipt, (bad_seq, denied))


class TestReviewPinSinkAndCapabilityTypes(_AuthorizerFixture):
    """FIX 3е: пустой sink_kind / non-str capability → denied fail-closed."""

    def test_empty_sink_kind_is_denied(self):
        module = self.authorizer_module()
        authorizer, grant = self.issue_on()
        for bad_sink in ("", None, 123, b"history-md", [], {}):
            with self.subTest(sink=repr(bad_sink)):
                denied = authorizer.validate(
                    self.SESSION_A, authorizer.epoch, grant.capability,
                    grant.policy_generation, 1, bad_sink,
                )
                self.assertFalse(denied.ok, (bad_sink, denied))
                self.assertEqual(
                    denied.reason, module.REASON_PLAINTEXT_SESSION_EXPIRED,
                    (bad_sink, denied),
                )
                self.assertIsNone(denied.receipt, (bad_sink, denied))

    def test_non_str_capability_at_on_is_denied(self):
        module = self.authorizer_module()
        authorizer, grant = self.issue_on()
        for bad_cap in (123, b"cap", 1.0, True, [], {}, ("x",)):
            with self.subTest(cap=repr(bad_cap)):
                denied = authorizer.validate(
                    self.SESSION_A, authorizer.epoch, bad_cap,
                    grant.policy_generation, 1, self.SINK_MD,
                )
                self.assertFalse(denied.ok, (bad_cap, denied))
                self.assertEqual(
                    denied.reason, module.REASON_PLAINTEXT_SESSION_EXPIRED,
                    (bad_cap, denied),
                )
                self.assertIsNone(denied.receipt, (bad_cap, denied))


class TestReviewPinIssueRotateWithoutValidate(_AuthorizerFixture):
    """FIX 3б: issue → rotate → issue БЕЗ validate: старый мёртв, новый работает."""

    def test_issue_rotate_issue_without_validate(self):
        module = self.authorizer_module()
        authorizer, old = self.issue_on()
        gen0 = old.policy_generation
        self.save_flags(encryption=True)
        fresh = authorizer.issue_grant(self.SESSION_A)
        self.assertTrue(fresh.ok, fresh)
        self.assertIsNone(fresh.reason, fresh)
        self.assertGreater(fresh.policy_generation, gen0)
        self.assertEqual(fresh.policy_generation, gen0 + 1)
        self.assertEqual(authorizer.active_grant_count, 1)
        stale = authorizer.validate(
            self.SESSION_A, authorizer.epoch, old.capability,
            gen0, 1, self.SINK_MD,
        )
        self.assertFalse(stale.ok, stale)
        self.assertEqual(stale.reason, module.REASON_PLAINTEXT_SESSION_EXPIRED)
        validated = authorizer.validate(
            self.SESSION_A, authorizer.epoch, fresh.capability,
            fresh.policy_generation, 1, self.SINK_MD,
        )
        self.assertTrue(validated.ok, validated)


class TestReviewPinMalformedIssueDuringUnknown(_AuthorizerFixture):
    """FIX 3в (пинит FIX 1): issue("") во время UNKNOWN/privacy всё равно чистит."""

    def test_malformed_issue_during_unknown_still_cleans(self):
        module = self.authorizer_module()
        authorizer, grant = self.issue_on()
        gen0 = grant.policy_generation
        self.assertEqual(authorizer.active_grant_count, 1)
        settings_path = self.data_dir / "settings.json"
        raw = settings_path.read_bytes()
        settings_path.unlink()
        try:
            denied = authorizer.issue_grant("")
            self.assertFalse(denied.ok, denied)
            self.assertEqual(
                denied.reason, module.REASON_PLAINTEXT_POLICY_UNAVAILABLE, denied
            )
            self.assertEqual(authorizer.active_grant_count, 0)
            self.assertGreater(authorizer.policy_generation, gen0)
            self.assertEqual(authorizer.policy_generation, gen0 + 1)
        finally:
            settings_path.write_bytes(raw)

    def test_malformed_issue_during_privacy_still_cleans(self):
        module = self.authorizer_module()
        authorizer, grant = self.issue_on()
        gen0 = grant.policy_generation
        self.save_flags(encryption=True, privacy=True)
        denied = authorizer.issue_grant("")
        self.assertFalse(denied.ok, denied)
        self.assertEqual(denied.reason, module.REASON_PRIVACY_MODE_ACTIVE, denied)
        self.assertEqual(authorizer.active_grant_count, 0)
        self.assertGreater(authorizer.policy_generation, gen0)


class TestReviewPinConsumeAfterRevokeTrigger(_AuthorizerFixture):
    """FIX 3г (пинит FIX 2): validate → revoke-триггер → consume == False."""

    def test_consume_after_delete_is_false(self):
        authorizer, grant = self.issue_on()
        gen0 = grant.policy_generation
        validated = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            gen0, 1, self.SINK_MD,
        )
        self.assertTrue(validated.ok, validated)
        receipt = validated.receipt
        settings_path = self.data_dir / "settings.json"
        raw = settings_path.read_bytes()
        settings_path.unlink()
        try:
            self.assertFalse(authorizer.consume_receipt(receipt))
            self.assertEqual(authorizer.active_grant_count, 0)
            self.assertEqual(authorizer.active_receipt_count, 0)
            self.assertGreater(authorizer.policy_generation, gen0)
        finally:
            settings_path.write_bytes(raw)

    def test_consume_after_privacy_flip_is_false(self):
        authorizer, grant = self.issue_on()
        gen0 = grant.policy_generation
        validated = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            gen0, 1, self.SINK_MD,
        )
        self.assertTrue(validated.ok, validated)
        receipt = validated.receipt
        self.save_flags(encryption=True, privacy=True)
        self.assertFalse(authorizer.consume_receipt(receipt))
        self.assertEqual(authorizer.active_grant_count, 0)
        self.assertEqual(authorizer.active_receipt_count, 0)

    def test_consume_after_rotate_generation_drift_is_false(self):
        authorizer, grant = self.issue_on()
        gen0 = grant.policy_generation
        validated = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            gen0, 1, self.SINK_MD,
        )
        self.assertTrue(validated.ok, validated)
        receipt = validated.receipt
        self.save_flags(encryption=True)
        self.assertFalse(authorizer.consume_receipt(receipt))
        self.assertEqual(authorizer.active_receipt_count, 0)


class TestReviewPinForgedCapabilityDuringUnknown(_AuthorizerFixture):
    """FIX 3д: forged/чужой capability при UNKNOWN → policy_unavailable + bump."""

    def test_forged_capability_during_unknown_gives_unavailable(self):
        module = self.authorizer_module()
        authorizer, grant = self.issue_on()
        gen0 = grant.policy_generation
        settings_path = self.data_dir / "settings.json"
        raw = settings_path.read_bytes()
        settings_path.unlink()
        try:
            denied = authorizer.validate(
                self.SESSION_A, authorizer.epoch, "forged-capability",
                gen0, 1, self.SINK_MD,
            )
            self.assertFalse(denied.ok, denied)
            self.assertEqual(
                denied.reason, module.REASON_PLAINTEXT_POLICY_UNAVAILABLE, denied
            )
            self.assertIsNone(denied.receipt, denied)
            self.assertEqual(authorizer.active_grant_count, 0)
            self.assertGreater(authorizer.policy_generation, gen0)
        finally:
            settings_path.write_bytes(raw)


class TestReviewPinTokenLengthAndRevokeGeneration(_AuthorizerFixture):
    """FIX 4: длина токена + неизменность generation после single-revoke."""

    def test_capability_and_receipt_length_match_urlsafe32(self):
        import secrets as _secrets

        expected = len(_secrets.token_urlsafe(32))
        self.assertEqual(expected, 43)
        authorizer, grant = self.issue_on()
        self.assertEqual(len(grant.capability), expected)
        validated = authorizer.validate(
            self.SESSION_A, authorizer.epoch, grant.capability,
            grant.policy_generation, 1, self.SINK_MD,
        )
        self.assertTrue(validated.ok, validated)
        self.assertEqual(len(validated.receipt), expected)

    def test_single_revoke_leaves_generation_unchanged(self):
        authorizer, grant_a = self.issue_on(session=self.SESSION_A)
        gen0 = grant_a.policy_generation
        grant_b = authorizer.issue_grant(self.SESSION_B)
        self.assertTrue(grant_b.ok, grant_b)
        self.assertEqual(grant_b.policy_generation, gen0)
        self.assertTrue(
            authorizer.revoke(self.SESSION_A, authorizer.epoch, grant_a.capability)
        )
        self.assertEqual(authorizer.policy_generation, gen0)
        self.assertEqual(authorizer.active_grant_count, 1)


class TestReviewPinEpochStrictness(_AuthorizerFixture):
    """FIX 4: конструктор принимает только bytes длиной 32, нули/list — отказ."""

    def test_zero_epoch_is_rejected(self):
        with self.assertRaises(ValueError):
            self.make_authorizer(epoch=b"\x00" * 32)

    def test_list_epoch_is_rejected(self):
        with self.assertRaises(ValueError):
            self.make_authorizer(epoch=[1] * 32)

    def test_bytearray_epoch_is_rejected(self):
        with self.assertRaises(ValueError):
            self.make_authorizer(epoch=bytearray(b"\x01" * 32))

    def test_short_and_long_epoch_are_rejected(self):
        with self.assertRaises(ValueError):
            self.make_authorizer(epoch=b"\x01" * 31)
        with self.assertRaises(ValueError):
            self.make_authorizer(epoch=b"\x01" * 33)


class TestReviewPinFingerprintNoneGuard(_AuthorizerFixture):
    """FIX 4: чужой read_snapshot с KNOWN + fingerprint None → UNKNOWN."""

    def test_known_with_none_fingerprint_is_treated_as_unknown(self):
        module = self.authorizer_module()
        self.seed_on()
        good = self.store.read_plaintext_policy_snapshot()
        self.assertIsNotNone(good.fingerprint)
        forged = module.PolicySnapshot(
            state=good.state,
            privacy_mode_enabled=False,
            internal_revision=good.internal_revision,
            fingerprint=None,
            reason=None,
        )
        authorizer = self.make_authorizer(read_snapshot=lambda: forged)
        denied_grant = authorizer.issue_grant(self.SESSION_A)
        self.assertFalse(denied_grant.ok, denied_grant)
        self.assertEqual(
            denied_grant.reason, module.REASON_PLAINTEXT_POLICY_UNAVAILABLE,
            denied_grant,
        )
        denied_validate = authorizer.validate(
            self.SESSION_A, authorizer.epoch, None,
            authorizer.policy_generation, 1, self.SINK_MD,
        )
        self.assertFalse(denied_validate.ok, denied_validate)
        self.assertEqual(
            denied_validate.reason, module.REASON_PLAINTEXT_POLICY_UNAVAILABLE,
            denied_validate,
        )


class TestReviewPinProfileIdentityMismatch(_AuthorizerFixture):
    """FIX 4: mis-wired read_snapshot (чужой profile) → mismatch→revoke, не молча."""

    def test_miswired_profile_identity_does_not_silently_grant(self):
        module = self.authorizer_module()
        self.seed_on()
        good = self.store.read_plaintext_policy_snapshot()
        self.assertIsNotNone(good.fingerprint)
        miswired = module.PolicySnapshot(
            state=good.state,
            privacy_mode_enabled=good.privacy_mode_enabled,
            internal_revision=good.internal_revision,
            fingerprint=module.PolicyFingerprint(
                profile_identity="/tmp/definitely-not-this-profile-a53",
                st_dev=good.fingerprint.st_dev,
                st_ino=good.fingerprint.st_ino,
                st_size=good.fingerprint.st_size,
                st_mtime_ns=good.fingerprint.st_mtime_ns,
                st_ctime_ns=good.fingerprint.st_ctime_ns,
                content_sha256=good.fingerprint.content_sha256,
                internal_revision=good.fingerprint.internal_revision,
            ),
            reason=None,
        )
        authorizer = self.make_authorizer(read_snapshot=lambda: miswired)
        denied = authorizer.issue_grant(self.SESSION_A)
        self.assertFalse(denied.ok, denied)
        self.assertEqual(
            denied.reason, module.REASON_PLAINTEXT_SESSION_EXPIRED, denied
        )


if __name__ == "__main__":
    unittest.main()
