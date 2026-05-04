import unittest
from datetime import datetime, timezone

import pydantic

from backend.error_bus import KrabError


class KrabErrorModelTests(unittest.TestCase):
    def test_minimal_valid_construction(self):
        err = KrabError(
            severity="warn",
            component="rewriter",
            code="rewriter.timeout",
            message_user="Rewriter недоступен",
            message_debug="HTTP timeout after 45s",
            timestamp=datetime.now(timezone.utc),
            context={"model": "gemma"},
            actionable=False,
            action_id=None,
        )
        self.assertEqual(err.severity, "warn")
        self.assertIsNone(err.action_id)

    def test_invalid_severity_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            KrabError(
                severity="catastrophic",  # not in Literal
                component="rewriter",
                code="rewriter.timeout",
                message_user="x",
                message_debug="x",
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=False,
                action_id=None,
            )

    def test_invalid_component_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            KrabError(
                severity="warn",
                component="nonexistent",  # not in Literal
                code="x.y",
                message_user="x",
                message_debug="x",
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=False,
                action_id=None,
            )

    def test_model_dump_json_mode_serialises_datetime(self):
        ts = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        err = KrabError(
            severity="info",
            component="stt",
            code="stt.empty_text",
            message_user="x",
            message_debug="x",
            timestamp=ts,
            context={"k": "v"},
            actionable=False,
            action_id=None,
        )
        dumped = err.model_dump(mode="json")
        self.assertEqual(dumped["timestamp"], "2026-05-04T12:00:00+00:00")
