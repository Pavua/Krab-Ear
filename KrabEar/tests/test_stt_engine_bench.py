"""Tests for scripts/stt_engine_bench.py

Verifies the benchmark framework (WER, CER, repetition detection, brand hits,
run_bench structure) WITHOUT loading any real STT models.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_stt_engine_bench.py -v
    # or via unittest:
    PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar/tests/test_stt_engine_bench.py -v
"""

import sys
import unittest
from pathlib import Path

# Ensure scripts/ is importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from stt_engine_bench import (  # noqa: E402
    DEFAULT_SAMPLES,
    MockAdapter,
    compute_cer,
    compute_wer,
    count_brand_normalizations,
    detect_repetition_loop,
    render_markdown,
    run_bench,
)


class TestComputeWer(unittest.TestCase):
    """Unit tests for compute_wer."""

    def test_identical_returns_zero(self):
        self.assertEqual(compute_wer("привет как дела", "привет как дела"), 0.0)

    def test_completely_different_returns_one(self):
        # All 3 ref words are substituted/deleted — WER = 3/3 = 1.0
        result = compute_wer("один два три", "а б в")
        self.assertAlmostEqual(result, 1.0)

    def test_empty_reference_empty_hypothesis(self):
        self.assertEqual(compute_wer("", ""), 0.0)

    def test_empty_reference_nonempty_hypothesis(self):
        self.assertEqual(compute_wer("", "some words"), 1.0)

    def test_partial_overlap(self):
        # "один два" vs "один три" → 1 substitution / 2 ref words = 0.5
        result = compute_wer("один два", "один три")
        self.assertAlmostEqual(result, 0.5)

    def test_extra_words_hypothesis(self):
        # ref has 2 words, hyp has 3 (1 insertion) → WER = 1/2 = 0.5
        result = compute_wer("один два", "один два три")
        self.assertAlmostEqual(result, 0.5)

    def test_case_insensitive(self):
        self.assertEqual(compute_wer("Привет", "привет"), 0.0)


class TestComputeCer(unittest.TestCase):
    """Unit tests for compute_cer."""

    def test_identical_returns_zero(self):
        self.assertEqual(compute_cer("abc", "abc"), 0.0)

    def test_empty_both(self):
        self.assertEqual(compute_cer("", ""), 0.0)

    def test_single_substitution(self):
        # "abc" → "axc": 1 sub / 3 chars = 0.333...
        result = compute_cer("abc", "axc")
        self.assertAlmostEqual(result, 1 / 3, places=4)

    def test_empty_ref_nonempty_hyp(self):
        self.assertEqual(compute_cer("", "x"), 1.0)


class TestDetectRepetitionLoop(unittest.TestCase):
    """Tests for detect_repetition_loop (delegates to core.utils or fallback)."""

    def test_clean_text_not_loop(self):
        is_loop, reason = detect_repetition_loop("Привет, как дела? Всё хорошо.")
        self.assertFalse(is_loop)
        self.assertEqual(reason, "")

    def test_empty_text_not_loop(self):
        is_loop, _ = detect_repetition_loop("")
        self.assertFalse(is_loop)

    def test_short_text_not_loop(self):
        is_loop, _ = detect_repetition_loop("Привет")
        self.assertFalse(is_loop)

    def test_repeated_bigram_is_loop(self):
        # Classic Whisper repetition: same 2-word phrase repeated 6 times.
        repeated = "да нет " * 6
        is_loop, reason = detect_repetition_loop(repeated.strip())
        self.assertTrue(is_loop)
        self.assertTrue(len(reason) > 0)

    def test_low_unique_ratio_is_loop(self):
        # Same single word repeated 40 times — unique ratio ≈ 1/40 = 0.025 < 0.15
        repeated = "слово " * 40
        is_loop, reason = detect_repetition_loop(repeated.strip())
        self.assertTrue(is_loop)


class TestCountBrandNormalizations(unittest.TestCase):
    """Tests for count_brand_normalizations."""

    def test_no_brands_returns_zero(self):
        result = count_brand_normalizations("Обычный текст без брендов.")
        self.assertEqual(result, 0)

    def test_returns_nonnegative_int(self):
        result = count_brand_normalizations("Гемма и Антропик сделали Клод.")
        self.assertIsInstance(result, int)
        self.assertGreaterEqual(result, 0)


