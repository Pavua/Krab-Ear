"""W9 — клиентский потолок исходящих событий в Sentry.

2026-08-13 организация выбрала бесплатную квоту 5000/мес: один issue
(`KRAB-EAR-BACKEND-1V`, зависание stop_recording) дал 2488 событий и выжег
55% месячного бюджета проекта. Серверный Key Rate Limit на бесплатном плане
недоступен — Sentry принимает PUT с HTTP 200 и молча оставляет rateLimit=null.
Значит потолок обязан стоять на КЛИЕНТЕ.

Инвариант, который проверяем: режем только ИСХОДЯЩИЙ поток. Локальная
наблюдаемость (ring buffer + событие на шине) не страдает — иначе лечение
слепоты само стало бы слепотой.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.error_bus import (  # noqa: E402
    SENTRY_HOURLY_CAP_PER_CODE,
    SENTRY_HOURLY_CAP_TOTAL,
    ErrorBus,
    KrabError,
)


class _FakeSentry:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def capture_message(self, message, level=None, tags=None, extras=None):
        self.sent.append((message, level, tags, extras))


class _FakeEventBus:
    def __init__(self) -> None:
        self.emitted: list[tuple] = []

    def emit(self, topic, payload):
        self.emitted.append((topic, payload))


def _err(code: str = "test.storm", severity: str = "error") -> KrabError:
    return KrabError(
        severity=severity,
        component="system",
        code=code,
        message_user="пользовательское сообщение",
        message_debug=f"debug {code}",
        timestamp="2026-08-18T12:00:00Z",
        context={},
        actionable=False,
        action_id=None,
    )


def _registry(*codes: str) -> dict:
    # dedupe_seconds=0 — проверяем именно потолок, а не дедуп.
    return {c: {"dedupe_seconds": 0, "severity": "error"} for c in codes}


class SentryCapTest(unittest.TestCase):
    def setUp(self):
        self.sentry = _FakeSentry()
        self.bus_events = _FakeEventBus()
        self.bus = ErrorBus(
            self.bus_events,
            _registry("test.storm", "test.other"),
            sentry_client=self.sentry,
            default_dedupe_window_sec=0.0,
        )

    def test_storm_of_one_code_is_capped(self):
        """2488 событий одного кода больше не должны уходить в Sentry."""
        for _ in range(50):
            self.bus.push(_err())
        self.assertLessEqual(len(self.sentry.sent), SENTRY_HOURLY_CAP_PER_CODE)
        self.assertGreater(len(self.sentry.sent), 0, "поток не должен глохнуть полностью")

    def test_local_visibility_is_not_capped(self):
        """🔴 Локально видно ВСЁ: режем только отправку наружу."""
        for _ in range(50):
            self.bus.push(_err())
        self.assertEqual(len(self.bus_events.emitted), 50)
        self.assertEqual(len(self.bus.list_recent(limit=100)), 50)

    def test_codes_are_capped_independently(self):
        """Шумный код не должен глушить соседний, редкий и важный."""
        for _ in range(50):
            self.bus.push(_err("test.storm"))
        before = len(self.sentry.sent)
        self.bus.push(_err("test.other"))
        self.assertEqual(
            len(self.sentry.sent), before + 1,
            "редкое событие другого кода обязано пройти",
        )

    def test_total_cap_limits_many_distinct_codes(self):
        """Много разных кодов тоже не должны выжечь бюджет."""
        codes = [f"test.code{i}" for i in range(60)]
        bus = ErrorBus(
            _FakeEventBus(), _registry(*codes),
            sentry_client=self.sentry, default_dedupe_window_sec=0.0,
        )
        for c in codes:
            bus.push(_err(c))
        self.assertLessEqual(len(self.sentry.sent), SENTRY_HOURLY_CAP_TOTAL)

    def test_window_rolls_over(self):
        """Через час потолок снова открыт — это лимит, а не выключатель."""
        with patch("backend.error_bus.time.monotonic", return_value=1000.0):
            for _ in range(50):
                self.bus.push(_err())
            capped = len(self.sentry.sent)
        with patch("backend.error_bus.time.monotonic", return_value=1000.0 + 3601):
            self.bus.push(_err())
        self.assertEqual(len(self.sentry.sent), capped + 1)

    def test_info_and_warn_paths_untouched(self):
        """info не шлётся никогда, warn идёт через батчер — потолок их не трогает."""
        for _ in range(30):
            self.bus.push(_err("test.storm", severity="info"))
        self.assertEqual(len(self.sentry.sent), 0)


if __name__ == "__main__":
    unittest.main()
