"""W9 — статус квоты Sentry обязан различать «тихо» и «нас не принимают».

Живой инцидент 2026-08-13..18: организация выбрала бесплатную квоту 5000/мес,
всё последующее ушло в rate_limited, а смок-рутина писала «Sentry quiet (0/0)»
и засчитывала это ЗДОРОВЬЕМ. Из-за этого четыре unclean-смерти backend 17-08 и
инцидент 12:21 18-08 прошли мимо владельца.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.sentry_quota import (  # noqa: E402
    QUOTA_BLIND,
    QUOTA_IDLE,
    QUOTA_OK,
    QUOTA_UNKNOWN,
    classify_quota,
    format_quota_line,
)


class ClassifyQuotaTest(unittest.TestCase):
    def test_rate_limited_without_accepted_is_blind(self):
        """Реальные цифры 2026-08-17: приёма нет, отказы есть."""
        self.assertEqual(classify_quota(accepted=0, rate_limited=33), QUOTA_BLIND)

    def test_fresh_accepted_is_ok(self):
        """Цифры 2026-08-13: приём ещё шёл."""
        self.assertEqual(classify_quota(accepted=85, rate_limited=1), QUOTA_OK)

    def test_no_events_at_all_is_idle_not_blind(self):
        """Честная тишина: ни приёма, ни отказов — система просто не падала."""
        self.assertEqual(classify_quota(accepted=0, rate_limited=0), QUOTA_IDLE)

    def test_unavailable_api_is_unknown_never_ok(self):
        """🔴 Главное: «не смог проверить» НЕ равно «всё хорошо»."""
        self.assertEqual(classify_quota(accepted=None, rate_limited=None), QUOTA_UNKNOWN)
        self.assertNotEqual(classify_quota(accepted=None, rate_limited=None), QUOTA_OK)
        self.assertNotEqual(classify_quota(accepted=None, rate_limited=None), QUOTA_IDLE)

    def test_partial_data_is_unknown(self):
        """Половина данных — тоже не основание объявлять здоровье."""
        self.assertEqual(classify_quota(accepted=None, rate_limited=5), QUOTA_UNKNOWN)
        self.assertEqual(classify_quota(accepted=5, rate_limited=None), QUOTA_UNKNOWN)

    def test_accepted_with_heavy_rate_limiting_is_degraded_not_ok(self):
        """Приём идёт, но большинство режется — это уже не здоровье."""
        self.assertEqual(classify_quota(accepted=3, rate_limited=300), QUOTA_BLIND)


class FormatQuotaLineTest(unittest.TestCase):
    """Строка для smoke-history обязана называть состояние словами."""

    def test_blind_line_says_blind_loudly(self):
        line = format_quota_line(QUOTA_BLIND, accepted=0, rate_limited=33)
        self.assertIn("СЛЕП", line.upper())
        self.assertIn("33", line)
        self.assertNotIn("quiet", line.lower())

    def test_ok_line_reports_counts(self):
        line = format_quota_line(QUOTA_OK, accepted=85, rate_limited=0)
        self.assertIn("85", line)

    def test_unknown_line_does_not_claim_health(self):
        line = format_quota_line(QUOTA_UNKNOWN, accepted=None, rate_limited=None)
        low = line.lower()
        self.assertNotIn("ok", low.replace("неизвест", ""))
        self.assertTrue("неизвест" in low or "unknown" in low)

    def test_idle_line_distinguishable_from_blind(self):
        idle = format_quota_line(QUOTA_IDLE, accepted=0, rate_limited=0)
        blind = format_quota_line(QUOTA_BLIND, accepted=0, rate_limited=33)
        self.assertNotEqual(idle, blind)


if __name__ == "__main__":
    unittest.main()
