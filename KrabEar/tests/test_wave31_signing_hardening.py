"""Wave-31 security tests: HMAC signing hardening.

FIX A1 (HIGH) — request_signing.py: empty-secret HMAC bypass
    sign_request() and verify_request() with an empty or whitespace-only
    secret must be rejected — raising ValueError from sign_request and
    returning False from verify_request.

FIX A2 (MED) — settings_service.py: env-pinned secret overwrite via set_settings
    When KRAB_EAR_IPC_SIGNING_SECRET or KRAB_EAR_IPC_SIGNING_ENABLED is set
    in the process environment, set_settings must reject updates for those keys
    with a clear error describing which env var to unset.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.request_signing import RequestSigner, SignedRequest  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers for SettingsService tests
# ---------------------------------------------------------------------------

def _make_settings_service():
    """Return a SettingsService backed by a real temp settings.json."""
    import json

    from backend.settings_service import SettingsService
    from backend.settings_backup import SettingsBackup

    tmp = tempfile.mkdtemp()
    settings_path = Path(tmp) / "settings.json"
    settings_path.write_text(json.dumps({}), encoding="utf-8")

    # Minimal StateStore stub
    store = MagicMock()
    store.load_settings.return_value = {}
    store.save_settings.return_value = {"ok": True}

    backup = MagicMock(spec=SettingsBackup)
    backup.create_backup.return_value = "backup_001"

    svc = SettingsService(store=store, backup=backup)
    return svc


# ===========================================================================
# FIX A1: empty-secret guard in RequestSigner
# ===========================================================================

class TestSignRequestEmptySecretRaisesValueError(unittest.TestCase):
    """sign_request() must raise ValueError when secret is empty/whitespace."""

    def setUp(self) -> None:
        self.signer = RequestSigner()

    def test_empty_string_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.signer.sign_request("ping", {}, "")
        self.assertIn("non-empty", str(ctx.exception).lower())

    def test_whitespace_only_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.signer.sign_request("ping", {}, "   ")

    def test_tab_only_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.signer.sign_request("ping", {}, "\t")

    def test_valid_secret_does_not_raise(self) -> None:
        secret = RequestSigner.generate_secret()
        result = self.signer.sign_request("ping", {}, secret)
        self.assertIsInstance(result, SignedRequest)


class TestVerifyRequestEmptySecretReturnsFalse(unittest.TestCase):
    """verify_request() must return False (not raise) when secret is empty."""

    def setUp(self) -> None:
        self.signer = RequestSigner()
        self.secret = RequestSigner.generate_secret()

    def _valid_signed(self):
        return self.signer.sign_request("ping", {}, self.secret)

    def test_empty_secret_returns_false(self) -> None:
        signed = self._valid_signed()
        result = self.signer.verify_request(
            signed.method, signed.params, signed.signature, "",
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(result)

    def test_whitespace_secret_returns_false(self) -> None:
        signed = self._valid_signed()
        result = self.signer.verify_request(
            signed.method, signed.params, signed.signature, "   ",
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(result)

    def test_empty_secret_does_not_raise(self) -> None:
        """verify_request must not propagate any exception for bad-secret input."""
        signed = self._valid_signed()
        try:
            self.signer.verify_request(
                signed.method, signed.params, signed.signature, "",
                timestamp=signed.timestamp, nonce=signed.nonce,
            )
        except Exception as exc:  # noqa: BLE001
            self.fail(f"verify_request raised unexpectedly: {exc!r}")

    def test_empty_secret_does_not_consume_nonce(self) -> None:
        """A rejected empty-secret verify must not spend a nonce slot."""
        ts = time.time()
        nonce = "wave31_test_nonce_00"
        signed = self.signer.sign_request("ping", {}, self.secret,
                                          timestamp=ts, nonce=nonce)
        # Attempt with empty secret — should fail and NOT consume the nonce
        self.signer.verify_request(
            "ping", {}, signed.signature, "",
            timestamp=ts, nonce=nonce,
        )
        self.assertEqual(self.signer.nonce_count(), 0,
                         "Empty-secret rejection must not consume a nonce slot")

        # Now the real call with the correct secret must succeed
        ok = self.signer.verify_request(
            "ping", {}, signed.signature, self.secret,
            timestamp=ts, nonce=nonce,
        )
        self.assertTrue(ok)

    def test_valid_secret_round_trip_still_works(self) -> None:
        """After the guard is in place, normal sign+verify still succeeds."""
        signed = self._valid_signed()
        ok = self.signer.verify_request(
            signed.method, signed.params, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertTrue(ok)

    def test_empty_secret_cannot_forge_valid_request(self) -> None:
        """An attacker who knows the empty-secret trivially-computable HMAC
        cannot get verify_request to return True."""
        # Compute the HMAC that would be produced by the empty-key
        import hashlib
        import hmac as _hmac
        import json as _json

        ts = time.time()
        nonce = "attacker_nonce"
        message = _json.dumps(
            {"method": "ping", "nonce": nonce, "params": {}, "timestamp": int(ts)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        forged_sig = _hmac.new(b"", message, hashlib.sha256).hexdigest()

        result = self.signer.verify_request(
            "ping", {}, forged_sig, "",
            timestamp=ts, nonce=nonce,
        )
        self.assertFalse(result, "Forged empty-key HMAC must not be accepted")


# ===========================================================================
# FIX A2: env-pinned secret guard in SettingsService
# ===========================================================================

class TestSetSettingsEnvPinnedSecretRejected(unittest.TestCase):
    """set_settings must reject updates for env-pinned signing keys."""

    def setUp(self) -> None:
        self.svc = _make_settings_service()
        # Ensure env vars are clean before each test
        os.environ.pop("KRAB_EAR_IPC_SIGNING_SECRET", None)
        os.environ.pop("KRAB_EAR_IPC_SIGNING_ENABLED", None)

    def tearDown(self) -> None:
        os.environ.pop("KRAB_EAR_IPC_SIGNING_SECRET", None)
        os.environ.pop("KRAB_EAR_IPC_SIGNING_ENABLED", None)

    def test_pinned_secret_update_raises(self) -> None:
        """When KRAB_EAR_IPC_SIGNING_SECRET is set, set_settings rejects it."""
        os.environ["KRAB_EAR_IPC_SIGNING_SECRET"] = "operator_secret_key"
        with self.assertRaises(ValueError) as ctx:
            self.svc.handle_set_settings({"ipc_signing_secret": "attacker_key"})
        msg = str(ctx.exception)
        self.assertIn("ipc_signing_secret", msg)
        self.assertIn("KRAB_EAR_IPC_SIGNING_SECRET", msg)

    def test_pinned_enabled_flag_update_raises(self) -> None:
        """When KRAB_EAR_IPC_SIGNING_ENABLED is set, set_settings rejects it."""
        os.environ["KRAB_EAR_IPC_SIGNING_ENABLED"] = "true"
        with self.assertRaises(ValueError) as ctx:
            self.svc.handle_set_settings({"ipc_signing_enabled": False})
        msg = str(ctx.exception)
        self.assertIn("ipc_signing_enabled", msg)
        self.assertIn("KRAB_EAR_IPC_SIGNING_ENABLED", msg)

    def test_unpinned_secret_update_allowed(self) -> None:
        """Without env var, updating ipc_signing_secret succeeds (no exception)."""
        # env var is not set (cleared in setUp)
        try:
            self.svc.handle_set_settings({"ipc_signing_secret": "new_secret"})
        except ValueError as exc:
            # only env-pin errors are a test failure; validation errors for format are OK
            if "pinned by env" in str(exc):
                self.fail(f"Unexpected env-pin rejection without env var: {exc}")

    def test_other_settings_not_blocked_when_signing_env_set(self) -> None:
        """Unrelated settings can still be updated even when the signing secret is pinned."""
        os.environ["KRAB_EAR_IPC_SIGNING_SECRET"] = "pinned"
        # Should not raise for unrelated key
        try:
            self.svc.handle_set_settings({"auto_paste": True})
        except ValueError as exc:
            if "pinned by env" in str(exc):
                self.fail(f"Env-pin guard incorrectly blocked unrelated setting: {exc}")

    def test_both_pinned_keys_rejected_together(self) -> None:
        """If both env vars are set, a request that tries to update both is rejected."""
        os.environ["KRAB_EAR_IPC_SIGNING_SECRET"] = "op_secret"
        os.environ["KRAB_EAR_IPC_SIGNING_ENABLED"] = "true"
        with self.assertRaises(ValueError):
            self.svc.handle_set_settings({
                "ipc_signing_secret": "attacker_key",
                "ipc_signing_enabled": False,
            })

    def test_error_message_mentions_env_var_to_unset(self) -> None:
        """The ValueError message tells the operator which env var to remove."""
        os.environ["KRAB_EAR_IPC_SIGNING_SECRET"] = "pinned_value"
        with self.assertRaises(ValueError) as ctx:
            self.svc.handle_set_settings({"ipc_signing_secret": "new_value"})
        self.assertIn("remove", str(ctx.exception).lower())

    def test_no_env_var_no_block(self) -> None:
        """When no env var is set, neither signing setting is blocked."""
        assert "KRAB_EAR_IPC_SIGNING_SECRET" not in os.environ
        assert "KRAB_EAR_IPC_SIGNING_ENABLED" not in os.environ
        # Should succeed (or fail for unrelated validation reasons, not env-pin)
        try:
            self.svc.handle_set_settings({"ipc_signing_enabled": True})
        except ValueError as exc:
            if "pinned by env" in str(exc):
                self.fail(f"Env-pin guard fired without env var: {exc}")


class TestCheckEnvPinnedHelper(unittest.TestCase):
    """Direct unit tests for SettingsService._check_env_pinned."""

    def setUp(self) -> None:
        self.svc = _make_settings_service()
        os.environ.pop("KRAB_EAR_IPC_SIGNING_SECRET", None)
        os.environ.pop("KRAB_EAR_IPC_SIGNING_ENABLED", None)

    def tearDown(self) -> None:
        os.environ.pop("KRAB_EAR_IPC_SIGNING_SECRET", None)
        os.environ.pop("KRAB_EAR_IPC_SIGNING_ENABLED", None)

    def test_no_env_no_raise(self) -> None:
        self.svc._check_env_pinned({"ipc_signing_secret": "x"})  # no exception

    def test_env_set_raises(self) -> None:
        os.environ["KRAB_EAR_IPC_SIGNING_SECRET"] = "pinned"
        with self.assertRaises(ValueError):
            self.svc._check_env_pinned({"ipc_signing_secret": "x"})

    def test_env_set_unrelated_key_no_raise(self) -> None:
        os.environ["KRAB_EAR_IPC_SIGNING_SECRET"] = "pinned"
        self.svc._check_env_pinned({"quality_profile": "max"})  # no exception

    def test_empty_params_no_raise(self) -> None:
        os.environ["KRAB_EAR_IPC_SIGNING_SECRET"] = "pinned"
        self.svc._check_env_pinned({})  # no exception


if __name__ == "__main__":
    unittest.main()
