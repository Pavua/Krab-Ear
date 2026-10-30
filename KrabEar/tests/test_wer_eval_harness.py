"""Unit tests for scripts/wer_eval.py — без живого REST и без MLX."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
for _p in (_SCRIPTS, _REPO_ROOT / "KrabEar"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import wer_eval  # noqa: E402
from record_golden import Phrase, parse_scenario  # noqa: E402
from stt_engine_bench import compute_wer  # noqa: E402


_MINI_SCENARIO = """\
## Фразы RU
1. Привет, мир!
2. Два три.

## Фразы ES
1. Hola mundo.

## Фразы EN
1. Hello world.
"""


class TestReferenceMapping(unittest.TestCase):
    def test_build_reference_map_from_scenario(self):
        phrases = parse_scenario(_MINI_SCENARIO)
        ref_map = wer_eval.build_reference_map(phrases)
        self.assertEqual(ref_map["ru_001"], "Привет, мир!")
        self.assertEqual(ref_map["ru_002"], "Два три.")
        self.assertEqual(ref_map["es_001"], "Hola mundo.")
        self.assertEqual(ref_map["en_001"], "Hello world.")

    def test_wav_stem_to_reference(self):
        phrases = parse_scenario(_MINI_SCENARIO)
        ref_map = wer_eval.build_reference_map(phrases)
        self.assertEqual(wer_eval.reference_for_stem("ru_001", ref_map), "Привет, мир!")

    def test_missing_reference_raises_clear_error(self):
        ref_map = {"ru_001": "x"}
        with self.assertRaises(wer_eval.MissingReferenceError) as ctx:
            wer_eval.reference_for_stem("ru_999", ref_map)
        self.assertIn("ru_999", str(ctx.exception))


class TestNormalizeForWer(unittest.TestCase):
    def test_strips_punctuation_and_lowercases(self):
        out = wer_eval.normalize_for_wer("Привет, мир!")
        self.assertEqual(out, "привет мир")

    def test_module_source_does_not_reference_mlx(self):
        import inspect

        src = inspect.getsource(wer_eval)
        self.assertNotIn("mlx_whisper", src)
        self.assertNotIn("import mlx", src)


class TestWerWithOps(unittest.TestCase):
    def test_perfect_match(self):
        ref = wer_eval.normalize_for_wer("один два")
        hyp = wer_eval.normalize_for_wer("один два")
        detail = wer_eval.wer_detail(ref, hyp)
        self.assertAlmostEqual(detail["wer"], 0.0)
        self.assertEqual(detail["substitutions"], 0)
        self.assertEqual(detail["deletions"], 0)
        self.assertEqual(detail["insertions"], 0)
        self.assertAlmostEqual(compute_wer(ref, hyp), detail["wer"])

    def test_one_substitution(self):
        ref = wer_eval.normalize_for_wer("один два")
        hyp = wer_eval.normalize_for_wer("один три")
        detail = wer_eval.wer_detail(ref, hyp)
        self.assertAlmostEqual(detail["wer"], 0.5)
        self.assertEqual(detail["substitutions"], 1)
        self.assertEqual(detail["deletions"], 0)
        self.assertEqual(detail["insertions"], 0)

    def test_insertion_counts(self):
        ref = wer_eval.normalize_for_wer("один два")
        hyp = wer_eval.normalize_for_wer("один два три")
        detail = wer_eval.wer_detail(ref, hyp)
        self.assertAlmostEqual(detail["wer"], 0.5)
        self.assertEqual(detail["insertions"], 1)


class TestAggregate(unittest.TestCase):
    def test_aggregate_by_language_and_overall(self):
        rows = [
            {"stem": "ru_001", "lang": "ru", "wer": 0.1, "substitutions": 1, "deletions": 0, "insertions": 0, "ref_words": 10},
            {"stem": "ru_002", "lang": "ru", "wer": 0.3, "substitutions": 2, "deletions": 1, "insertions": 0, "ref_words": 10},
            {"stem": "es_001", "lang": "es", "wer": 0.0, "substitutions": 0, "deletions": 0, "insertions": 0, "ref_words": 5},
        ]
        agg = wer_eval.aggregate_results(rows)
        self.assertAlmostEqual(agg["by_lang"]["ru"]["mean_wer"], 0.2)
        self.assertEqual(agg["by_lang"]["ru"]["count"], 2)
        self.assertAlmostEqual(agg["by_lang"]["es"]["mean_wer"], 0.0)
        self.assertAlmostEqual(agg["overall"]["mean_wer"], (0.1 + 0.3 + 0.0) / 3)
        self.assertEqual(agg["overall"]["count"], 3)


class TestRestClient(unittest.TestCase):
    def _fake_wav(self, tmp: Path) -> Path:
        tmp.mkdir(parents=True, exist_ok=True)
        p = tmp / "ru_001.wav"
        p.write_bytes(b"RIFF" + b"\x00" * 40)
        return p

    def test_privacy_mode_403(self):
        resp = MagicMock()
        resp.status_code = 403
        resp.ok = False
        resp.json.return_value = {"ok": False, "skipped": "privacy_mode"}
        with patch("wer_eval.requests.post", return_value=resp):
            with self.assertRaises(wer_eval.RestPrivacyError) as ctx:
                wer_eval.transcribe_wav_rest(
                    self._fake_wav(Path("/tmp/wer_eval_test_priv")),
                    base_url="http://127.0.0.1:5005",
                    lang_hint="ru",
                )
        self.assertIn("privacy", str(ctx.exception).lower())

    def test_connection_failure(self):
        with patch(
            "wer_eval.requests.post",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            with self.assertRaises(wer_eval.RestUnavailableError):
                wer_eval.transcribe_wav_rest(
                    self._fake_wav(Path("/tmp/wer_eval_test_conn")),
                    base_url="http://127.0.0.1:5005",
                    lang_hint="ru",
                )

    def test_success_parses_text(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.json.return_value = {"status": "ok", "text": " привет мир "}
        with patch("wer_eval.requests.post", return_value=resp) as mock_post:
            text = wer_eval.transcribe_wav_rest(
                self._fake_wav(Path("/tmp/wer_eval_test_ok")),
                base_url="http://127.0.0.1:5005",
                lang_hint="ru",
            )
        self.assertEqual(text, " привет мир ")
        data = mock_post.call_args.kwargs["data"]
        self.assertEqual(data.get("persist_history"), "false")
        self.assertEqual(data.get("diarize"), "false")


class TestJsonReport(unittest.TestCase):
    def test_build_report_serializable(self):
        rows = [
            {
                "stem": "ru_001",
                "lang": "ru",
                "wer": 0.0,
                "substitutions": 0,
                "deletions": 0,
                "insertions": 0,
                "ref_words": 2,
                "reference": "a",
                "hypothesis": "a",
            }
        ]
        report = wer_eval.build_json_report(rows, wer_eval.aggregate_results(rows))
        json.dumps(report, ensure_ascii=False)
        self.assertIn("overall", report["aggregate"])


if __name__ == "__main__":
    unittest.main()