class TestRunBench(unittest.TestCase):
    """Tests for run_bench — verifies structure without loading models."""

    def setUp(self):
        self.samples = [
            {"audio": "fake/path/sample.wav", "reference": "тест", "lang": "ru", "domain": "test"},
            {"audio": "fake/path/silence.wav", "reference": "", "lang": "ru", "domain": "silence"},
        ]
        self.engines = ["gigaam-rnnt", "whisper-mlx"]

    def test_returns_one_result_per_engine_per_sample(self):
        results = run_bench(self.samples, self.engines, use_mock=True)
        self.assertEqual(len(results), len(self.samples) * len(self.engines))

    def test_result_has_required_keys(self):
        results = run_bench(self.samples, self.engines, use_mock=True)
        required = {"engine", "sample", "wer", "cer", "repetition_loop", "loop_reason",
                    "brand_norm_hits", "latency_ms", "mocked", "hypothesis", "reference"}
        for r in results:
            for key in required:
                self.assertIn(key, r, f"Missing key '{key}' in result")

    def test_mocked_flag_is_true_in_mock_mode(self):
        results = run_bench(self.samples, self.engines, use_mock=True)
        for r in results:
            self.assertTrue(r["mocked"])

    def test_wer_is_float_in_range(self):
        results = run_bench(self.samples, self.engines, use_mock=True)
        for r in results:
            self.assertIsInstance(r["wer"], float)
            self.assertGreaterEqual(r["wer"], 0.0)

    def test_engine_names_preserved_in_results(self):
        results = run_bench(self.samples, self.engines, use_mock=True)
        returned_engines = {r["engine"] for r in results}
        self.assertEqual(returned_engines, set(self.engines))

    def test_empty_engines_returns_empty(self):
        results = run_bench(self.samples, [], use_mock=True)
        self.assertEqual(results, [])

    def test_empty_samples_returns_empty(self):
        results = run_bench([], self.engines, use_mock=True)
        self.assertEqual(results, [])


class TestDefaultSamples(unittest.TestCase):
    """Tests for DEFAULT_SAMPLES structure."""

    def test_all_samples_have_required_fields(self):
        required = {"audio", "reference", "lang", "domain"}
        for i, sample in enumerate(DEFAULT_SAMPLES):
            for key in required:
                self.assertIn(key, sample, f"DEFAULT_SAMPLES[{i}] missing key '{key}'")

    def test_lang_is_valid(self):
        valid_langs = {"ru", "en", "es", "auto"}
        for sample in DEFAULT_SAMPLES:
            self.assertIn(sample["lang"], valid_langs)

    def test_audio_paths_are_strings(self):
        for sample in DEFAULT_SAMPLES:
            self.assertIsInstance(sample["audio"], str)

    def test_reference_is_string(self):
        for sample in DEFAULT_SAMPLES:
            self.assertIsInstance(sample["reference"], str)


class TestRenderMarkdown(unittest.TestCase):
    """Tests for render_markdown output format."""

    def test_renders_without_error(self):
        results = run_bench(
            [{"audio": "x.wav", "reference": "test", "lang": "ru", "domain": "d"}],
            ["whisper-mlx"],
            use_mock=True,
        )
        md = render_markdown(results, ["whisper-mlx"], [{"audio": "x.wav"}])
        self.assertIn("# STT Engine Benchmark Report", md)
        self.assertIn("whisper-mlx", md)

    def test_markdown_contains_table_header(self):
        results = run_bench(
            [{"audio": "x.wav", "reference": "", "lang": "ru", "domain": "d"}],
            ["gigaam-rnnt"],
            use_mock=True,
        )
        md = render_markdown(results, ["gigaam-rnnt"], [{"audio": "x.wav"}])
        self.assertIn("| Engine |", md)
        self.assertIn("| WER |", md)


class TestMockAdapter(unittest.TestCase):
    """Tests for MockAdapter."""

    def test_transcribe_returns_string(self):
        adapter = MockAdapter("test-engine")
        result = adapter.transcribe("any/path.wav")
        self.assertIsInstance(result, str)

    def test_engine_name_in_output(self):
        adapter = MockAdapter("my-engine")
        result = adapter.transcribe("any/path.wav")
        self.assertIn("my-engine", result)


if __name__ == "__main__":
    unittest.main()
