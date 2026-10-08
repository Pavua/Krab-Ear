"""Свежая policy, grant handshake и граница разрешённой локальной записи."""

import json
from dataclasses import replace
import gc
from pathlib import Path
import tempfile
import unittest
import uuid
import weakref
from unittest import mock

from backend.plaintext_export_authorization import PlaintextExportAuthorizer
from backend.state_store import StateStore


class PlaintextPolicyProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ear-policy-protocol-")
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "profile")
        self.store.initialize_startup_plaintext_policy(new_profile=True)
        self.session = str(uuid.uuid4())
        self.authorizer = PlaintextExportAuthorizer(
            read_snapshot=self.store.read_plaintext_policy_snapshot,
            profile_identity=str(self.store.data_dir),
        )

    def set_policy(self, encryption=True, privacy=False):
        self.store.save_settings({
            "history_encryption_enabled": encryption,
            "privacy_mode_enabled": privacy,
        })

    def grant(self):
        status = self.authorizer.get_policy()
        return self.authorizer.issue_grant(
            self.session,
            expected_epoch=bytes.fromhex(status["epoch"]),
            expected_policy_generation=status["policy_generation"],
        )

    def test_policy_read_is_fresh_revokes_old_grant_and_never_grants(self):
        self.set_policy()
        grant = self.grant()
        self.assertTrue(grant.ok)
        self.set_policy(encryption=False)
        status = self.authorizer.get_policy()
        self.assertTrue(status["ok"])
        self.assertIs(status["encryption_enabled"], False)
        self.assertIs(status["allowed_without_grant"], True)
        self.assertGreater(status["policy_generation"], grant.policy_generation)
        self.assertEqual(self.authorizer.active_grant_count, 0)
        self.assertNotIn("capability", status)

    def test_policy_change_after_sheet_rejects_grant(self):
        self.set_policy()
        observed = self.authorizer.get_policy()
        # Даже сохранение тех же bool создаёт новую durable revision.
        self.set_policy()
        result = self.authorizer.issue_grant(
            self.session,
            expected_epoch=bytes.fromhex(observed["epoch"]),
            expected_policy_generation=observed["policy_generation"],
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "plaintext_session_expired")
        self.assertEqual(self.authorizer.active_grant_count, 0)

    def test_foreign_epoch_and_noninteger_generation_cannot_grant(self):
        self.set_policy()
        observed = self.authorizer.get_policy()
        for epoch, generation in (
            (b"x" * 32, observed["policy_generation"]),
            (self.authorizer.epoch, True),
        ):
            with self.subTest(epoch_matches=epoch == self.authorizer.epoch):
                result = self.authorizer.issue_grant(
                    self.session, expected_epoch=epoch,
                    expected_policy_generation=generation,
                )
                self.assertFalse(result.ok)
                self.assertIsNone(result.capability)

    def test_unknown_policy_read_revokes_and_does_not_return_false_off(self):
        self.set_policy()
        grant = self.grant()
        self.assertTrue(grant.ok)
        raw = self.store.settings_path.read_bytes()
        self.store.settings_path.unlink()
        status = self.authorizer.get_policy()
        self.assertFalse(status["ok"])
        self.assertEqual(status["reason"], "plaintext_policy_unavailable")
        self.assertIsNone(status["encryption_enabled"])
        self.assertFalse(status["allowed_without_grant"])
        self.assertEqual(self.authorizer.active_grant_count, 0)
        self.store.settings_path.write_bytes(raw)
        denied = self.authorizer.validate(
            self.session, grant.epoch, grant.capability,
            grant.policy_generation, 1, "history-md",
        )
        self.assertFalse(denied.ok)

    def test_privacy_policy_read_denies_even_when_encryption_off(self):
        self.set_policy(encryption=False, privacy=True)
        status = self.authorizer.get_policy()
        self.assertFalse(status["ok"])
        self.assertEqual(status["reason"], "privacy_mode_active")
        self.assertFalse(status["allowed_without_grant"])

    def test_completed_validation_survives_revoke_but_next_write_does_not(self):
        self.set_policy()
        grant = self.grant()
        result = self.authorizer.validate_for_write(
            self.session, grant.epoch, grant.capability,
            grant.policy_generation, 1, "history-md",
        )
        self.authorizer.revoke(self.session, grant.epoch, grant.capability)
        # Ответ уже закончен сервером, даже если доставлен клиенту после revoke.
        self.assertTrue(result.ok)
        self.assertTrue(result.receipt)
        self.assertEqual(self.authorizer.active_receipt_count, 0)
        self.assertFalse(self.authorizer.consume_receipt(result.receipt))
        next_write = self.authorizer.validate_for_write(
            self.session, grant.epoch, grant.capability,
            grant.policy_generation, 2, "history-md",
        )
        self.assertFalse(next_write.ok)

    def test_completed_validation_rejects_repeated_sequence_on_off_path(self):
        status = self.authorizer.get_policy()
        args = (
            self.session, self.authorizer.epoch, None,
            status["policy_generation"], 1, "history-md",
        )
        self.assertTrue(self.authorizer.validate_for_write(*args).ok)
        self.assertFalse(self.authorizer.validate_for_write(*args).ok)

    def test_shutdown_clears_ram_and_prevents_regrant(self):
        self.set_policy()
        grant = self.grant()
        self.assertTrue(grant.ok)
        self.authorizer.close()
        self.authorizer.close()
        self.assertEqual(self.authorizer.active_grant_count, 0)
        self.assertEqual(self.authorizer.active_receipt_count, 0)
        self.assertFalse(self.authorizer.get_policy()["ok"])
        self.assertFalse(self.authorizer.issue_grant(self.session).ok)
        self.assertNotIn(grant.capability, json.dumps(self.authorizer.get_policy()))

    def test_completed_local_receipts_are_not_retained_by_backend(self):
        class ObservableToken(str):
            pass

        status = self.authorizer.get_policy()
        references = []
        # Наблюдаем lifetime реальных receipt-объектов, без зависимости от
        # имени/типа внутренних контейнеров authorizer.
        with mock.patch(
            "backend.plaintext_export_authorization.secrets.token_urlsafe",
            side_effect=lambda n: ObservableToken(uuid.uuid4().hex),
        ):
            for sequence in range(1, 33):
                result = self.authorizer.validate_for_write(
                    self.session, self.authorizer.epoch, None,
                    status["policy_generation"], sequence, "history-md",
                )
                self.assertTrue(result.ok)
                references.append(weakref.ref(result.receipt))
                del result
        gc.collect()
        self.assertTrue(all(reference() is None for reference in references))

    def test_malformed_provider_revokes_grants_and_fails_closed(self):
        self.set_policy()
        for mutation in ({"privacy_mode_enabled": None}, {"privacy_mode_enabled": 0},
                         {"fingerprint": "invalid"}):
            with self.subTest(mutation=mutation):
                grant = self.grant()
                snapshot = self.store.read_plaintext_policy_snapshot()
                with mock.patch.object(self.authorizer, "_read_snapshot",
                                       return_value=replace(snapshot, **mutation)):
                    denied = self.authorizer.validate_for_write(
                        self.session, grant.epoch, grant.capability,
                        grant.policy_generation, 1, "history-md",
                    )
                    self.assertFalse(denied.ok)
                    self.assertEqual(denied.reason, "plaintext_policy_unavailable")
                    self.assertFalse(self.authorizer.issue_grant(self.session).ok)
                self.assertEqual(self.authorizer.active_grant_count, 0)
                self.assertFalse(self.authorizer.validate_for_write(
                    self.session, grant.epoch, grant.capability,
                    grant.policy_generation, 2, "history-md",
                ).ok)


