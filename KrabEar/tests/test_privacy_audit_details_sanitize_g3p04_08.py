"""G3-privacy-04 P04-08: журнал приватности не хранит чувствительное содержимое details.

Раньше ``log_event`` сериализовал любые переданные ``details`` как есть: секрет,
текст транскрипта или длинный токен, случайно попавшие в details, ложились в
комплаенс-журнал открытым текстом. Теперь details проходят централизованную
санитизацию ДО расчёта HMAC (цепочка остаётся проверяемой).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

# Токеноподобная строка собирается из повторов, а не литералом: сканеры секретов
# (GitGuardian) принимают высокоэнтропийный литерал за настоящий ключ.
TOKEN_SHAPED = "Zq7k" * 9

from backend.privacy_audit import (  # noqa: E402
    REDACTED,
    PrivacyAuditLogger,
    sanitize_audit_details,
)


class SanitizeUnitTests(unittest.TestCase):
    def test_counters_flags_and_short_strings_pass_unchanged(self):
        details = {
            "history_deleted": 12,
            "encryption_key_shredded": True,
            "reason": "privacy_mode_enabled",
            "provider": "groq",
            "ratio": 0.5,
            "none_value": None,
            "secondary_errors": ["a", "b"],
        }
        self.assertEqual(sanitize_audit_details(details), details)

    def test_sensitive_key_with_string_value_is_redacted(self):
        out = sanitize_audit_details(
            {"api_key": "sk-TESTSECRET", "transcript": "привет мир", "password": "p"}
        )
        self.assertEqual(out, {"api_key": REDACTED, "transcript": REDACTED, "password": REDACTED})

    def test_sensitive_key_with_numeric_value_is_kept(self):
        out = sanitize_audit_details({"text_length": 5, "token_count": 3, "transcripts_deleted": 2})
        self.assertEqual(out, {"text_length": 5, "token_count": 3, "transcripts_deleted": 2})

    def test_token_shaped_value_is_redacted_under_innocent_key(self):
        secret = TOKEN_SHAPED
        out = sanitize_audit_details({"note": secret, "hdr": "Bearer abc.def"})
        self.assertEqual(out, {"note": REDACTED, "hdr": REDACTED})

    def test_long_string_is_truncated(self):
        out = sanitize_audit_details({"reason": "x " * 400})
        self.assertLessEqual(len(out["reason"]), 200)

    def test_nested_values_are_sanitized_and_depth_capped(self):
        out = sanitize_audit_details(
            {"ctx": {"secret": "s", "n": 1, "deep": {"a": {"b": {"c": "z"}}}}, "items": list(range(100))}
        )
        self.assertEqual(out["ctx"]["secret"], REDACTED)
        self.assertEqual(out["ctx"]["n"], 1)
        self.assertLessEqual(len(out["items"]), 20)
        self.assertNotIn("z", json.dumps(out))

    def test_non_json_values_degrade_to_type_name_and_nan_to_none(self):
        out = sanitize_audit_details({"obj": object(), "nan": float("nan"), "raw": b"\x00secret"})
        self.assertEqual(out["obj"], "object")
        self.assertIsNone(out["nan"])
        self.assertEqual(out["raw"], "bytes")

    def test_bad_keys_are_normalized_not_dropped_silently(self):
        out = sanitize_audit_details({1: "a", "k" * 300: "b"})
        self.assertEqual(len(out), 2)
        self.assertTrue(all(isinstance(k, str) and len(k) <= 64 for k in out))

    def test_non_dict_details_become_empty(self):
        self.assertEqual(sanitize_audit_details(None), {})
        self.assertEqual(sanitize_audit_details("oops"), {})  # type: ignore[arg-type]


class PhysicalLogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.log_path = Path(tmp.name) / "audit.log"
        PrivacyAuditLogger.reset_instance()
        self.addCleanup(PrivacyAuditLogger.reset_instance)

    def test_secret_never_reaches_physical_log_and_chain_stays_valid(self):
        audit = PrivacyAuditLogger(log_path=self.log_path)
        audit.log_event("sentry", "blocked", {"api_key": "sk-TESTSECRET-9", "reason": "r"})
        outcome = audit.log_event_strict("privacy", "x", {"note": TOKEN_SHAPED})
        self.assertEqual(outcome, "confirmed")
        raw = self.log_path.read_text("utf-8")
        self.assertNotIn("sk-TESTSECRET-9", raw)
        self.assertNotIn(TOKEN_SHAPED, raw)
        self.assertIn("reason", raw)
        self.assertTrue(audit.verify_chain()["valid"])


if __name__ == "__main__":
    unittest.main()
