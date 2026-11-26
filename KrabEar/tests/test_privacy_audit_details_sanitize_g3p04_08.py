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
    def test_counters_flags_and_labels_pass_unchanged(self):
        details = {
            "history_deleted": 12,
            "encryption_key_shredded": True,
            "reason": "privacy_mode_enabled",
            "provider": "groq",
            "ratio": 0.5,
            "none_value": None,
            "secondary_errors": ["data_dir", "privacy_audit"],
            "ledger_entry": "каталог",
            "content_type": "audio/wav",
        }
        self.assertEqual(sanitize_audit_details(details), details)

    def test_every_real_producer_payload_survives_unchanged(self):
        producers = [
            {"result": "refused", "reason": "preflight_failed", "ledger_entry": "symlink",
             "secondary_errors": ["a_step"]},
            {"provider": "openai", "input_chars": 10, "output_chars": 9, "language": "ru"},
            {"provider": "openai", "language": "es"},
            {"original_mode": "online", "method": "handle_translate_text"},
            {"reason": "privacy_mode_enabled"},
            {"key_length": 32, "quarantined_as": "privacy_audit.key.quarantine-1"},
        ]
        for payload in producers:
            with self.subTest(payload=payload):
                self.assertEqual(sanitize_audit_details(payload), payload)

    def test_unknown_key_with_string_is_redacted(self):
        out = sanitize_audit_details({"note": "any text", "message": "hi", "x": "y"})
        self.assertEqual(out, {"note": REDACTED, "message": REDACTED, "x": REDACTED})

    def test_sensitive_names_with_string_value_are_redacted(self):
        out = sanitize_audit_details(
            {"api_key": "sk-TESTSECRET", "transcript": "привет мир", "password": "p"}
        )
        self.assertEqual(out, {"api_key": REDACTED, "transcript": REDACTED, "password": REDACTED})

    def test_numbers_under_sensitive_names_are_kept(self):
        out = sanitize_audit_details({"text_length": 5, "token_count": 3, "transcripts_deleted": 2})
        self.assertEqual(out, {"text_length": 5, "token_count": 3, "transcripts_deleted": 2})

    def test_short_secret_under_innocent_key_is_redacted(self):
        # Короткая строка не попадала под «токеноподобную» (>=32) эвристику denylist.
        out = sanitize_audit_details({"hdr": "Bearer abc.def", "pin": "sk-short-1", "tag": "hunter2"})
        self.assertEqual(out, {"hdr": REDACTED, "pin": REDACTED, "tag": REDACTED})

    def test_token_shaped_value_is_redacted_even_under_safe_key_with_spaces_or_length(self):
        out = sanitize_audit_details({"reason": TOKEN_SHAPED + " " + TOKEN_SHAPED, "provider": "x" * 80})
        self.assertEqual(out, {"reason": REDACTED, "provider": REDACTED})

    def test_sentence_under_safe_key_is_redacted(self):
        out = sanitize_audit_details({"reason": "пользователь сказал секретную фразу"})
        self.assertEqual(out, {"reason": REDACTED})

    def test_key_spelling_variants_do_not_bypass(self):
        out = sanitize_audit_details(
            {
                "apiKey": "v1",
                "api-key": "v2",
                "API_KEY": "v3",
                "api\u200bkey": "v4",
                "Reason\u200b": "has space here",
                "tränscript": "v5",
            }
        )
        for value in out.values():
            self.assertEqual(value, REDACTED)

    def test_zero_width_in_key_does_not_turn_unsafe_value_into_safe(self):
        # «re\u200bason» нормализуется в safe-ключ, но значение-предложение всё равно режется.
        out = sanitize_audit_details({"re\u200bason": "secret sentence"})
        self.assertEqual(list(out.values()), [REDACTED])

    def test_invisible_chars_and_camel_case_in_safe_keys_are_normalized(self):
        # Нормализация работает в обе стороны: легитимный ключ с мусором не теряется.
        out = sanitize_audit_details({"pro\u200bvider": "groq", "originalMode": "online"})
        self.assertEqual(out, {"provider": "groq", "original_mode": "online"})

    def test_secret_in_dict_key_is_not_written_even_after_normalization(self):
        # После casefold/замены дефисов такой ключ подходит под a-z0-9_, но он — секрет.
        secret_key = "sk-live-SECRETSECRETSECRETSECRET12"
        digits_key = "tok_34612345678"
        out = sanitize_audit_details({"ids": {secret_key: 1, digits_key: 2}})
        dumped = json.dumps(out).lower()
        self.assertNotIn("secretsecret", dumped)
        self.assertNotIn("34612345678", dumped)
        self.assertEqual(sorted(out["ids"].values()), [1, 2])

    def test_legitimate_long_counter_keys_stay_readable(self):
        details = {
            "deletion_ledger_ids_preserved": 3,
            "history_encryption_enabled_after": True,
            "call_sessions_deleted": 1,
            "sha256": "ab" * 32,
        }
        self.assertEqual(sanitize_audit_details(details), details)

    def test_text_in_dict_key_is_not_written_to_log(self):
        leak = "Привет, мой пароль 12345"
        out = sanitize_audit_details({leak: 7, "k" * 300: "b"})
        dumped = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("пароль", dumped)
        self.assertNotIn("kkkkkkkk", dumped)
        self.assertTrue(all(len(k) <= 64 for k in out))
        self.assertEqual(len(out), 2)
        self.assertIn(7, out.values())

    def test_nested_strings_in_lists_and_dicts_are_redacted(self):
        out = sanitize_audit_details(
            {
                "secondary_errors": ["ok_step", "free text with spaces", TOKEN_SHAPED[:10] + " x"],
                "ctx": {"secret": "s", "n": 1, "inner": {"note": "z"}},
                "items": ["abc", "def"],
            }
        )
        self.assertEqual(out["secondary_errors"], ["ok_step", REDACTED, REDACTED])
        self.assertEqual(out["ctx"]["secret"], REDACTED)
        self.assertEqual(out["ctx"]["n"], 1)
        self.assertEqual(out["items"], [REDACTED, REDACTED])

    def test_depth_and_list_length_are_capped(self):
        out = sanitize_audit_details(
            {"deep": {"a": {"b": {"c": "z"}}}, "items": list(range(100))}
        )
        self.assertLessEqual(len(out["items"]), 20)
        self.assertNotIn("\"z\"", json.dumps(out))
        # Число на третьем уровне вложенности не должно просачиваться как есть.
        numeric = sanitize_audit_details({"a": {"b": {"c": {"d": 5}}}})
        self.assertEqual(numeric["a"]["b"]["c"], REDACTED)

    def test_set_values_are_sanitized_not_dropped_to_type_name_with_content(self):
        out = sanitize_audit_details({"secondary_errors": {"step_a"}, "other": {"free text"}})
        self.assertEqual(out["secondary_errors"], ["step_a"])
        self.assertEqual(out["other"], [REDACTED])

    def test_id_hash_keys_accept_uuid_or_hex_only(self):
        uid = "123e4567-e89b-12d3-a456-426614174000"
        sha = "deadbeef" * 8
        out = sanitize_audit_details(
            {
                "request_id": uid,
                "file_sha": sha,
                "chain_hash": "ABCDEF0123456789",
                "item_ids": [uid],
                "session_id": "my secret session text",
                "doc_id": "not-hex-zzzz",
            }
        )
        self.assertEqual(out["request_id"], uid)
        self.assertEqual(out["file_sha"], sha)
        self.assertEqual(out["chain_hash"], "ABCDEF0123456789")
        self.assertEqual(out["item_ids"], [uid])
        self.assertEqual(out["session_id"], REDACTED)
        self.assertEqual(out["doc_id"], REDACTED)

    def test_non_json_values_degrade_to_type_name_and_nan_to_none(self):
        out = sanitize_audit_details({"obj": object(), "nan": float("nan"), "raw": b"\x00secret"})
        self.assertEqual(out["obj"], "object")
        self.assertIsNone(out["nan"])
        self.assertEqual(out["raw"], "bytes")

    def test_non_string_keys_are_normalized_not_dropped_silently(self):
        out = sanitize_audit_details({1: "a", "k" * 300: "b"})
        self.assertEqual(len(out), 2)
        self.assertTrue(all(isinstance(k, str) and len(k) <= 64 for k in out))
        self.assertNotIn("a", out.values())

    def test_colliding_normalized_keys_keep_both_values(self):
        out = sanitize_audit_details({"apiKey": 1, "api_key": 2})
        self.assertEqual(sorted(out.values()), [1, 2])

    def test_path_under_safe_key_is_redacted(self):
        # ChatGPT-ревью: «/» в метке пропускал абсолютный путь под reason.
        out = sanitize_audit_details(
            {"reason": "/Users/alice/Documents/PrivateNotes.txt", "method": "C:\\Users\\a\\x"}
        )
        self.assertEqual(out, {"reason": REDACTED, "method": REDACTED})

    def test_secret_word_label_under_safe_key_is_redacted(self):
        out = sanitize_audit_details({"provider": "abc_secret_token", "reason": "bearer-xyz"})
        self.assertEqual(out, {"provider": REDACTED, "reason": REDACTED})

    def test_content_type_keeps_mime_but_not_a_path(self):
        out = sanitize_audit_details({"content_type": "audio/wav"})
        self.assertEqual(out, {"content_type": "audio/wav"})
        self.assertEqual(
            sanitize_audit_details({"content_type": "/etc/passwd"}), {"content_type": REDACTED}
        )

    def test_trailing_newline_does_not_slip_through(self):
        # re.match с «$» принимал хвостовой перевод строки.
        uid = "123e4567-e89b-12d3-a456-426614174000"
        out = sanitize_audit_details({"reason": "ok\n", "request_id": uid + "\n"})
        self.assertEqual(out, {"reason": REDACTED, "request_id": REDACTED})

    def test_huge_int_is_clamped_and_log_serializable(self):
        out = sanitize_audit_details({"count": 2**20000, "neg": -(2**20000)})
        json.dumps(out)  # раньше ValueError: лимит int -> str
        self.assertEqual(out["count"], 2**63)
        self.assertEqual(out["neg"], -(2**63))

    def test_key_count_is_capped(self):
        out = sanitize_audit_details({f"k{i}": i for i in range(500)})
        self.assertLessEqual(len(out), 51)
        self.assertEqual(out["_dropped_keys"], 450)

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