class BuildServiceProfileOwnershipTest(unittest.TestCase):
    def test_custom_socket_claim_allows_only_its_exact_startup_lock(self):
        from backend.service import build_service
        from backend.socket_ownership import SocketOwnershipClaim
        from backend.plaintext_export_authorization import PolicyState

        for socket_relative, extra_lock in (
            ("ear.ipc", False), ("ear.ipc", True),
            ("ipc/backend.ipc", False), ("ipc/backend.ipc", True),
        ):
            with self.subTest(socket=socket_relative, extra_lock=extra_lock), tempfile.TemporaryDirectory(prefix="ear-claim-") as root:
                profile = Path(root) / "profile"
                profile.mkdir()
                claim = SocketOwnershipClaim(profile / socket_relative)
                claim.acquire()
                try:
                    claim.prepare_for_bind()
                    if extra_lock:
                        (claim.socket_path.parent / "restored.sock.lock").write_text("inherited state")
                    with mock.patch("backend.service.BackendService", side_effect=lambda **kw: kw["store"]):
                        store = build_service(
                            profile, socket_path=claim.socket_path,
                            socket_ownership_snapshot_getter=claim.snapshot,
                            profile_created_by_this_startup=True,
                        )
                    expected = PolicyState.UNKNOWN if extra_lock else PolicyState.KNOWN_OFF
                    self.assertIs(store.read_plaintext_policy_snapshot().state, expected)
                finally:
                    claim.release()

    def test_competing_mkdir_does_not_turn_existing_missing_profile_into_off(self):
        from backend.service import build_service
        from backend.plaintext_export_authorization import PolicyState

        with tempfile.TemporaryDirectory(prefix="ear-startup-race-") as root:
            data_dir = Path(root) / "profile"
            real_mkdir = Path.mkdir
            competed = False

            def competing_mkdir(path, *args, **kwargs):
                nonlocal competed
                if path == data_dir and not competed:
                    competed = True
                    real_mkdir(path, parents=True, exist_ok=False)
                return real_mkdir(path, *args, **kwargs)

            with (
                mock.patch.object(Path, "mkdir", competing_mkdir),
                mock.patch("backend.service.BackendService", side_effect=lambda **kw: kw["store"]),
            ):
                store = build_service(data_dir)
            self.assertTrue(competed)
            self.assertFalse(store.settings_path.exists())
            self.assertIs(store.read_plaintext_policy_snapshot().state, PolicyState.UNKNOWN)
