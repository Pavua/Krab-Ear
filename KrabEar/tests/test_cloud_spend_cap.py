"""F2: месячный spend-cap облачного фоллбэка summary (D3-узко).

Шов уже в проде (_maybe_apply_cloud_summarize); здесь только cap.
Сеть запрещена: cloud_summarize всегда мок.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm_rewriter import LLMRewriteResult, LLMRewriter


def _rewriter(settings: dict, spend_dir: Path | str | None) -> LLMRewriter:
    rw = LLMRewriter(base_url="http://127.0.0.1:1", api_key="", model="test-model")
    rw._settings_getter = lambda k, d=None: settings.get(k, d)
    rw._spend_dir = spend_dir
    return rw


def _failed_result() -> LLMRewriteResult:
    # timeout ∈ _STUDIO_UNAVAILABLE_REASONS → is_studio_unavailable True.
    return LLMRewriteResult(ok=False, text=None, fallback_reason="timeout", latency_ms=None)


def _no_audit():
    # get_privacy_audit_logger импортируется ВНУТРИ _maybe_apply_cloud_summarize
    # (from ... import в момент вызова) — патчить нужно место ОПРЕДЕЛЕНИЯ:
    return patch("backend.privacy_audit.get_privacy_audit_logger")


class CloudSpendCapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.spend_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _base_settings(self, **over):
        cfg = {"privacy_mode_enabled": False, "cloud_rewriter_enabled": True,
               "cloud_rewriter_provider": "openai", "cloud_spend_cap_usd_monthly": 100.0}
        cfg.update(over)
        return cfg

    def test_cap_exceeded_blocks_cloud(self) -> None:
        """RED-критерий №1: лимит 0 → cloud_summarize НЕ вызывается."""
        rw = _rewriter(self._base_settings(cloud_spend_cap_usd_monthly=0.0), self.spend_dir)
        with patch("backend.cloud_rewriter.cloud_summarize") as cloud, _no_audit():
            out = rw._maybe_apply_cloud_summarize(_failed_result(), "тестовый текст 1", 3)
        cloud.assert_not_called()
        self.assertFalse(out.ok)

    def test_cap_allows_and_records_spend(self) -> None:
        """RED-критерий №2: лимит есть → вызов + запись трат в файл."""
        rw = _rewriter(self._base_settings(), self.spend_dir)
        with patch("backend.cloud_rewriter.cloud_summarize", return_value="облачное саммари") as cloud, _no_audit():
            out = rw._maybe_apply_cloud_summarize(_failed_result(), "тестовый текст 2", 3)
        cloud.assert_called_once()
        self.assertTrue(out.ok)
        self.assertEqual(out.text, "облачное саммари")
        from backend import cloud_rewriter as cr
        spent = cr.read_spend_usd(self.spend_dir, cr.current_month_key())
        self.assertGreater(spent, 0.0)

    def test_no_spend_dir_blocks_cloud(self) -> None:
        """_spend_dir None → fail-closed (RED-критерий №3: до имплементации облако вызывается)."""
        rw = _rewriter(self._base_settings(), None)
        with patch("backend.cloud_rewriter.cloud_summarize") as cloud, _no_audit():
            out = rw._maybe_apply_cloud_summarize(_failed_result(), "тестовый текст 3", 3)
        cloud.assert_not_called()
        self.assertFalse(out.ok)

    def test_privacy_mode_blocks_before_cap(self) -> None:
        """privacy ON → облака нет (guard, был и будет)."""
        rw = _rewriter(self._base_settings(privacy_mode_enabled=True), self.spend_dir)
        with patch("backend.cloud_rewriter.cloud_summarize") as cloud, _no_audit():
            out = rw._maybe_apply_cloud_summarize(_failed_result(), "тестовый текст 4", 3)
        cloud.assert_not_called()
        self.assertFalse(out.ok)

    def test_empty_catalog_never_reaches_cap(self) -> None:
        """Пустой каталог ≠ DOWN (guard, был и будет)."""
        rw = _rewriter(self._base_settings(), self.spend_dir)
        studio_empty = LLMRewriteResult(ok=False, text=None, fallback_reason="studio_empty_no_autoload",
                                        latency_ms=None)
        with patch("backend.cloud_rewriter.cloud_summarize") as cloud, _no_audit():
            out = rw._maybe_apply_cloud_summarize(studio_empty, "тестовый текст 5", 3)
        cloud.assert_not_called()
        self.assertFalse(out.ok)

    def test_spend_helpers_roundtrip(self) -> None:
        """read/add атомарно считают месяц (хелперы новые: pre-impl это ERROR — ожидаемо, см. ниже)."""
        from backend import cloud_rewriter as cr
        month = cr.current_month_key()
        self.assertEqual(cr.read_spend_usd(self.spend_dir, month), 0.0)
        cr.add_spend_usd(self.spend_dir, month, 0.001)
        cr.add_spend_usd(self.spend_dir, month, 0.002)
        self.assertAlmostEqual(cr.read_spend_usd(self.spend_dir, month), 0.003)


if __name__ == "__main__":
    unittest.main()
