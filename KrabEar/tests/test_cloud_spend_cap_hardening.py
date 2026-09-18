"""F2b: харденинг spend-cap (гонка, inf-cap, битый файл, reconcile).

Ревью F2 (#2036): MED-1 read-modify-write гонка, MED-2 inf-обход,
LOW-1 fail-open на битый файл, LOW-3 дефолт при сбое чтения.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import cloud_rewriter as cr  # noqa: E402


class ReserveSpendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.month = "2026-09"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_concurrent_reserve_no_lost_updates(self) -> None:
        """MED-1: 8 потоков × 50 резервов — ничего не теряется."""
        est = 0.0001
        cap = 100.0
        results: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(50):
                ok = cr.reserve_spend_usd(self.dir, self.month, est, cap)
                with lock:
                    results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 400)
        self.assertTrue(all(results), "при cap=100 все резервы должны пройти")
        spent = cr.read_spend_usd(self.dir, self.month)
        self.assertAlmostEqual(spent, 400 * est, places=9)

    def test_concurrent_reserve_never_exceeds_cap(self) -> None:
        """MED-1: суммарный резерв не превышает cap + один шаг."""
        est = 0.01
        cap = 0.05  # ровно 5 успешных резервов
        results: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            ok = cr.reserve_spend_usd(self.dir, self.month, est, cap)
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        granted = sum(1 for r in results if r)
        self.assertEqual(granted, 5, "ровно cap/est резервов, без обхода гонкой")
        self.assertAlmostEqual(cr.read_spend_usd(self.dir, self.month), 5 * est, places=6)

    def test_corrupt_file_denies_and_is_not_silently_reset(self) -> None:
        """LOW-1: битый файл → резерв запрещён (fail-closed), файл не перезаписан молча."""
        path = self.dir / "cloud_spend.json"
        path.write_text("{не json", encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        self.assertFalse(cr.reserve_spend_usd(self.dir, self.month, 0.001, 100.0))
        self.assertEqual(path.read_text(encoding="utf-8"), before, "битый файл не перезаписан")
        self.assertFalse(cr.reserve_spend_usd(self.dir, self.month, 0.001, 100.0))

    def test_non_finite_cap_is_rejected(self) -> None:
        """MED-2: defense-in-depth в резерве — inf/NaN cap запрещены."""
        for bad_cap in (float("inf"), float("nan"), float("-inf")):
            with self.subTest(cap=bad_cap):
                self.assertFalse(cr.reserve_spend_usd(self.dir, self.month, 0.001, bad_cap))

    def test_validator_range_registered(self) -> None:
        """MED-2: ключ в _RANGE_FIELDS с клампом (non-finite режет валидатор)."""
        from backend.settings_validator import _RANGE_FIELDS
        self.assertIn("cloud_spend_cap_usd_monthly", _RANGE_FIELDS)
        lo, hi, default, _ = _RANGE_FIELDS["cloud_spend_cap_usd_monthly"]
        self.assertEqual((lo, hi), (0.0, 1000.0))
        self.assertEqual(default, 1.0)

    def test_no_fixed_tmp_name_left_after_write(self) -> None:
        """LOW-2: unique-mkstemp (atomic_io) — фиксированного .tmp в каталоге нет."""
        self.assertTrue(cr.reserve_spend_usd(self.dir, self.month, 0.002, 100.0))
        self.assertFalse((self.dir / "cloud_spend.json.tmp").exists())

    def test_negative_est_is_rejected(self) -> None:
        """Полировка: отрицательный est — отказ, а не free-pass."""
        self.assertFalse(cr.reserve_spend_usd(self.dir, self.month, -1.0, 1.0))
        self.assertFalse((self.dir / "cloud_spend.json").exists())
        # ноль допустим (custom-тариф 0.0)
        self.assertTrue(cr.reserve_spend_usd(self.dir, self.month, 0.0, 1.0))

    def test_garbage_month_rejected(self) -> None:
        """Полировка: невалидный месяц — отказ/no-op, файл не портится self-DoS-ключом.

        Валидируется ФОРМАТ (как strict-ридер), не календарь: «2026-13» — вне скоупа.
        """
        for bad_month in ("garbage", "2026-09\n", "26-09", ""):
            with self.subTest(month=bad_month):
                self.assertFalse(cr.reserve_spend_usd(self.dir, bad_month, 0.001, 100.0))
        cr.add_spend_usd(self.dir, "garbage", 1.0)  # no-op
        self.assertTrue(cr.reserve_spend_usd(self.dir, self.month, 0.001, 100.0))
        raw = (self.dir / "cloud_spend.json").read_text(encoding="utf-8")
        self.assertNotIn("garbage", raw)

    def test_tmp_symlink_not_followed(self) -> None:
        """LOW-2: подложенный .tmp-symlink не перезаписывает жертву (mkstemp игнорирует имя)."""
        victim = self.dir / "victim.txt"
        victim.write_text("SECRET2", encoding="utf-8")
        (self.dir / "cloud_spend.json.tmp").symlink_to(victim)
        self.assertTrue(cr.reserve_spend_usd(self.dir, self.month, 0.001, 100.0))
        self.assertEqual(victim.read_text(encoding="utf-8"), "SECRET2")


class SeamReservationTests(unittest.TestCase):
    """Шов: reserve → вызов → reconcile/release (реальный LLMRewriter, мок cloud_summarize)."""

    def _rewriter(self, spend_dir: Path):
        from backend.llm_rewriter import LLMRewriter
        rw = LLMRewriter(base_url="http://127.0.0.1:1", api_key="", model="test-model")
        rw._settings_getter = lambda k, d=None: {
            "privacy_mode_enabled": False,
            "cloud_rewriter_enabled": True,
            "cloud_rewriter_provider": "openai",
            "cloud_rewriter_openai_model": "gpt-4o-mini",
            "cloud_spend_cap_usd_monthly": 100.0,
        }.get(k, d)
        rw._spend_dir = spend_dir
        return rw

    def _failed(self):
        from backend.llm_rewriter import LLMRewriteResult
        return LLMRewriteResult(ok=False, text=None, fallback_reason="timeout", latency_ms=None)

    def test_failed_cloud_releases_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rw = self._rewriter(Path(d))
            with patch("backend.cloud_rewriter.cloud_summarize", return_value=None), \
                 patch("backend.privacy_audit.get_privacy_audit_logger"):
                out = rw._maybe_apply_cloud_summarize(self._failed(), "тестовый текст 1", 3)
            self.assertFalse(out.ok)
            # Файл появился (резерв был) и обнулён release'ом (pre-fix: файла нет вовсе).
            self.assertTrue((Path(d) / "cloud_spend.json").exists())
            self.assertAlmostEqual(cr.read_spend_usd(Path(d), cr.current_month_key()), 0.0, places=9)

    def test_exception_releases_reservation(self) -> None:
        """Ветка release-on-exception: cloud_summarize бросил — резерв снят, текст не утёк."""
        with tempfile.TemporaryDirectory() as d:
            rw = self._rewriter(Path(d))
            with patch("backend.cloud_rewriter.cloud_summarize", side_effect=RuntimeError("boom")), \
                 patch("backend.privacy_audit.get_privacy_audit_logger"):
                out = rw._maybe_apply_cloud_summarize(self._failed(), "тестовый текст 3", 3)
            self.assertFalse(out.ok)
            self.assertTrue((Path(d) / "cloud_spend.json").exists())
            self.assertAlmostEqual(cr.read_spend_usd(Path(d), cr.current_month_key()), 0.0, places=9)

    def test_success_reconciles_to_actual_not_bound(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rw = self._rewriter(Path(d))
            with patch("backend.cloud_rewriter.cloud_summarize", return_value="ок"), \
                 patch("backend.privacy_audit.get_privacy_audit_logger"):
                out = rw._maybe_apply_cloud_summarize(self._failed(), "тестовый текст 2 подлиннее", 3)
            self.assertTrue(out.ok)
            month = cr.current_month_key()
            spent = cr.read_spend_usd(Path(d), month)
            bound = cr.estimate_summarize_usd("тестовый текст 2 подлиннее", "тестовый текст 2 подлиннее",
                                              "openai", "gpt-4o-mini")
            actual = cr.estimate_summarize_usd("тестовый текст 2 подлиннее", "ок", "openai", "gpt-4o-mini")
            self.assertAlmostEqual(spent, actual, places=6)
            self.assertLess(spent, bound)

    def test_missing_cap_key_blocks_cloud(self) -> None:
        """LOW-3 пин: ключа cap нет → дефолт шва 0.0 → облако не вызвано."""
        with tempfile.TemporaryDirectory() as d:
            rw = self._rewriter(Path(d))
            rw._settings_getter = lambda k, dv=None: {
                "privacy_mode_enabled": False,
                "cloud_rewriter_enabled": True,
                "cloud_rewriter_provider": "openai",
                "cloud_rewriter_openai_model": "gpt-4o-mini",
            }.get(k, dv)
            with patch("backend.cloud_rewriter.cloud_summarize") as cloud, \
                 patch("backend.privacy_audit.get_privacy_audit_logger"):
                out = rw._maybe_apply_cloud_summarize(self._failed(), "тестовый текст 4", 3)
            cloud.assert_not_called()
            self.assertFalse(out.ok)

    def test_raising_getter_blocks_cloud(self) -> None:
        """LOW-3 пин: getter падает на cap-ключе → cap=0.0 → облако не вызвано."""
        with tempfile.TemporaryDirectory() as d:
            rw = self._rewriter(Path(d))

            def _raising(key, dv=None):
                if key == "cloud_spend_cap_usd_monthly":
                    raise RuntimeError("settings store down")
                return {
                    "privacy_mode_enabled": False,
                    "cloud_rewriter_enabled": True,
                    "cloud_rewriter_provider": "openai",
                    "cloud_rewriter_openai_model": "gpt-4o-mini",
                }.get(key, dv)

            rw._settings_getter = _raising
            with patch("backend.cloud_rewriter.cloud_summarize") as cloud, \
                 patch("backend.privacy_audit.get_privacy_audit_logger"):
                out = rw._maybe_apply_cloud_summarize(self._failed(), "тестовый текст 5", 3)
            cloud.assert_not_called()
            self.assertFalse(out.ok)


if __name__ == "__main__":
    unittest.main()
