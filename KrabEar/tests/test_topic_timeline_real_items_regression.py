# -*- coding: utf-8 -*-
"""Regression: get_topic_timeline must not crash on REAL history items.

Root cause (found by the live E2E smoke, 2026-06-21):
    handle_get_topic_timeline passed HistoryItem OBJECTS straight from
    _load_active_items_unlocked() into TopicTracker, which expects plain dicts
    and reads the text field via ``item.get(...)``. With any non-empty history
    this raised "'HistoryItem' object has no attribute 'get'" → the IPC method
    returned ok=False on every real call, so the topic-timeline feature was
    completely broken for users.

    It slipped every existing test because:
      - the dispatch smoke test ran against an EMPTY store (track_topics
        short-circuits on empty input, no .get() ever called), and
      - the privacy/DoS test MOCKED the topic_tracker, never exercising the
        real object→dict boundary.

This test seeds real items through the IPC boundary and asserts the method
returns ok=True with its documented shape — it FAILS before the to_dict() fix
(ok=False, internal_error) and PASSES after.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.service import BackendService
from backend.state_store import StateStore


def _make_service() -> BackendService:
    tmp = Path(tempfile.mkdtemp())
    store = StateStore(data_dir=tmp / "data")
    return BackendService(store=store)


class TopicTimelineRealItemsRegression(unittest.TestCase):

    def setUp(self) -> None:
        self.service = _make_service()
        # Seed a handful of varied real items so TopicTracker actually runs
        # (empty history short-circuits and never hit the bug).
        seed_texts = [
            "Сегодня обсудили квартальные продажи и маркетинговую кампанию на январь.",
            "Нужно проверить пул-реквест: CI зелёный, все тесты проходят.",
            "El informe de auditoría está completo, tres áreas de mejora identificadas.",
            "Договорились о задачах на следующий спринт, дедлайн в пятницу.",
            "Q3 revenue reached 2.4 million, customer acquisition cost dropped.",
        ]
        for text in seed_texts:
            resp = self.service.handle_request({
                "id": "seed",
                "method": "add_history_item",
                "params": {"text": text, "paste_status": "pasted"},
            })
            self.assertTrue(resp.get("ok"), f"seed add_history_item failed: {resp}")

    def tearDown(self) -> None:
        self.service.close()

    def test_get_topic_timeline_ok_with_real_items(self) -> None:
        resp = self.service.handle_request({
            "id": "t",
            "method": "get_topic_timeline",
            "params": {"window_size": 3, "limit": 10},
        })
        # Before the fix this was ok=False with
        # error.message="'HistoryItem' object has no attribute 'get'".
        self.assertTrue(
            resp.get("ok"),
            msg=f"get_topic_timeline crashed on real history items: {resp.get('error')}",
        )
        result = resp["result"]
        for key in ("segments", "total_shifts", "current_topic"):
            self.assertIn(key, result, f"get_topic_timeline result missing key {key!r}")
        self.assertIsInstance(result["segments"], list, "segments must be a list")
        self.assertIsInstance(result["total_shifts"], int, "total_shifts must be an int")

    def test_get_topic_timeline_default_params_ok(self) -> None:
        # No params → handler defaults (window_size=5, limit=100); must still be ok.
        resp = self.service.handle_request({
            "id": "t2",
            "method": "get_topic_timeline",
            "params": {},
        })
        self.assertTrue(
            resp.get("ok"),
            msg=f"get_topic_timeline (default params) crashed: {resp.get('error')}",
        )
        self.assertIn("segments", resp["result"])


if __name__ == "__main__":
    unittest.main()
