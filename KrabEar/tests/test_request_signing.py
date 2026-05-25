"""Тесты для RequestSigner — HMAC-SHA256 подпись/верификация IPC-запросов.

Покрывает:
1.  generate_secret — длина и уникальность
2.  sign_request — возвращает SignedRequest с корректными полями
3.  verify_request — валидная подпись принимается
4.  verify_request — неверный секрет отклоняется
5.  verify_request — изменённые params отклоняются (tampered payload)
6.  verify_request — изменённый метод отклоняется
7.  verify_request — повторный nonce отклоняется (replay attack)
8.  verify_request — устаревший timestamp отклоняется (expired window)
9.  verify_request — будущий timestamp за пределами окна отклоняется
10. nonce eviction — при 1001 nonce'е старейший вытесняется (max 1000)
11. clear_nonces — сброс хранилища
12. nonce_count — счётчик отражает реальное количество
13. sign + verify round-trip: метод без params
14. constant-time compare: незначительно изменённая подпись отклоняется
15. verify без timestamp/nonce (упрощённый режим)
"""

from __future__ import annotations
from backend.request_signing import (
    MAX_NONCES,
    TIMESTAMP_WINDOW_SEC,
    RequestSigner,
    SignedRequest,
)

import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestGenerateSecret(unittest.TestCase):
    """1. generate_secret."""

    def test_length_is_64_hex_chars(self) -> None:
        """32 байта → 64 hex-символа."""
        secret = RequestSigner.generate_secret()
        self.assertEqual(len(secret), 64)
        # Должна быть корректной hex-строкой
        int(secret, 16)

    def test_secrets_are_unique(self) -> None:
        """Два последовательных вызова возвращают разные значения."""
        s1 = RequestSigner.generate_secret()
        s2 = RequestSigner.generate_secret()
        self.assertNotEqual(s1, s2)


class TestSignRequest(unittest.TestCase):
    """2. sign_request."""

    def setUp(self) -> None:
        self.signer = RequestSigner()
        self.secret = RequestSigner.generate_secret()

    def test_returns_signed_request_dataclass(self) -> None:
        result = self.signer.sign_request("ping", {}, self.secret)
        self.assertIsInstance(result, SignedRequest)

    def test_method_and_params_preserved(self) -> None:
        params = {"key": "value", "num": 42}
        result = self.signer.sign_request("transcribe_paths", params, self.secret)
        self.assertEqual(result.method, "transcribe_paths")
        self.assertEqual(result.params, params)

    def test_signature_is_hex_string(self) -> None:
        result = self.signer.sign_request("ping", {}, self.secret)
        self.assertIsInstance(result.signature, str)
        # HMAC-SHA256 → 64 hex-символа
        self.assertEqual(len(result.signature), 64)
        int(result.signature, 16)

    def test_nonce_generated_automatically(self) -> None:
        result = self.signer.sign_request("ping", {}, self.secret)
        self.assertIsInstance(result.nonce, str)
        self.assertTrue(len(result.nonce) > 0)

    def test_custom_timestamp_used(self) -> None:
        ts = 1_700_000_000.0
        result = self.signer.sign_request("ping", {}, self.secret, timestamp=ts)
        self.assertEqual(result.timestamp, ts)

    def test_custom_nonce_used(self) -> None:
        nonce = "deadbeef" * 4
        result = self.signer.sign_request("ping", {}, self.secret, nonce=nonce)
        self.assertEqual(result.nonce, nonce)


