"""D.2.3 — Tests for scored STT adapter selection (KrabEar/core/stt_router.py).

Covers:
  - score_adapter: match / multilingual / no-support
  - speed bonus per engine
  - quality bonus per engine
  - long-audio penalty for GigaAM
  - select_adapter_scored: picks best adapter for RU / EN / ZH / unknown
  - get_stt_routing_decision IPC handler
  - None audio_duration_s handling
"""

from __future__ import annotations

import sys
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

# Project root on sys.path for standalone discovery
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.stt_router import (  # noqa: E402
    score_adapter,
    score_adapters,
    select_adapter_scored,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _adapter(name: str, languages: "set[str]", available: bool = True) -> SimpleNamespace:
    """Create a minimal duck-typed adapter stub."""
    ns = SimpleNamespace(
        name=name,
        supported_languages=languages,
    )
    ns.is_available = lambda: available  # type: ignore[attr-defined]
    return ns


GIGAAM = _adapter("gigaam", {"ru", "uk"})
PARAKEET = _adapter("parakeet", {"en"})
SENSEVOICE = _adapter("sensevoice", {"zh", "ja", "ko", "yue", "en", "ru"})
WHISPER_MLX = _adapter("whisper-mlx", set())          # multilingual
UNAVAILABLE = _adapter("unavail-adapter", {"ru"}, available=False)


# ---------------------------------------------------------------------------
# score_adapter — language match
# ---------------------------------------------------------------------------

class TestScoreAdapterLanguageMatch(unittest.TestCase):

    def test_score_exact_language_match_wins(self):
        """Exact language support gives 100-base score (+ bonuses)."""
        score = score_adapter(GIGAAM, "ru")
        self.assertGreaterEqual(score, 100)

    def test_score_multilingual_fallback(self):
        """Multilingual adapter (empty supported_languages) scores 60-base."""
        score_whisper = score_adapter(WHISPER_MLX, "ru")
        # Should be > 0 (multilingual support) and include quality bonus
        self.assertGreater(score_whisper, 0)
        # Base = 60 + quality(whisper=15) = 75
        self.assertEqual(score_whisper, 75)

    def test_score_no_support_gets_zero(self):
        """Adapter with explicit unsupported language returns 0."""
        # parakeet only supports "en"
        score = score_adapter(PARAKEET, "ru")
        self.assertEqual(score, 0)

    def test_score_zh_not_supported_by_gigaam(self):
        """GigaAM does not support ZH — score must be 0."""
        score = score_adapter(GIGAAM, "zh")
        self.assertEqual(score, 0)

    def test_score_unknown_lang_multilingual_still_scores(self):
        """Whisper-MLX (multilingual) still scores > 0 for unknown lang."""
        score = score_adapter(WHISPER_MLX, "xyz")
        self.assertGreater(score, 0)


# ---------------------------------------------------------------------------
# score_adapter — speed bonus
# ---------------------------------------------------------------------------

class TestSpeedBonus(unittest.TestCase):

    def test_speed_bonus_for_specialized_gigaam(self):
        """GigaAM gets +20 speed bonus."""
        score = score_adapter(GIGAAM, "ru")
        # 100 (match) + 20 (speed) + 10 (quality) = 130
        self.assertEqual(score, 130)

    def test_speed_bonus_for_parakeet(self):
        """Parakeet gets +20 speed bonus."""
        score = score_adapter(PARAKEET, "en")
        # 100 (match) + 20 (speed) + 10 (quality) = 130
        self.assertEqual(score, 130)

    def test_speed_bonus_for_sensevoice(self):
        """SenseVoice gets +10 speed bonus."""
        score = score_adapter(SENSEVOICE, "zh")
        # 100 (match) + 10 (speed) + 0 (quality) = 110
        self.assertEqual(score, 110)

    def test_no_speed_bonus_for_whisper(self):
        """Whisper-MLX gets 0 speed bonus (generalist)."""
        score = score_adapter(WHISPER_MLX, "en")
        # 60 (multilingual) + 0 (speed) + 15 (quality) = 75
        self.assertEqual(score, 75)


# ---------------------------------------------------------------------------
# score_adapter — quality bonus
# ---------------------------------------------------------------------------

class TestQualityBonus(unittest.TestCase):

    def test_quality_bonus_whisper_mlx(self):
        """Whisper-MLX gets +15 quality bonus for confidence reliability."""
        score = score_adapter(WHISPER_MLX, "ru")
        # 60 (multilingual) + 15 (quality) = 75
        self.assertEqual(score, 75)

    def test_quality_bonus_gigaam(self):
        """GigaAM gets +10 quality bonus per RU-mat benchmark."""
        score = score_adapter(GIGAAM, "ru")
        # 100 + 20 (speed) + 10 (quality) = 130
        self.assertEqual(score, 130)

    def test_quality_bonus_parakeet(self):
        """Parakeet gets +10 quality bonus per EN-WER benchmark."""
        score = score_adapter(PARAKEET, "en")
        # 100 + 20 (speed) + 10 (quality) = 130
        self.assertEqual(score, 130)


# ---------------------------------------------------------------------------
# score_adapter — long audio penalty
# ---------------------------------------------------------------------------

class TestLongAudioPenalty(unittest.TestCase):

    def test_long_audio_penalty_for_gigaam(self):
        """GigaAM получает штраф сразу после upstream-границы 25 секунд."""
        short_score = score_adapter(GIGAAM, "ru", audio_duration_s=20.0)
        long_score = score_adapter(GIGAAM, "ru", audio_duration_s=25.1)
        self.assertEqual(short_score - long_score, 50)

    def test_no_penalty_for_gigaam_at_threshold(self):
        """Ровно 25 секунд ещё допустимы shortform-контрактом GigaAM."""
        score_at_threshold = score_adapter(GIGAAM, "ru", audio_duration_s=25.0)
        score_below = score_adapter(GIGAAM, "ru", audio_duration_s=24.9)
        self.assertEqual(score_at_threshold, score_below)

    def test_no_long_audio_penalty_for_whisper(self):
        """Whisper-MLX has no duration penalty even for long audio."""
        short = score_adapter(WHISPER_MLX, "ru", audio_duration_s=20.0)
        long = score_adapter(WHISPER_MLX, "ru", audio_duration_s=120.0)
        self.assertEqual(short, long)

    def test_no_long_audio_penalty_for_parakeet(self):
        """Parakeet has no duration penalty for long audio."""
        short = score_adapter(PARAKEET, "en", audio_duration_s=10.0)
        long = score_adapter(PARAKEET, "en", audio_duration_s=120.0)
        self.assertEqual(short, long)

    def test_none_duration_no_penalty(self):
        """None duration means no GigaAM penalty."""
        score_no_dur = score_adapter(GIGAAM, "ru", audio_duration_s=None)
        score_short = score_adapter(GIGAAM, "ru", audio_duration_s=10.0)
        self.assertEqual(score_no_dur, score_short)


# ---------------------------------------------------------------------------
# select_adapter_scored — integration
# ---------------------------------------------------------------------------

class TestSelectAdapterScored(unittest.TestCase):

    def _all_adapters(self):
        return [GIGAAM, PARAKEET, SENSEVOICE, WHISPER_MLX]

    def test_routing_picks_parakeet_for_en(self):
        """For EN, Parakeet should win (100+20+10=130 > Whisper 75, SenseVoice 110)."""
        best = select_adapter_scored("en", None, self._all_adapters())
        self.assertIsNotNone(best)
        self.assertEqual(best.name, "parakeet")

    def test_routing_picks_gigaam_for_ru(self):
        """For RU with short audio, GigaAM should win (130 > SenseVoice 110 > Whisper 75)."""
        best = select_adapter_scored("ru", 10.0, self._all_adapters())
        self.assertIsNotNone(best)
        self.assertEqual(best.name, "gigaam")

    def test_routing_picks_sensevoice_for_zh(self):
        """For ZH, SenseVoice should win (110 > Whisper 75; GigaAM=0, Parakeet=0)."""
        best = select_adapter_scored("zh", None, self._all_adapters())
        self.assertIsNotNone(best)
        self.assertEqual(best.name, "sensevoice")

    def test_routing_falls_back_to_whisper_for_unknown(self):
        """For unknown lang (xyz), only Whisper-MLX (multilingual) scores > 0."""
        adapters = [GIGAAM, PARAKEET, SENSEVOICE, WHISPER_MLX]
        best = select_adapter_scored("xyz", None, adapters)
        self.assertIsNotNone(best)
        self.assertEqual(best.name, "whisper-mlx")

    def test_routing_fallback_when_gigaam_penalised_long(self):
        """For long RU audio, GigaAM penalty (80) < Whisper (75) — GigaAM wins only if still higher.

        GigaAM long: 130 - 50 = 80 > Whisper 75 → GigaAM still wins unless very long.
        Score is still 80 vs 75 so GigaAM wins but with reduced margin.
        """
        best = select_adapter_scored("ru", 35.0, self._all_adapters())
        self.assertIsNotNone(best)
        # GigaAM penalised: 80 vs SenseVoice 110 → SenseVoice wins
        self.assertEqual(best.name, "sensevoice")

    def test_routing_empty_adapters_returns_none(self):
        """Empty adapter list returns None."""
        result = select_adapter_scored("ru", None, [])
        self.assertIsNone(result)

    def test_routing_unavailable_adapter_ignored(self):
        """Unavailable adapter is scored 0 and not selected."""
        best = select_adapter_scored("ru", None, [UNAVAILABLE, WHISPER_MLX])
        self.assertIsNotNone(best)
        self.assertEqual(best.name, "whisper-mlx")

    def test_routing_decision_handles_no_audio_duration(self):
        """None audio_duration_s works without error."""
        best = select_adapter_scored("ru", None, self._all_adapters())
        self.assertIsNotNone(best)


# ---------------------------------------------------------------------------
# score_adapters — full dict
# ---------------------------------------------------------------------------

class TestScoreAdapters(unittest.TestCase):

    def test_returns_scores_for_all_adapters(self):
        adapters = [GIGAAM, PARAKEET, SENSEVOICE, WHISPER_MLX]
        scores = score_adapters(adapters, "ru", 10.0)
        self.assertIn("gigaam", scores)
        self.assertIn("parakeet", scores)
        self.assertIn("sensevoice", scores)
        self.assertIn("whisper-mlx", scores)

    def test_unavailable_adapter_gets_zero_in_dict(self):
        scores = score_adapters([UNAVAILABLE, WHISPER_MLX], "ru", None)
        self.assertEqual(scores["unavail-adapter"], 0)
        self.assertGreater(scores["whisper-mlx"], 0)


# ---------------------------------------------------------------------------
# IPC handler — handle_get_stt_routing_decision (live extracted)
# ---------------------------------------------------------------------------

class TestRoutingDecisionIPC(unittest.TestCase):
    """Tests for STTManagementService.handle_get_stt_routing_decision (live extracted handler)."""

    def _make_stt_svc(self):
        """Build a minimal STTManagementService stub for handler testing."""
        sys.modules.setdefault("backend.privacy_audit", MagicMock())
        sys.modules.setdefault("backend.audit_logger", MagicMock())

        try:
            from backend.stt_management_service import STTManagementService
        except Exception:
            self.skipTest("STTManagementService import failed in test env")
            return None

        settings_svc = MagicMock()
        svc = STTManagementService(settings_svc=settings_svc, transcriber=None)
        return svc

    def test_routing_decision_ipc_returns_scores(self):
        """IPC handler returns selected_engine and scores dict."""
        svc = self._make_stt_svc()
        if svc is None:
            return

        # Patch _build_virtual_adapters_for_routing to return controlled adapters
        svc._build_virtual_adapters_for_routing = lambda: [
            _adapter("gigaam", {"ru", "uk"}),
            _adapter("whisper-mlx", set()),
        ]

        result = svc.handle_get_stt_routing_decision({
            "language": "ru",
            "audio_duration_s": 10.0,
        })

        self.assertIn("selected_engine", result)
        self.assertIn("scores", result)
        self.assertIsInstance(result["scores"], dict)
        self.assertEqual(result["language"], "ru")
        self.assertAlmostEqual(result["audio_duration_s"], 10.0)
        # GigaAM should win for short RU audio
        self.assertEqual(result["selected_engine"], "gigaam")

    def test_routing_decision_handles_no_audio_duration(self):
        """Handler works when audio_duration_s is not provided."""
        svc = self._make_stt_svc()
        if svc is None:
            return

        svc._build_virtual_adapters_for_routing = lambda: [
            _adapter("whisper-mlx", set()),
        ]

        result = svc.handle_get_stt_routing_decision({"language": "en"})
        self.assertIsNone(result.get("audio_duration_s"))
        self.assertEqual(result["selected_engine"], "whisper-mlx")

    def test_routing_decision_no_language_uses_und(self):
        """Missing language param defaults to 'und' — only multilingual adapters match."""
        svc = self._make_stt_svc()
        if svc is None:
            return

        svc._build_virtual_adapters_for_routing = lambda: [
            _adapter("gigaam", {"ru"}),
            _adapter("whisper-mlx", set()),
        ]

        result = svc.handle_get_stt_routing_decision({})
        self.assertEqual(result["language"], "und")
        # Only whisper-mlx supports unknown lang
        self.assertEqual(result["selected_engine"], "whisper-mlx")


if __name__ == "__main__":
    unittest.main()
