"""test_apply_recommended_setup.py — SettingsService.handle_apply_recommended_setup
(spec docs/superpowers/specs/2026-07-07-recommended-setup-design.md §1-2).

10 безусловных + 3 условных (probe-функции инжектируются как fakes — реальные
HealthCheckService/ModelDownloader вызовы см. test_apply_recommended_setup_probes.py,
Задача 2). GigaAM-пара ВСЕГДА skipped — тест это явно проверяет как политику (9.7),
не как баг.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_apply_recommended_setup.py -v
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

from backend.settings_service import SettingsService  # noqa: E402


_UNCONDITIONAL_KEYS = {
    "smart_silence_skip_enabled", "realtime_silence_filter_enabled",
    "auto_dedup_enabled", "auto_save_transcripts", "phonetic_vocab_enabled",
    "text_snippets_enabled", "auto_learn_corrections_enabled",
    "quick_edit_enabled", "paste_undo_enabled", "calendar_link_enabled",
}
_CONDITIONAL_KEYS = {"llm_rewrite_enabled", "action_items_auto_extract", "stt_sensevoice_enabled"}
_NEVER_APPLIED_KEYS = {"stt_gigaam_enabled", "stt_language_routing_enabled"}


class _FakeStore:
    def __init__(self):
        self._settings: dict = {}

    def load_settings(self, lock_timeout_sec: float | None = None, nowait: bool = False):
        return dict(self._settings)

    def save_settings(self, settings):
        self._settings = dict(settings)
        return dict(settings)


def _make_svc(tmp_dir):
    from backend.settings_backup import SettingsBackup
    backup = SettingsBackup(backup_dir=Path(tmp_dir) / "backups")
    svc = SettingsService(store=_FakeStore(), backup=backup)
    return svc


class DryRunDefaultTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_dry_run_defaults_to_true(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {}, probe_llm_fn=lambda: {"reachable": False}, sensevoice_cached_fn=lambda: False,
        )
        self.assertTrue(result["dry_run"])

    def test_dry_run_true_does_not_write_settings(self):
        svc = _make_svc(self._tmp)
        before = svc.store.load_settings()
        svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        after = svc.store.load_settings()
        self.assertEqual(before, after, "dry_run=true не должен писать settings.json")

    def test_dry_run_true_snapshot_id_is_none(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        self.assertIsNone(result["snapshot_id"])


class UnconditionalSetTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_all_ten_unconditional_keys_applied(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        applied_keys = {a["key"] for a in result["applied"]}
        self.assertTrue(_UNCONDITIONAL_KEYS.issubset(applied_keys))

    def test_dry_run_false_actually_writes_and_creates_backup(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": False}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        self.assertFalse(result["dry_run"])
        self.assertIsNotNone(result["snapshot_id"])
        saved = svc.store.load_settings()
        for key in _UNCONDITIONAL_KEYS:
            self.assertTrue(saved.get(key), f"{key} должен быть True после apply")

    def test_already_enabled_key_reported_with_reason(self):
        svc = _make_svc(self._tmp)
        svc.store.save_settings({"smart_silence_skip_enabled": True})
        svc.invalidate_cache()
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        skipped_keys = {s["key"]: s["reason"] for s in result["skipped"]}
        applied_keys = {a["key"] for a in result["applied"]}
        # уже включённый ключ — либо в applied с old==new, либо в skipped с "уже включено";
        # контракт финальной спеки допускает оба прочтения — фиксируем текущий выбор реализации:
        self.assertTrue(
            "smart_silence_skip_enabled" in applied_keys
            or skipped_keys.get("smart_silence_skip_enabled") == "уже включено"
        )


class GigaAMNeverAppliedTestCase(unittest.TestCase):
    """Решение 9.7: GigaAM-пара ВСЕГДА skipped — это политика, не баг."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_gigaam_pair_always_skipped_even_with_valid_venv_mocked(self):
        svc = _make_svc(self._tmp)
        # Мокаем "как будто" venv существует и валиден — GigaAM ВСЁ РАВНО должен остаться skipped.
        with patch("os.path.exists", return_value=True), \
                patch("pathlib.Path.is_relative_to", return_value=True):
            result = svc.handle_apply_recommended_setup(
                {"dry_run": True}, probe_llm_fn=lambda: {"reachable": True, "latency_ms": 5},
                sensevoice_cached_fn=lambda: True,
            )
        applied_keys = {a["key"] for a in result["applied"]}
        skipped_keys = {s["key"]: s["reason"] for s in result["skipped"]}
        for key in _NEVER_APPLIED_KEYS:
            self.assertNotIn(key, applied_keys, f"{key} НИКОГДА не должен быть в applied (9.7)")
            self.assertIn(key, skipped_keys)
            self.assertIn("GigaAM", skipped_keys[key])

    def test_gigaam_pair_reason_is_fixed_string_not_probe_derived(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        skipped_keys = {s["key"]: s["reason"] for s in result["skipped"]}
        self.assertEqual(
            skipped_keys["stt_gigaam_enabled"],
            "настройте GigaAM вручную в Настройках",
        )
        self.assertEqual(
            skipped_keys["stt_language_routing_enabled"],
            "настройте GigaAM вручную в Настройках",
        )


class NoDeadOrNoKeysInAppliedTestCase(unittest.TestCase):
    """Regression-тест §10 п.2 черновика: НЕТ/МЁРТВЫЕ кандидаты никогда не в applied."""

    _FORBIDDEN_KEYS = {
        # НЕТ (сеть/необратимость/тяжёлые зависимости/архитектурные/не-фичи)
        "cloud_rewriter_enabled", "recap_email_enabled",
        "auto_purge_enabled", "pipeline_v2_enabled", "rest_api_auth_enabled",
        "privacy_mode_enabled", "stt_use_ru_finetune", "voxtral_enabled",
        "voxtral_reasoning_enabled", "wake_word_engine",
        # МЁРТВЫЕ находки черновика §3.2
        "wake_word_enabled", "stt_streaming_enabled", "export_include_speaker_labels",
        # ВОПРОС-кандидаты (вне v1)
        "semantic_search_enabled", "history_encryption_enabled",
        "stt_punctuation_llm_pass_enabled", "voice_fingerprint_enabled",
    }

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_forbidden_keys_never_in_applied_dry_run_true(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": True},
            sensevoice_cached_fn=lambda: True,
        )
        applied_keys = {a["key"] for a in result["applied"]}
        overlap = applied_keys & self._FORBIDDEN_KEYS
        self.assertEqual(overlap, set(), f"Запрещённые ключи попали в applied: {overlap}")

    def test_forbidden_keys_never_in_applied_dry_run_false(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": False}, probe_llm_fn=lambda: {"reachable": True},
            sensevoice_cached_fn=lambda: True,
        )
        applied_keys = {a["key"] for a in result["applied"]}
        overlap = applied_keys & self._FORBIDDEN_KEYS
        self.assertEqual(overlap, set(), f"Запрещённые ключи попали в applied: {overlap}")

    def test_forbidden_keys_ignored_even_if_explicitly_requested_via_keys_param(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True, "keys": list(self._FORBIDDEN_KEYS)},
            probe_llm_fn=lambda: {"reachable": True}, sensevoice_cached_fn=lambda: True,
        )
        applied_keys = {a["key"] for a in result["applied"]}
        self.assertEqual(applied_keys & self._FORBIDDEN_KEYS, set())


