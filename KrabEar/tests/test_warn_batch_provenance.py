"""Sentry delivery time must not erase the original warning time range."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.error_bus import KrabError, WarnBatcher  # noqa: E402


class RecordingSentry:
    def __init__(self):
        self.events = []

    def capture_message(self, message, **kwargs):
        self.events.append({"message": message, **kwargs})


def warning(timestamp, *, code="test.cache_missing", context=None):
    return KrabError(
        severity="warn", component="startup", code=code,
        message_user="Synthetic warning", message_debug="Synthetic cache miss",
        timestamp=timestamp, context=context or {}, actionable=False, action_id=None,
    )


def test_delayed_flush_retains_original_warning_time():
    sentry = RecordingSentry()
    batcher = WarnBatcher(sentry)
    original = "2001-01-02T03:04:05+00:00"
    batcher.add(warning(original, context={"model": "synthetic-model"}))
    assert not sentry.events
    before = datetime.now(timezone.utc)
    assert batcher.flush_all() == 1
    after = datetime.now(timezone.utc)
    event = sentry.events[0]
    provenance = event["extras"]["warn_batch"]
    assert provenance["first_error_at"] == original
    assert provenance["last_error_at"] == original
    assert before <= datetime.fromisoformat(provenance["flushed_at"]) <= after
    assert event["extras"]["model"] == "synthetic-model"
    assert event["extras"]["count"] == 1
    assert event["level"] == "warning"
    assert event["tags"]["code"] == "test.cache_missing"


def test_size_flush_uses_chronological_utc_range_for_mixed_timestamps():
    sentry = RecordingSentry()
    batcher = WarnBatcher(sentry, batch_size=3)
    batcher.add(warning("2001-01-02T04:00:00+01:00"))
    batcher.add(warning(datetime(2001, 1, 2, 1)))  # naive means UTC
    batcher.add(warning("2001-01-01T23:00:00-03:00"))
    assert len(sentry.events) == 1
    provenance = sentry.events[0]["extras"]["warn_batch"]
    assert provenance["first_error_at"] == "2001-01-02T01:00:00+00:00"
    assert provenance["last_error_at"] == "2001-01-02T03:00:00+00:00"
    assert batcher.flush_all() == 0
    assert len(sentry.events) == 1


def test_context_cannot_override_warning_provenance():
    sentry = RecordingSentry()
    batcher = WarnBatcher(sentry, batch_size=1)
    original = "2001-01-02T03:04:05+00:00"
    context = {"warn_batch": {"first_error_at": "spoofed"}, "model": "synthetic"}
    err = warning(original, context=context)
    batcher.add(err)
    extras = sentry.events[0]["extras"]
    assert extras["warn_batch"]["first_error_at"] == original
    assert extras["model"] == "synthetic"
    assert err.context == context


def test_code_and_subsequent_batch_ranges_are_independent():
    sentry = RecordingSentry()
    batcher = WarnBatcher(sentry, batch_size=2)
    batcher.add(warning("2001-01-01T00:00:00Z", code="test.a"))
    batcher.add(warning("2002-01-01T00:00:00Z", code="test.b"))
    batcher.add(warning("2001-01-02T00:00:00Z", code="test.a"))
    batcher.add(warning("2003-01-01T00:00:00Z", code="test.a"))
    assert batcher.flush_all() == 2
    ranges = [event["extras"]["warn_batch"] for event in sentry.events]
    assert [(r["first_error_at"], r["last_error_at"]) for r in ranges] == [
        ("2001-01-01T00:00:00+00:00", "2001-01-02T00:00:00+00:00"),
        ("2002-01-01T00:00:00+00:00", "2002-01-01T00:00:00+00:00"),
        ("2003-01-01T00:00:00+00:00", "2003-01-01T00:00:00+00:00"),
    ]
