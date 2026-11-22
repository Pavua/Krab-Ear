"""Wave-21 multi-worker coherence tests for backend/rest_auth.py.

Simulates two gunicorn worker processes by creating two separate RestAuth
instances that share the same api_tokens.json file on disk.

Test matrix:
  1. Cross-worker revocation: token created on worker-A, revoked on worker-A,
     worker-B must reject it after the mtime-reload path is triggered.
  2. Cross-worker creation: token created on worker-A, worker-B (whose mtime
     cache is stale) must accept it after reload.
  3. Unchanged-mtime cache: when the file has NOT changed, verify_token does
     not reload (fast path — mtime stays the same).
  4. flock serialisation: two workers calling _save concurrently must not
     corrupt the file (both writes succeed, final file is valid JSON).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.rest_auth import RestAuth  # noqa: E402


def _make_auth(tmp_dir: str) -> RestAuth:
    return RestAuth(data_dir=tmp_dir)


class TestCrossWorkerRevocation(unittest.TestCase):
    """Revocation on worker-A propagates to worker-B via mtime-reload."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        # Worker-A: creates the token.
        self.worker_a = _make_auth(self._tmp)
        # Worker-B: constructed AFTER the file exists (mtime captured).
        self.worker_b = _make_auth(self._tmp)

    def test_revoked_token_rejected_by_other_worker(self):
        """Worker-B must reject a token that worker-A revoked."""
        raw, meta = self.worker_a.create_token("shared-client")

        # Sanity: worker-B can verify it before revocation.
        # (Also triggers mtime capture on worker-B after the create.)
        result_before = self.worker_b.verify_token(raw)
        self.assertIsNotNone(result_before, "Token should be valid before revocation")

        # Worker-A revokes — rewrites api_tokens.json, mtime advances.
        revoked = self.worker_a.revoke_token(meta["id"])
        self.assertTrue(revoked)

        # Worker-B's cached mtime is now stale.  verify_token must reload.
        result_after = self.worker_b.verify_token(raw)
        self.assertIsNone(result_after, "Worker-B must reject revoked token after mtime-reload")

    def test_revoke_then_new_token_still_accepted_by_other_worker(self):
        """After revocation reload, a second new token works on worker-B."""
        raw_old, meta_old = self.worker_a.create_token("old")
        # Force worker-B to see the first token.
        self.worker_b.verify_token(raw_old)

        # Worker-A revokes the first token and creates a second one.
        self.worker_a.revoke_token(meta_old["id"])
        raw_new, _ = self.worker_a.create_token("new")

        # Worker-B mtime is stale now.  verify_token must reload and accept new token.
        result = self.worker_b.verify_token(raw_new)
        self.assertIsNotNone(result, "Worker-B must accept new token after reload")


class TestCrossWorkerCreation(unittest.TestCase):
    """Token created on worker-A becomes visible on worker-B after stale mtime."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.worker_a = _make_auth(self._tmp)
        # Worker-B starts with empty file / no tokens.
        self.worker_b = _make_auth(self._tmp)

    def test_token_created_on_a_visible_to_b(self):
        raw, _ = self.worker_a.create_token("from-a")
        # Worker-B's mtime is now stale (or file didn't exist when B started).
        result = self.worker_b.verify_token(raw)
        self.assertIsNotNone(result, "Worker-B must see token created by worker-A")


class TestUnchangedMtimeFastPath(unittest.TestCase):
    """When mtime has NOT changed, _reload_if_stale must NOT update self._tokens."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.auth = _make_auth(self._tmp)

    def test_unchanged_mtime_skips_reload(self):
        raw, _ = self.auth.create_token("fast-path")
        # Capture mtime and mutate in-memory list directly (simulating stale
        # in-memory state while file is unchanged).
        saved_mtime = self.auth._file_mtime
        original_len = len(self.auth._tokens)

        # Secretly remove the token from the in-memory list WITHOUT touching disk.
        self.auth._tokens = []

        # File mtime has NOT changed so _reload_if_stale must be a no-op.
        # We verify by confirming the reload guard keeps _tokens empty.
        with self.auth._lock:
            mtime_before_reload = self.auth._file_mtime
            self.auth._reload_if_stale()
            mtime_after_reload = self.auth._file_mtime

        # mtime should be identical (no reload happened, no disk read).
        self.assertEqual(mtime_before_reload, mtime_after_reload)
        # The in-memory list stayed empty because no reload occurred.
        self.assertEqual(len(self.auth._tokens), 0,
                         "Fast path should not reload when mtime unchanged")

        # Sanity: the original token must still be on disk.
        tokens_on_disk = json.loads(
            (Path(self._tmp) / "api_tokens.json").read_text()
        )
        self.assertEqual(len(tokens_on_disk), original_len)

    def test_changed_mtime_triggers_reload(self):
        """Artificially back-date _file_mtime to force a reload on next call."""
        raw, _ = self.auth.create_token("must-reload")
        # Back-date the cached mtime so next _reload_if_stale thinks file changed.
        self.auth._file_mtime = 0.0

        with self.auth._lock:
            self.auth._reload_if_stale()

        # After reload _tokens must contain the token again.
        self.assertEqual(len(self.auth._tokens), 1)
        self.assertNotEqual(self.auth._file_mtime, 0.0,
                            "_file_mtime must be updated after reload")