class PrivacyModeSkipsTranscriptCandidatesTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_privacy_mode_enabled_skips_action_items_and_auto_learn_and_auto_dedup(self):
        svc = _make_svc(self._tmp)
        svc.store.save_settings({"privacy_mode_enabled": True})
        svc.invalidate_cache()
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": True},
            sensevoice_cached_fn=lambda: True,
        )
        applied_keys = {a["key"] for a in result["applied"]}
        skipped_keys = {s["key"] for s in result["skipped"]}
        for key in ("action_items_auto_extract", "auto_learn_corrections_enabled", "auto_dedup_enabled"):
            self.assertNotIn(key, applied_keys)
            self.assertIn(key, skipped_keys)

    def test_privacy_mode_disabled_does_not_skip_privacy_sensitive_keys_by_itself(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": True},
            sensevoice_cached_fn=lambda: True,
        )
        applied_keys = {a["key"] for a in result["applied"]}
        self.assertIn("auto_dedup_enabled", applied_keys)
        self.assertIn("auto_learn_corrections_enabled", applied_keys)


class SnapshotRoundTripTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_apply_then_restore_returns_to_original_settings(self):
        svc = _make_svc(self._tmp)
        original = svc.cached_settings()
        result = svc.handle_apply_recommended_setup(
            {"dry_run": False}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        snapshot_id = result["snapshot_id"]
        self.assertIsNotNone(snapshot_id)

        restore_result = svc.handle_restore_settings_backup({"backup_id": snapshot_id})
        restored = restore_result["restored_settings"]
        for key in _UNCONDITIONAL_KEYS:
            # Симметричный .get(key, False) с обеих сторон: и SettingsValidator, и
            # restore_settings_backup НЕ бэкфиллят отсутствующие ключи значениями по
            # умолчанию (fixed = dict(settings), см. settings_validator.py) — production
            # settings.json всегда полностью заполнен, но синтетический _FakeStore в этом
            # тесте начинается пустым, поэтому "отсутствует и до, и после" — корректный
            # roundtrip (приложение везде читает настройки через .get(key, default)).
            self.assertEqual(
                restored.get(key, False), original.get(key, False),
                f"{key} должен вернуться к значению до apply после restore",
            )


class RationaleAndTierTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_response_has_tier_and_rationale(self):
        svc = _make_svc(self._tmp)
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        self.assertIn(result["tier"], ("low", "mid", "high"))
        self.assertIsInstance(result["rationale"], str)
        self.assertGreater(len(result["rationale"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
