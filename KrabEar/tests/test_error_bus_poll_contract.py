"""Контракт IPC-поллинга error bus (krab_error SSE gap fix, 2026-07-05).

Прод-бэкенд (service.py, IPC-сокет) и REST-сервер (rest_server.py :5005) — два
раздельных OS-процесса, каждый со своим экземпляром backend.event_bus.bus.
ErrorBus.push() эмиттит krab_error ТОЛЬКО в EventBus IPC-процесса — событие
никогда не доходит до SSE /v1/events REST-процесса, на который подписан
native-агент (main+Errors.swift). Тосты об ошибках были декоративны в проде.

Здесь: ErrorBus.list_recent_since()/latest_seq() (backend) + since_seq-контракт
IPC handle_list_recent_errors (source-контракт на service.py, по аналогии с
test_wake_word_polling_contract.py).

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_error_bus_poll_contract.py -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
BACKEND_DIR = _PROJECT_ROOT / "KrabEar"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from backend.error_bus import ErrorBus, KrabError  # noqa: E402


def _make_err(code: str = "stt.empty_text") -> KrabError:
    return KrabError(
        severity="warn",
        component="stt",
        code=code,
        message_user="test user",
        message_debug="test debug",
        timestamp=datetime.now(timezone.utc),
        context={},
        actionable=False,
        action_id=None,
    )


def _make_bus(ring_buffer_size: int = 200) -> ErrorBus:
    return ErrorBus(
        event_bus=MagicMock(),
        registry={},
        ring_buffer_size=ring_buffer_size,
        # No dedupe window collisions between pushes in the same test tick.
        default_dedupe_window_sec=0.0,
    )


class TestListRecentSince(unittest.TestCase):
    def test_since_zero_returns_full_ring(self) -> None:
        bus = _make_bus()
        bus.push(_make_err("a"))
        bus.push(_make_err("b"))
        items, latest = bus.list_recent_since(0)
        self.assertEqual([i.code for i in items], ["a", "b"])
        self.assertEqual(latest, 2)

    def test_since_filters_to_only_newer_items(self) -> None:
        bus = _make_bus()
        bus.push(_make_err("a"))
        _, latest_after_a = bus.list_recent_since(0)
        bus.push(_make_err("b"))
        bus.push(_make_err("c"))
        items, latest = bus.list_recent_since(latest_after_a)
        self.assertEqual([i.code for i in items], ["b", "c"])
        self.assertEqual(latest, 3)

    def test_no_new_items_returns_empty_list(self) -> None:
        bus = _make_bus()
        bus.push(_make_err("a"))
        _, latest = bus.list_recent_since(0)
        items, latest2 = bus.list_recent_since(latest)
        self.assertEqual(items, [])
        self.assertEqual(latest2, latest)

    def test_seq_survives_ring_eviction(self) -> None:
        """A poller's since_seq must still work after old entries fall off the ring."""
        bus = _make_bus(ring_buffer_size=3)
        for i in range(5):
            bus.push(_make_err(f"code{i}"))
        # Ring now holds code2, code3, code4 (seq 3, 4, 5); code0/code1 evicted.
        items, latest = bus.list_recent_since(3)
        self.assertEqual([i.code for i in items], ["code3", "code4"])
        self.assertEqual(latest, 5)

    def test_latest_seq_cheap_getter_matches_list_recent_since(self) -> None:
        bus = _make_bus()
        bus.push(_make_err("a"))
        bus.push(_make_err("b"))
        _, latest = bus.list_recent_since(0)
        self.assertEqual(bus.latest_seq(), latest)

    def test_latest_seq_after_ring_eviction_is_not_ring_length(self) -> None:
        """Regression guard: a bug that implements latest_seq() as len(self._ring)
        instead of self._next_seq would pass every OTHER test in this file (none
        of them push past ring_buffer_size while also calling latest_seq()
        directly) but silently make since_seq comparisons wrong the moment the
        ring evicts anything in production (ring_buffer_size=200 by default)."""
        bus = _make_bus(ring_buffer_size=3)
        for i in range(5):
            bus.push(_make_err(f"code{i}"))
        # Ring length is capped at 3, but 5 pushes have happened.
        self.assertEqual(bus.latest_seq(), 5)


class TestClearPreservesSeqMonotonicity(unittest.TestCase):
    def test_clear_does_not_reset_next_seq(self) -> None:
        """A stale since_seq from before a clear() must never look 'newer' than
        a freshly re-pushed error after the ring empties — clear() must NOT
        reset the sequence counter."""
        bus = _make_bus()
        bus.push(_make_err("a"))
        bus.push(_make_err("b"))
        seq_before_clear = bus.latest_seq()
        bus.clear()
        bus.push(_make_err("c"))
        items, latest = bus.list_recent_since(seq_before_clear)
        self.assertEqual([i.code for i in items], ["c"])
        self.assertGreater(latest, seq_before_clear)


class TestServiceWiringSourceContract(unittest.TestCase):
    """handle_list_recent_errors must expose the since_seq poll contract and
    always echo latest_seq — the poller has no other way to bootstrap."""

    def test_handler_supports_since_seq_and_returns_latest_seq(self) -> None:
        src = (BACKEND_DIR / "backend" / "service.py").read_text(encoding="utf-8")
        self.assertIn('"since_seq" in params', src)
        self.assertIn('"latest_seq": latest_seq', src)


if __name__ == "__main__":
    unittest.main()
