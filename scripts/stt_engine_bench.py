#!/usr/bin/env python3
"""STT engine benchmark — compare Whisper, GigaAM, RU fine-tune on standard samples.

Usage:
    python3 scripts/stt_engine_bench.py --audio path/to/sample.wav --reference 'text'
    python3 scripts/stt_engine_bench.py --suite default

Output: markdown report with WER, char-error rate, repetition loop detections,
        and brand normalization counts per engine.

Notes:
    - Does NOT load actual STT models. For framework validation and dry-run only.
    - To run with real audio: engine adapters must be importable and models loaded.
    - Default samples assume tests/audio/ directory with standard fixtures.
    - Branch: feat/phase-d-prep-batch5-2026-05-05 (Phase D prep)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Standard test samples with reference texts.
# Add real .wav files to KrabEar/tests/audio/ and extend this list.
# ---------------------------------------------------------------------------
DEFAULT_SAMPLES: list[dict[str, str]] = [
    {
        "audio": "KrabEar/tests/audio/ru_short_clean.wav",
        "reference": "Привет, как дела?",
        "lang": "ru",
        "domain": "casual",
    },
    {
        "audio": "KrabEar/tests/audio/ru_brands.wav",
        "reference": "Использую Krab Ear с моделью Gemma 4 в LM Studio.",
        "lang": "ru",
        "domain": "technical",
    },
    {
        "audio": "KrabEar/tests/audio/ru_silence_30s.wav",
        "reference": "",
        "lang": "ru",
        "domain": "silence",
    },
    {
        "audio": "KrabEar/tests/audio/ru_mat_heavy.wav",
        "reference": "Разговорная речь с техническими терминами.",
        "lang": "ru",
        "domain": "casual_mat",
    },
]


# ---------------------------------------------------------------------------
# WER (Word Error Rate) — pure Python, no jiwer required for dry-run.
# ---------------------------------------------------------------------------

def compute_wer(reference: str, hypothesis: str) -> float:
    """Word Error Rate via Levenshtein edit distance on word tokens.

    Returns:
        Float in [0, inf); 0.0 = perfect, 1.0 = substituted all words.
        Returns 1.0 when reference is empty but hypothesis has words.
        Returns 0.0 when both are empty.
    """
    ref_words = reference.lower().split()
    hyp_words = hypothesis.lower().split()
    if not ref_words:
        return 1.0 if hyp_words else 0.0

    m, n = len(ref_words), len(hyp_words)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if ref_words[i - 1] == hyp_words[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])
    return dp[m][n] / max(len(ref_words), 1)


def compute_cer(reference: str, hypothesis: str) -> float:
    """Character Error Rate — same edit distance logic but on characters."""
    ref_chars = list(reference.lower())
    hyp_chars = list(hypothesis.lower())
    if not ref_chars:
        return 1.0 if hyp_chars else 0.0

    m, n = len(ref_chars), len(hyp_chars)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if ref_chars[i - 1] == hyp_chars[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])
    return dp[m][n] / max(len(ref_chars), 1)


# ---------------------------------------------------------------------------
# Phase C C.4 integration helpers
# ---------------------------------------------------------------------------

def detect_repetition_loop(text: str) -> tuple[bool, str]:
    """Delegate to core.utils.is_likely_repetition_loop when available.

    Falls back to a minimal heuristic when the module is not importable
    (e.g. running outside the KrabEar venv).

    Returns:
        (is_loop, reason) tuple matching the core.utils signature.
    """
    try:
        # Resolve project root so the import works regardless of cwd.
        project_root = Path(__file__).resolve().parent.parent / "KrabEar"
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
        from core.utils import is_likely_repetition_loop  # type: ignore[import]
        return is_likely_repetition_loop(text)
    except ImportError:
        # Fallback: naive bigram repetition check.
        words = text.lower().split()
        if len(words) < 6:
            return (False, "")
        bigrams: dict[tuple[str, str], int] = {}
        for i in range(len(words) - 1):
            bg = (words[i], words[i + 1])
            bigrams[bg] = bigrams.get(bg, 0) + 1
        max_count = max(bigrams.values(), default=0)
        if max_count >= 5:
            return (True, f"fallback_bigram x{max_count}")
        return (False, "")


def count_brand_normalizations(text: str) -> int:
    """Count how many brand normalization substitutions would fire on `text`.

    Uses BRAND_REPLACEMENTS from core.utils when importable; otherwise zero.
    This counts substitutions that WOULD fire, not actual output changes.
    """
    try:
        project_root = Path(__file__).resolve().parent.parent / "KrabEar"
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
        from core.utils import BRAND_REPLACEMENTS  # type: ignore[import]
        count = 0
        for pattern, _ in BRAND_REPLACEMENTS:
            if pattern.search(text):
                count += 1
        return count
    except ImportError:
        return 0


# ---------------------------------------------------------------------------
# Mock adapter — used when real engine adapters are not loadable.
# ---------------------------------------------------------------------------

class MockAdapter:
    """Returns a stable mock output for framework testing without loading models."""

    def __init__(self, engine_name: str) -> None:
        self.engine_name = engine_name

    def transcribe(self, audio_path: str, lang: str = "ru") -> str:
        return f"[MOCK:{self.engine_name}] no real model loaded"


def _load_adapter(engine_name: str) -> Any:
    """Attempt to load a real engine adapter; fall back to MockAdapter.

    Real adapters are expected in backend.gigaam_worker / core.engine — they
    require models to be present in the HuggingFace cache.
    """
    return MockAdapter(engine_name)


# ---------------------------------------------------------------------------
# Core benchmark runner
# ---------------------------------------------------------------------------

def run_bench(
    samples: list[dict[str, str]],
    engine_names: list[str],
    use_mock: bool = True,
) -> list[dict[str, Any]]:
    """Run all samples x engines and return result dicts.

    Args:
        samples: list of sample dicts (audio, reference, lang, domain).
        engine_names: list of engine identifier strings.
        use_mock: when True always uses MockAdapter (default).

    Returns:
        List of result dicts, one per (engine, sample) combination.
    """
    results: list[dict[str, Any]] = []

    for engine_name in engine_names:
        adapter = _load_adapter(engine_name) if use_mock else _load_adapter(engine_name)

        for sample in samples:
            audio_path = sample["audio"]
            reference = sample.get("reference", "")
            lang = sample.get("lang", "ru")
            domain = sample.get("domain", "unknown")

            t0 = time.monotonic()
            hypothesis = adapter.transcribe(audio_path, lang=lang)
            elapsed_ms = int((time.monotonic() - t0) * 1000)

            wer = compute_wer(reference, hypothesis)
            cer = compute_cer(reference, hypothesis)
            is_loop, loop_reason = detect_repetition_loop(hypothesis)
            brand_hits = count_brand_normalizations(hypothesis)

            results.append(
                {
                    "engine": engine_name,
                    "sample": audio_path,
                    "domain": domain,
                    "lang": lang,
                    "reference": reference,
                    "hypothesis": hypothesis,
                    "wer": round(wer, 4),
                    "cer": round(cer, 4),
                    "repetition_loop": is_loop,
                    "loop_reason": loop_reason,
                    "brand_norm_hits": brand_hits,
                    "latency_ms": elapsed_ms,
                    "mocked": isinstance(adapter, MockAdapter),
                }
            )

    return results


# ---------------------------------------------------------------------------
# Markdown report renderer
# ---------------------------------------------------------------------------

def render_markdown(results: list[dict[str, Any]], engine_names: list[str], samples: list[dict[str, str]]) -> str:
    lines: list[str] = [
        "# STT Engine Benchmark Report",
        "",
        f"Engines: {', '.join(engine_names)}",
        f"Samples: {len(samples)}",
        f"Total runs: {len(results)}",
        "",
        "| Engine | Sample | Domain | WER | CER | Repetition? | Brand hits | Latency ms | Mocked? |",
        "|--------|--------|--------|-----|-----|-------------|-----------|------------|---------|",
    ]
    for r in results:
        sample_name = Path(r["sample"]).name
        rep = "YES" if r["repetition_loop"] else "no"
        mocked = "yes" if r["mocked"] else "NO"
        lines.append(
            f"| {r['engine']} | {sample_name} | {r['domain']} "
            f"| {r['wer']:.2%} | {r['cer']:.2%} "
            f"| {rep} | {r['brand_norm_hits']} | {r['latency_ms']} | {mocked} |"
        )

    lines += [
        "",
        "## Per-engine summary",
        "",
        "| Engine | Avg WER | Avg CER | Rep loops | Brand hits total |",
        "|--------|---------|---------|-----------|-----------------|",
    ]
    for engine in engine_names:
        eng_rows = [r for r in results if r["engine"] == engine]
        if not eng_rows:
            continue
        avg_wer = sum(r["wer"] for r in eng_rows) / len(eng_rows)
        avg_cer = sum(r["cer"] for r in eng_rows) / len(eng_rows)
        loops = sum(1 for r in eng_rows if r["repetition_loop"])
        brands = sum(r["brand_norm_hits"] for r in eng_rows)
        lines.append(
            f"| {engine} | {avg_wer:.2%} | {avg_cer:.2%} | {loops} | {brands} |"
        )

    lines += [
        "",
        "> Generated by `scripts/stt_engine_bench.py` — Phase D prep 2026-05-05",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="STT engine benchmark (Whisper vs GigaAM vs RU fine-tune)"
    )
    ap.add_argument("--audio", help="Single audio file path")
    ap.add_argument("--reference", default="", help="Reference text for single-file mode")
    ap.add_argument("--lang", default="ru", help="Language code (default: ru)")
    ap.add_argument("--domain", default="custom", help="Domain label (default: custom)")
    ap.add_argument("--suite", default="default", help="Test suite name (default: default)")
    ap.add_argument(
        "--engines",
        default="gigaam-rnnt,whisper-mlx,whisper-ru-finetune",
        help="Comma-separated engine names",
    )
    ap.add_argument("--json", action="store_true", help="Output JSON instead of Markdown")
    ap.add_argument(
        "--no-mock",
        action="store_true",
        help="Attempt to load real adapters (requires model cache)",
    )
    args = ap.parse_args()

    if args.audio:
        samples: list[dict[str, str]] = [
            {
                "audio": args.audio,
                "reference": args.reference,
                "lang": args.lang,
                "domain": args.domain,
            }
        ]
    else:
        samples = DEFAULT_SAMPLES

    engine_names = [e.strip() for e in args.engines.split(",") if e.strip()]
    use_mock = not args.no_mock

    results = run_bench(samples, engine_names, use_mock=use_mock)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(results, engine_names, samples))


if __name__ == "__main__":
    main()
