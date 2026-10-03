"""A5.3: настоящий BackendService dispatcher с временным профилем."""

from pathlib import Path
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock


class PlaintextExportIPCTest(unittest.TestCase):
    def setUp(self):
        from backend.service import BackendService
        from backend.state_store import StateStore

        self.tmp = tempfile.TemporaryDirectory(prefix="ear-export-ipc-")
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "profile")
        store.initialize_startup_plaintext_policy(new_profile=True)
        llm_off = mock.patch("backend.service.settings.LLM_ENABLED", False)
        llm_off.start()
        self.addCleanup(llm_off.stop)
        # Реальный dispatcher/store/authorizer; аудио и сеть не относятся к IPC.
        engine = SimpleNamespace(
            quality_profile="balanced", current_model="fixture-model",
            _llm_rewriter=None, _settings_get=None,
            _resolve_diarization_device=lambda: "cpu", warmup=lambda: None,
        )
        self.service = BackendService(
            store=store,
            recorder=SimpleNamespace(is_recording=False, start=lambda: None, stop=lambda: b""),
            transcriber=SimpleNamespace(engine=engine, _error_bus=None),
            translator=mock.Mock(),
        )
        self.addCleanup(self.service.close)
        self.session = str(uuid.uuid4())

    def call(self, method, **params):
        response = self.service.handle_request({"id": 1, "method": method, "params": params})
        self.assertTrue(response["ok"], response)
        return response["result"]

    def enable(self):
        self.service.store.save_settings({
            "history_encryption_enabled": True, "privacy_mode_enabled": False,
        })

    def grant(self):
        policy = self.call("get_plaintext_export_policy")
        return self.call(
            "grant_plaintext_export_session", app_session_id=self.session,
            expected_epoch=policy["epoch"],
            expected_policy_generation=policy["policy_generation"],
        )

    def validate(self, context, seq=1, sink="history-md"):
        return self.call(
            "validate_plaintext_export", app_session_id=self.session,
            epoch=context["epoch"], capability=context.get("capability"),
            expected_policy_generation=context["policy_generation"],
            operation_seq=seq, sink_kind=sink,
        )

    def test_same_uid_direct_grant_is_honest_trust_boundary(self):
        self.enable()
        grant = self.grant()
        self.assertTrue(grant["ok"])
        first = self.validate(grant)
        self.assertTrue(first["ok"])
        self.assertTrue(first["receipt"])
        self.assertFalse(self.validate(grant)["ok"])
        self.call("revoke_plaintext_export_session", app_session_id=self.session,
                  epoch=grant["epoch"], capability=grant["capability"])
        self.assertFalse(self.validate(grant, seq=2)["ok"])

    def test_off_needs_fresh_validation_without_creating_grant(self):
        policy = self.call("get_plaintext_export_policy")
        self.assertTrue(policy["allowed_without_grant"])
        self.assertTrue(self.validate(policy)["ok"])
        self.assertEqual(self.service._plaintext_export_authorizer.active_grant_count, 0)
        self.service.store.save_settings({
            "history_encryption_enabled": False, "privacy_mode_enabled": True,
        })
        denied = self.validate(policy, seq=2)
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["reason"], "privacy_mode_active")
        self.assertNotIn("receipt", denied)

    def test_stale_sheet_and_malformed_session_cannot_grant(self):
        self.enable()
        observed = self.call("get_plaintext_export_policy")
        self.enable()
        denied = self.call("grant_plaintext_export_session", app_session_id=self.session,
                           expected_epoch=observed["epoch"],
                           expected_policy_generation=observed["policy_generation"])
        self.assertEqual(denied["reason"], "plaintext_session_expired")
        fresh = self.call("get_plaintext_export_policy")
        malformed = self.call("grant_plaintext_export_session", app_session_id="not-a-uuid",
                              expected_epoch=fresh["epoch"],
                              expected_policy_generation=fresh["policy_generation"])
        self.assertFalse(malformed["ok"])
        self.assertNotIn("capability", malformed)

    def test_unknown_sink_and_malformed_context_are_denied(self):
        self.enable()
        grant = self.grant()
        self.assertFalse(self.validate(grant, sink="unknown-writer")["ok"])
        self.assertFalse(self.validate({**grant, "epoch": "bad"})["ok"])
        self.assertFalse(self.validate(grant, seq=True)["ok"])
        self.assertTrue(self.validate(grant)["ok"])

    def test_close_revokes_authorizer_and_does_not_allow_regrant(self):
        self.enable()
        grant = self.grant()
        self.assertTrue(grant["ok"])
        self.service.close()
        self.assertEqual(self.service._plaintext_export_authorizer.active_grant_count, 0)
        self.assertFalse(self.service._plaintext_export_authorizer.get_policy()["ok"])
