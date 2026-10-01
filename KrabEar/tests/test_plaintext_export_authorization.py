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
import json
import os
import tempfile
import threading
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


if __name__ == "__main__":
    unittest.main()