class TestVerifyRequest(unittest.TestCase):
    """3–9. verify_request."""

    def setUp(self) -> None:
        self.signer = RequestSigner()
        self.secret = RequestSigner.generate_secret()

    # ------------------------------------------------------------------
    # 3. Валидная подпись принимается
    # ------------------------------------------------------------------
    def test_valid_signature_accepted(self) -> None:
        signed = self.signer.sign_request("ping", {}, self.secret)
        ok = self.signer.verify_request(
            signed.method, signed.params, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertTrue(ok)

    # ------------------------------------------------------------------
    # 4. Неверный секрет отклоняется
    # ------------------------------------------------------------------
    def test_wrong_secret_rejected(self) -> None:
        signed = self.signer.sign_request("ping", {}, self.secret)
        wrong_secret = RequestSigner.generate_secret()
        ok = self.signer.verify_request(
            signed.method, signed.params, signed.signature, wrong_secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(ok)

    # ------------------------------------------------------------------
    # 5. Изменённые params отклоняются (tampered payload)
    # ------------------------------------------------------------------
    def test_tampered_params_rejected(self) -> None:
        signed = self.signer.sign_request("get_settings", {"user": "alice"}, self.secret)
        tampered_params = {"user": "eve"}
        ok = self.signer.verify_request(
            signed.method, tampered_params, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(ok)

    # ------------------------------------------------------------------
    # 6. Изменённый метод отклоняется
    # ------------------------------------------------------------------
    def test_tampered_method_rejected(self) -> None:
        signed = self.signer.sign_request("ping", {}, self.secret)
        ok = self.signer.verify_request(
            "delete_history_item", signed.params, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(ok)

    # ------------------------------------------------------------------
    # 7. Повторный nonce отклоняется (replay attack)
    # ------------------------------------------------------------------
    def test_replay_attack_rejected(self) -> None:
        signed = self.signer.sign_request("ping", {}, self.secret)
        # Первый вызов должен пройти
        ok1 = self.signer.verify_request(
            signed.method, signed.params, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertTrue(ok1)
        # Тот же nonce — replay attack
        ok2 = self.signer.verify_request(
            signed.method, signed.params, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(ok2)

    # ------------------------------------------------------------------
    # 8. Устаревший timestamp отклоняется (expired window)
    # ------------------------------------------------------------------
    def test_expired_timestamp_rejected(self) -> None:
        old_ts = time.time() - TIMESTAMP_WINDOW_SEC - 1
        signed = self.signer.sign_request("ping", {}, self.secret, timestamp=old_ts)
        ok = self.signer.verify_request(
            signed.method, signed.params, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(ok)

    # ------------------------------------------------------------------
    # 9. Будущий timestamp за пределами окна отклоняется
    # ------------------------------------------------------------------
    def test_future_timestamp_rejected(self) -> None:
        future_ts = time.time() + TIMESTAMP_WINDOW_SEC + 1
        signed = self.signer.sign_request("ping", {}, self.secret, timestamp=future_ts)
        ok = self.signer.verify_request(
            signed.method, signed.params, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(ok)


class TestNonceEviction(unittest.TestCase):
    """10–12. Нonce management."""

    def setUp(self) -> None:
        self.signer = RequestSigner()
        self.secret = RequestSigner.generate_secret()

    # ------------------------------------------------------------------
    # 10. При MAX_NONCES+1 nonce'ах старейший вытесняется
    # ------------------------------------------------------------------
    def test_nonce_eviction_at_max_capacity(self) -> None:
        # Регистрируем MAX_NONCES nonce'ов через verify (использует _register_nonce внутри)
        first_nonce = "first_nonce_000"
        self.signer._register_nonce_for_test(first_nonce)
        # Заполняем до MAX_NONCES
        for i in range(MAX_NONCES - 1):
            self.signer._register_nonce_for_test(f"nonce_{i:06d}")

        self.assertEqual(self.signer.nonce_count(), MAX_NONCES)

        # Добавляем ещё один — first_nonce должен быть вытеснен
        self.signer._register_nonce_for_test("overflow_nonce")
        self.assertEqual(self.signer.nonce_count(), MAX_NONCES)

        # first_nonce больше не в хранилище → повторная регистрация не упадёт
        # (проверяем через verify_request без timestamp — если nonce не в сете, проходит)
        self.assertNotIn(first_nonce, self.signer._nonce_set)

    # ------------------------------------------------------------------
    # 11. clear_nonces сбрасывает хранилище
    # ------------------------------------------------------------------
    def test_clear_nonces_empties_storage(self) -> None:
        for i in range(10):
            self.signer._register_nonce_for_test(f"nc_{i}")
        self.assertEqual(self.signer.nonce_count(), 10)
        self.signer.clear_nonces()
        self.assertEqual(self.signer.nonce_count(), 0)

    # ------------------------------------------------------------------
    # 12. nonce_count отражает реальное количество
    # ------------------------------------------------------------------
    def test_nonce_count_tracks_additions(self) -> None:
        self.assertEqual(self.signer.nonce_count(), 0)
        self.signer._register_nonce_for_test("a")
        self.assertEqual(self.signer.nonce_count(), 1)
        self.signer._register_nonce_for_test("b")
        self.assertEqual(self.signer.nonce_count(), 2)


class TestRoundTrip(unittest.TestCase):
    """13–15. Дополнительные сценарии."""

    def setUp(self) -> None:
        self.signer = RequestSigner()
        self.secret = RequestSigner.generate_secret()

    # ------------------------------------------------------------------
    # 13. Round-trip: метод без params
    # ------------------------------------------------------------------
    def test_round_trip_empty_params(self) -> None:
        signed = self.signer.sign_request("compact_history", {}, self.secret)
        ok = self.signer.verify_request(
            signed.method, signed.params, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertTrue(ok)

    # ------------------------------------------------------------------
    # 14. Незначительно изменённая подпись отклоняется
    # ------------------------------------------------------------------
    def test_one_char_signature_change_rejected(self) -> None:
        signed = self.signer.sign_request("ping", {}, self.secret)
        # Инвертируем последний символ hex-строки
        last = signed.signature[-1]
        bad_char = "0" if last != "0" else "1"
        bad_sig = signed.signature[:-1] + bad_char
        ok = self.signer.verify_request(
            signed.method, signed.params, bad_sig, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(ok)

    # ------------------------------------------------------------------
    # 15. verify без timestamp и nonce (упрощённый/legacy режим)
    # ------------------------------------------------------------------
    def test_verify_without_timestamp_and_nonce(self) -> None:
        """Без timestamp и nonce верифицируется только HMAC."""
        fixed_ts = time.time()
        fixed_nonce = "fixed_nonce_test"
        signed = self.signer.sign_request(
            "ping", {}, self.secret,
            timestamp=fixed_ts, nonce=fixed_nonce,
        )
        # Вычисляем подпись вручную — signature привязана к конкретным ts/nonce
        # Поэтому verify без ts/nonce (ts=0, nonce="") должен вернуть False
        ok_no_ts_nonce = self.signer.verify_request(
            signed.method, signed.params, signed.signature, self.secret,
        )
        self.assertFalse(ok_no_ts_nonce)

    def test_verify_without_timestamp_nonce_correct_sig(self) -> None:
        """Подпись, вычисленная с ts=0/nonce='', принимается без ts/nonce."""
        signed_zero = self.signer.sign_request(
            "ping", {}, self.secret,
            timestamp=0.0, nonce="",
        )
        ok = self.signer.verify_request(
            signed_zero.method, signed_zero.params,
            signed_zero.signature, self.secret,
            # timestamp=None и nonce=None → verify использует 0.0 и ""
        )
        self.assertTrue(ok)


class TestEmptyPayloadEdgeCases(unittest.TestCase):
    """Edge cases: пустые params, пустой метод, нестандартные входные данные."""

    def setUp(self) -> None:
        self.signer = RequestSigner()
        self.secret = RequestSigner.generate_secret()

    def test_empty_params_sign_and_verify(self) -> None:
        """Пустой dict params — базовый случай."""
        signed = self.signer.sign_request("ping", {}, self.secret)
        ok = self.signer.verify_request(
            signed.method, {}, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertTrue(ok)

    def test_nested_params_sign_and_verify(self) -> None:
        """Вложенные params корректно сериализуются."""
        params = {"nested": {"key": [1, 2, 3]}, "flag": True}
        signed = self.signer.sign_request("update_settings", params, self.secret)
        ok = self.signer.verify_request(
            signed.method, params, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertTrue(ok)

    def test_unicode_params_sign_and_verify(self) -> None:
        """Unicode-значения в params."""
        params = {"text": "Привет мир", "lang": "ru"}
        signed = self.signer.sign_request("transcribe", params, self.secret)
        ok = self.signer.verify_request(
            signed.method, params, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertTrue(ok)

    def test_empty_params_tamper_detected(self) -> None:
        """Добавление поля к изначально пустым params → подпись не проходит."""
        signed = self.signer.sign_request("ping", {}, self.secret)
        ok = self.signer.verify_request(
            signed.method, {"injected": True}, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(ok)

    def test_signature_deterministic_same_inputs(self) -> None:
        """Одни и те же входные данные → одна и та же подпись."""
        ts = 1_700_000_000.0
        nonce = "fixed_nonce_abc"
        sig1 = RequestSigner._compute_signature("ping", {}, self.secret, ts, nonce)
        sig2 = RequestSigner._compute_signature("ping", {}, self.secret, ts, nonce)
        self.assertEqual(sig1, sig2)

    def test_signature_differs_for_different_params(self) -> None:
        """Разные params → разные подписи."""
        ts = 1_700_000_000.0
        nonce = "fixed_nonce_abc"
        sig_empty = RequestSigner._compute_signature("ping", {}, self.secret, ts, nonce)
        sig_with_data = RequestSigner._compute_signature("ping", {"x": 1}, self.secret, ts, nonce)
        self.assertNotEqual(sig_empty, sig_with_data)


class TestWave136Required(unittest.TestCase):
    """Wave 136 required test names for RequestSigner."""

    def setUp(self) -> None:
        self.signer = RequestSigner()
        self.secret = RequestSigner.generate_secret()

    def test_sign_request_produces_hmac(self) -> None:
        """sign_request returns a 64-char hex HMAC-SHA256 signature."""
        signed = self.signer.sign_request("ping", {"key": "val"}, self.secret)
        self.assertIsInstance(signed.signature, str)
        self.assertEqual(len(signed.signature), 64)
        # Must be valid hex
        int(signed.signature, 16)

    def test_verify_valid_signature(self) -> None:
        """A freshly signed request verifies successfully."""
        signed = self.signer.sign_request("get_settings", {}, self.secret)
        ok = self.signer.verify_request(
            signed.method, signed.params, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertTrue(ok)

    def test_verify_tampered_request_fails(self) -> None:
        """Changing params after signing causes verification to fail."""
        signed = self.signer.sign_request("transcribe", {"lang": "ru"}, self.secret)
        ok = self.signer.verify_request(
            signed.method, {"lang": "en"}, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(ok)

    def test_verify_wrong_secret_fails(self) -> None:
        """Using a different secret key to verify returns False."""
        signed = self.signer.sign_request("ping", {}, self.secret)
        other_secret = RequestSigner.generate_secret()
        ok = self.signer.verify_request(
            signed.method, signed.params, signed.signature, other_secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(ok)

    def test_unicode_payload(self) -> None:
        """Unicode characters in method name and params sign + verify cleanly."""
        params = {"текст": "Привет мир 🎤", "язык": "ru"}
        signed = self.signer.sign_request("транскрибировать", params, self.secret)
        ok = self.signer.verify_request(
            signed.method, params, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertTrue(ok)

    def test_timestamp_skew_rejected(self) -> None:
        """A request with timestamp older than TIMESTAMP_WINDOW_SEC is rejected."""
        stale_ts = time.time() - TIMESTAMP_WINDOW_SEC - 10
        signed = self.signer.sign_request("ping", {}, self.secret, timestamp=stale_ts)
        ok = self.signer.verify_request(
            signed.method, signed.params, signed.signature, self.secret,
            timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(ok)

    def test_concurrent_sign(self) -> None:
        """Multiple threads signing simultaneously produce valid, distinct signatures."""
        import threading

        results = []
        errors = []

        def worker():
            try:
                local_signer = RequestSigner()
                local_secret = RequestSigner.generate_secret()
                signed = local_signer.sign_request(
                    "ping", {"worker": threading.get_ident()}, local_secret
                )
                ok = local_signer.verify_request(
                    signed.method, signed.params, signed.signature, local_secret,
                    timestamp=signed.timestamp, nonce=signed.nonce,
                )
                results.append((signed.signature, ok))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent sign errors: {errors}")
        self.assertEqual(len(results), 12)
        # All verifications pass
        for sig, ok in results:
            self.assertTrue(ok, f"Signature {sig!r} failed verification")
        # All signatures are distinct (different secrets + params)
        sigs = [r[0] for r in results]
        self.assertEqual(len(set(sigs)), 12)


# ---------------------------------------------------------------------------
# Вспомогательный метод для тестов — добавляем к RequestSigner через monkey-patch
# ---------------------------------------------------------------------------

def _register_nonce_for_test(self: RequestSigner, nonce: str) -> None:
    """Публичный хелпер для юнит-тестов: регистрирует nonce напрямую."""
    with self._lock:
        self._register_nonce(nonce)


RequestSigner._register_nonce_for_test = _register_nonce_for_test  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