class TestFlockConcurrentWrites(unittest.TestCase):
    """Two 'workers' calling _save concurrently must not corrupt the file."""

    def setUp(self):
        _td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(_td.cleanup)
        self._tmp = _td.name

    def test_concurrent_creates_no_corruption(self):
        """N потоков (воркеров) создают токены — НИ ОДИН не потерян, каждый проходит проверку."""
        n_workers = 4
        n_tokens_each = 5
        errors: list[Exception] = []
        issued: list[tuple[str, str]] = []  # (raw, id) всех успешных созданий
        issued_lock = threading.Lock()

        def worker_fn(worker_id: int) -> None:
            try:
                auth = _make_auth(self._tmp)  # конструктор внутри try: его ошибки попадают в errors
                for i in range(n_tokens_each):
                    raw, meta = auth.create_token(f"worker-{worker_id}-token-{i}")
                    with issued_lock:
                        issued.append((raw, meta["id"]))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker_fn, args=(i,)) for i in range(n_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertFalse([t for t in threads if t.is_alive()], "worker threads did not finish")

        self.assertEqual(errors, [], f"Worker errors: {errors}")

        # Точное множество: ровно n_workers * n_tokens_each УНИКАЛЬНЫХ записей (раньше хватало
        # len(data) > 0 — потеря 19 из 20 токенов проходила).
        expected = n_workers * n_tokens_each
        self.assertEqual(len(issued), expected)
        data = json.loads((Path(self._tmp) / "api_tokens.json").read_text())
        self.assertIsInstance(data, list)
        self.assertEqual({t["id"] for t in data}, {tid for _, tid in issued},
                         "в файле не все выданные токены (потерянная запись)")
        # каждый выданный токен проверяется НОВЫМ экземпляром RestAuth
        fresh = _make_auth(self._tmp)
        for raw, tid in issued:
            meta = fresh.verify_token(raw)
            self.assertIsNotNone(meta, f"выданный токен {tid} не проходит проверку")

    def test_file_remains_valid_json_after_concurrent_revokes(self):
        """Конкурентные отзывы: каждый успешен, файл валиден, отозванные токены недействительны."""
        auth_a = _make_auth(self._tmp)
        token_ids = []
        raw_tokens = []
        for i in range(6):
            raw, meta = auth_a.create_token(f"token-{i}")
            token_ids.append(meta["id"])
            raw_tokens.append(raw)
        raw_keep, meta_keep = auth_a.create_token("keep-me")  # не отзываемый контроль

        errors: list[Exception] = []
        results: dict[str, bool] = {}
        results_lock = threading.Lock()

        def revoke_fn(tid: str) -> None:
            try:
                auth = _make_auth(self._tmp)
                ok = auth.revoke_token(tid)
                with results_lock:
                    results[tid] = ok
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=revoke_fn, args=(tid,)) for tid in token_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertFalse([t for t in threads if t.is_alive()], "revoke threads did not finish")

        self.assertEqual(errors, [], f"Revoke errors: {errors}")
        self.assertEqual(results, {tid: True for tid in token_ids}, "не каждый revoke_token вернул True")

        data = json.loads((Path(self._tmp) / "api_tokens.json").read_text())
        self.assertIsInstance(data, list)
        self.assertEqual({t["id"] for t in data}, {meta_keep["id"]}, "остались отозванные токены")
        fresh = _make_auth(self._tmp)
        for raw in raw_tokens:
            self.assertIsNone(fresh.verify_token(raw), "отозванный токен всё ещё действителен")
        self.assertIsNotNone(fresh.verify_token(raw_keep), "неотозванный токен потерян")


class TestMtimeReloadIntegration(unittest.TestCase):
    """Integration: mtime correctly tracks each write cycle."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_mtime_advances_after_each_write(self):
        auth = _make_auth(self._tmp)
        mtime_0 = auth._file_mtime

        auth.create_token("t1")
        mtime_1 = auth._file_mtime

        auth.create_token("t2")
        mtime_2 = auth._file_mtime

        # On a fast filesystem writes may land in the same second, but
        # _file_mtime is updated from os.stat after each write so at minimum
        # it must be >= the file's actual mtime (never stale after own write).
        actual_mtime = os.stat(Path(self._tmp) / "api_tokens.json").st_mtime
        self.assertEqual(mtime_2, actual_mtime,
                         "_file_mtime must match actual file mtime after write")
        # Initial mtime was 0 (no file) or the pre-create mtime.
        self.assertGreaterEqual(mtime_1, mtime_0)
        self.assertGreaterEqual(mtime_2, mtime_1)

    def test_reload_after_external_modification(self):
        """Simulate a third process writing the file externally."""
        auth = _make_auth(self._tmp)
        auth.create_token("original")

        # External process writes a completely different token list.
        external_token = {
            "id": "ext-id-0001",
            "name": "external",
            "token_hash": "aa" * 32,
            "scopes": ["*"],
            "created_at": "2026-01-01T00:00:00+00:00",
            "last_used": None,
        }
        token_path = Path(self._tmp) / "api_tokens.json"
        token_path.write_text(json.dumps([external_token]))
        # Ensure mtime changes (write_text should be enough, but touch to be safe).
        os.utime(token_path, None)

        # Back-date our cache so _reload_if_stale sees a difference.
        auth._file_mtime = 0.0

        # verify_token for an unknown raw must trigger reload and return None.
        result = auth.verify_token("completely_unknown_token")
        self.assertIsNone(result)

        # After the reload, _tokens must reflect the external write.
        with auth._lock:
            self.assertEqual(len(auth._tokens), 1)
            self.assertEqual(auth._tokens[0]["id"], "ext-id-0001")


if __name__ == "__main__":
    unittest.main()
