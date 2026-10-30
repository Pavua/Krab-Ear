#!/usr/bin/env python3
"""WER-оценка Golden Set: REST STT против эталонов из r2-scenario.md.

Пример:
    python3 scripts/wer_eval.py
    python3 scripts/wer_eval.py --lang ru --limit 5 --json-out /tmp/wer.json

Без jiwer; эталоны — parse_scenario из record_golden.py; WER — compute_wer
из stt_engine_bench.py. Нормализация — TextUtils.normalize_phrase (без MLX).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = REPO_ROOT / "scripts"
_KRAB = REPO_ROOT / "KrabEar"
for _p in (_SCRIPTS, _KRAB):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.utils import TextUtils  # noqa: E402
from record_golden import DEFAULT_OUT, SCENARIO_PATH, Phrase, parse_scenario  # noqa: E402
from stt_engine_bench import compute_wer  # noqa: E402

DEFAULT_BASE_URL = "http://127.0.0.1:5005"
REST_TIMEOUT_SEC = 120
_LANG_PREFIX = {"ru": "ru_", "es": "es_", "en": "en_"}


class WerEvalError(Exception):
    """Базовая ошибка харнесса."""


class MissingReferenceError(WerEvalError):
    """WAV без строки в сценарии."""


class RestPrivacyError(WerEvalError):
    """REST вернул privacy_mode (403)."""


class RestUnavailableError(WerEvalError):
    """REST недоступен или неожиданный ответ."""


def normalize_for_wer(text: str) -> str:
    return TextUtils.normalize_phrase(text)


def build_reference_map(phrases: list[Phrase]) -> dict[str, str]:
    return {p.phrase_id: p.text for p in phrases}


def reference_for_stem(stem: str, ref_map: dict[str, str]) -> str:
    if stem not in ref_map:
        raise MissingReferenceError(
            f"Нет эталона для «{stem}» — проверьте docs/golden/r2-scenario.md и имя файла."
        )
    return ref_map[stem]


def _word_tokens(text: str) -> list[str]:
    return text.split() if text else []


def wer_detail(reference: str, hypothesis: str) -> dict[str, Any]:
    """WER и число подстановок/удалений/вставок (после нормализации)."""
    ref_words = _word_tokens(reference)
    hyp_words = _word_tokens(hypothesis)
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

    subs = dels = ins = 0
    i, j = m, n
    while i > 0 or j > 0:
        if i > 0 and j > 0 and ref_words[i - 1] == hyp_words[j - 1]:
            i -= 1
            j -= 1
        elif i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + 1:
            subs += 1
            i -= 1
            j -= 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            dels += 1
            i -= 1
        else:
            ins += 1
            j -= 1

    wer = compute_wer(reference, hypothesis)
    return {
        "wer": wer,
        "substitutions": subs,
        "deletions": dels,
        "insertions": ins,
        "ref_words": max(len(ref_words), 1),
    }


def evaluate_pair(reference_raw: str, hypothesis_raw: str) -> dict[str, Any]:
    ref_n = normalize_for_wer(reference_raw)
    hyp_n = normalize_for_wer(hypothesis_raw)
    detail = wer_detail(ref_n, hyp_n)
    detail["reference_norm"] = ref_n
    detail["hypothesis_norm"] = hyp_n
    detail["reference"] = reference_raw
    detail["hypothesis"] = hypothesis_raw
    return detail


def aggregate_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_lang: dict[str, list[float]] = {}
    for row in rows:
        if "error" in row:
            continue
        lang = row["lang"]
        by_lang.setdefault(lang, []).append(float(row["wer"]))

    def _mean(vals: list[float]) -> float | None:
        return sum(vals) / len(vals) if vals else None

    by_lang_agg = {
        lang: {"count": len(wers), "mean_wer": _mean(wers)}
        for lang, wers in sorted(by_lang.items())
    }
    all_wers = [w for wers in by_lang.values() for w in wers]
    return {
        "by_lang": by_lang_agg,
        "overall": {"count": len(all_wers), "mean_wer": _mean(all_wers)},
    }


def build_json_report(rows: list[dict[str, Any]], aggregate: dict[str, Any]) -> dict[str, Any]:
    return {"files": rows, "aggregate": aggregate}


def _parse_privacy_payload(resp: requests.Response) -> bool:
    try:
        body = resp.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    skipped = body.get("skipped")
    if skipped == "privacy_mode":
        return True
    if resp.status_code == 403 and body.get("ok") is False and skipped == "privacy_mode":
        return True
    return skipped == "privacy_mode"


def transcribe_wav_rest(
    wav_path: Path,
    *,
    base_url: str = DEFAULT_BASE_URL,
    lang_hint: str,
    timeout_sec: float = REST_TIMEOUT_SEC,
) -> str:
    url = base_url.rstrip("/") + "/v1/stt/transcribe"
    data = {
        "lang_hint": lang_hint,
        "diarize": "false",
        "persist_history": "false",
    }
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            with wav_path.open("rb") as fh:
                files = {"file": (wav_path.name, fh, "audio/wav")}
                resp = requests.post(url, data=data, files=files, timeout=timeout_sec)
        except requests.exceptions.RequestException as exc:
            last_err = exc
            if attempt == 0:
                continue
            raise RestUnavailableError(
                f"REST недоступен ({base_url}): {exc}. Запущен rest_server на :5005?"
            ) from exc

        if resp.status_code == 403 and _parse_privacy_payload(resp):
            raise RestPrivacyError(
                "REST вернул privacy_mode (403). Выключите privacy_mode_enabled "
                "в настройках Krab Ear на время прогона."
            )
        if resp.status_code >= 500 and attempt == 0:
            last_err = RestUnavailableError(f"REST {resp.status_code}: {resp.text[:200]}")
            continue
        if not resp.ok:
            raise RestUnavailableError(
                f"REST ошибка {resp.status_code}: {resp.text[:300]}"
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise RestUnavailableError("REST вернул не-JSON") from exc
        text = body.get("text")
        if text is None and body.get("skipped") == "privacy_mode":
            raise RestPrivacyError(
                "privacy_mode активен — выключите privacy_mode_enabled перед замером WER."
            )
        return str(text or "")
    if last_err:
        raise last_err
    raise RestUnavailableError("REST: не удалось транскрибировать после повтора")


def lang_from_stem(stem: str) -> str:
    return stem.split("_", 1)[0]


def iter_golden_wavs(golden_dir: Path, lang_filter: str) -> list[Path]:
    if not golden_dir.is_dir():
        raise WerEvalError(f"Каталог golden не найден: {golden_dir}")
    wavs = sorted(golden_dir.glob("*.wav"))
    if lang_filter == "all":
        return wavs
    prefix = _LANG_PREFIX.get(lang_filter)
    if not prefix:
        raise WerEvalError(f"Неизвестный --lang: {lang_filter}")
    return [p for p in wavs if p.stem.startswith(prefix)]


def format_table_row(stem: str, detail: dict[str, Any]) -> str:
    wer_pct = detail["wer"] * 100.0
    return (
        f"{stem:<12} {wer_pct:6.2f}%  "
        f"S={detail['substitutions']} D={detail['deletions']} I={detail['insertions']}"
    )


def run_eval(
    *,
    golden_dir: Path,
    base_url: str,
    lang_filter: str,
    limit: int | None,
    scenario_path: Path,
    transcribe_fn=transcribe_wav_rest,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    scenario_text = scenario_path.read_text(encoding="utf-8")
    ref_map = build_reference_map(parse_scenario(scenario_text))
    wavs = iter_golden_wavs(golden_dir, lang_filter)
    if limit is not None:
        wavs = wavs[:limit]

    rows: list[dict[str, Any]] = []
    for wav in wavs:
        stem = wav.stem
        row: dict[str, Any] = {"stem": stem, "lang": lang_from_stem(stem), "file": str(wav)}
        try:
            reference_raw = reference_for_stem(stem, ref_map)
            lang_hint = lang_from_stem(stem)
            hypothesis_raw = transcribe_fn(wav, base_url=base_url, lang_hint=lang_hint)
            detail = evaluate_pair(reference_raw, hypothesis_raw)
            row.update(
                {
                    "wer": detail["wer"],
                    "substitutions": detail["substitutions"],
                    "deletions": detail["deletions"],
                    "insertions": detail["insertions"],
                    "ref_words": detail["ref_words"],
                    "reference": reference_raw,
                    "hypothesis": hypothesis_raw,
                }
            )
        except WerEvalError as exc:
            row["error"] = str(exc)
        rows.append(row)
    return rows, aggregate_results(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WER Golden Set через REST STT")
    parser.add_argument(
        "--golden-dir",
        type=Path,
        default=DEFAULT_OUT,
        help="Каталог с ru_*.wav / es_*.wav / en_*.wav",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="Базовый URL REST (без /v1/...)",
    )
    parser.add_argument(
        "--lang",
        choices=("ru", "es", "en", "all"),
        default="all",
        help="Фильтр языка",
    )
    parser.add_argument("--limit", type=int, default=None, help="Макс. число файлов")
    parser.add_argument("--json-out", type=Path, default=None, help="Путь JSON-отчёта")
    parser.add_argument(
        "--scenario",
        type=Path,
        default=SCENARIO_PATH,
        help="Путь к r2-scenario.md (для тестов/override)",
    )
    args = parser.parse_args(argv)

    try:
        rows, aggregate = run_eval(
            golden_dir=args.golden_dir,
            base_url=args.base_url,
            lang_filter=args.lang,
            limit=args.limit,
            scenario_path=args.scenario,
        )
    except WerEvalError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    print(f"{'file':<12} {'WER%':>7}  ops (S/D/I)")
    print("-" * 40)
    for row in rows:
        if "error" in row:
            print(f"{row['stem']:<12}  ERROR: {row['error']}")
        else:
            print(format_table_row(row["stem"], row))

    print()
    for lang, stats in aggregate["by_lang"].items():
        if stats["mean_wer"] is not None:
            print(f"  {lang}: mean WER {stats['mean_wer'] * 100:.2f}%  (n={stats['count']})")
    ov = aggregate["overall"]
    if ov["mean_wer"] is not None:
        print(f"  ALL: mean WER {ov['mean_wer'] * 100:.2f}%  (n={ov['count']})")

    if args.json_out:
        report = build_json_report(rows, aggregate)
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON: {args.json_out}")

    errors = [r for r in rows if "error" in r]
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
