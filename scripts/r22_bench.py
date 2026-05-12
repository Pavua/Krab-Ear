#!/usr/bin/env python3
"""R22 LLM Rewriter Benchmark — Krab Ear
Тестирует 3 модели (baseline + 2 candidates) на 5 русских транскриптах.
Candidates: supergemma4-26b-abliterated-multimodal-mlx (check warm-cache pure text perf)
            gemma-4-e2b-it-ultra-uncensored-heretic-mlx-int8-affine (check internal token dump again)
Output: docs/llm-bench-results-R22.md
"""

from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LM_STUDIO_URL = "http://localhost:1234/v1"
def _load_lm_studio_token() -> str:
    """Read LM Studio token from env / Krab/.env (Wave 47 CRIT-1 fix)."""
    import os
    import re
    from pathlib import Path
    env_val = os.environ.get("LM_STUDIO_TOKEN", "").strip()
    if env_val:
        return env_val
    env_file = Path.home() / "Antigravity_AGENTS" / "Краб" / ".env"
    if env_file.exists():
        match = re.search(r"sk-lm-[A-Za-z0-9:_-]+", env_file.read_text())
        if match:
            return match.group(0)
    raise RuntimeError("LM_STUDIO_TOKEN not found. Set env var or add to Krab/.env")


LM_STUDIO_TOKEN = _load_lm_studio_token()
TIMEOUT_SEC = 600  # generous cold-load tolerance (urllib uses int seconds)
WARMUP_RUNS = 1    # warm-up calls before timed runs
TIMED_RUNS = 2     # timed calls per model per prompt (simple pattern, like R19-simple)
INTER_MODEL_SLEEP = 75  # seconds between models for TTL eviction

BASELINE = "gemma-4-26b-a4b-it-optiq"

MODELS = [
    # baseline
    BASELINE,
    # candidate 1: multimodal — check if warm-cache removes vision-encoder overhead for pure text
    "supergemma4-26b-abliterated-multimodal-mlx",
    # candidate 2: E2B-heretic — recheck internal token dump
    "gemma-4-e2b-it-ultra-uncensored-heretic-mlx-int8-affine",
]

# ---------------------------------------------------------------------------
# Rewriter system prompt (exact copy from llm_rewriter.py)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """Ты — редактор диктовки. Твоя задача — исправить пунктуацию, орфографию и грамматику, сохранив смысл и стиль автора.

Жёсткие правила:
1. НЕ добавляй слов, которых нет в оригинале.
2. НЕ удаляй слов, кроме (а) явных filler'ов в начале ("э-э", "ну", "вот") и (б) немедленных повторов от re-articulation: если человек переспрашивает слово сразу же ("записываю уже, уже" → "записываю уже"; "слово, слово" → "слово"; "вот сейчас, вот сейчас" → "вот сейчас"), оставляй ОДНО вхождение. Не путай с риторическими повторами и emphasis ("очень очень важно" → оставь как есть).
3. НЕ меняй порядок слов, кроме случаев когда этого требует грамматика.
4. НЕ переформулируй фразы — только исправляй ошибки.
5. СОХРАНЯЙ язык ввода — НЕ переводи между языками. Если вход на испанском — выход на испанском. Если на английском — на английском.
6. Бренды и технические термины оставляй латиницей: Spotify, YouTube, GitHub, Claude, OpenAI, Docker, Python, Swift, macOS, iPhone, iPad, Mac, Telegram, WhatsApp, Slack, Notion, Figma, VS Code, Xcode, Linux, Linear, Jira, Qwen, MLX, GigaAM, Krab Ear.
7. Расставь правильные знаки препинания: запятые, точки, тире, двоеточия.
8. Заглавные буквы в начале предложений и у имён собственных.
9. Если текст пустой или бессмысленный — верни его без изменений.
10. Не используй <think> теги или reasoning chains.

Верни ТОЛЬКО исправленный текст. Без пояснений. Без кавычек. Без префиксов типа "Исправленный текст:"."""

# ---------------------------------------------------------------------------
# Test prompts — 5 типовых русских STT транскриптов с типичными ошибками
# ---------------------------------------------------------------------------
PROMPTS = [
    {
        "name": "meeting_150w",
        "label": "Meeting transcript ~150 words",
        "text": (
            "ну значит сегодня встреча по проекту краб ир я хочу обсудить три вещи "
            "первое это интеграция с питоном второе развертывание на маки и третье "
            "у нас есть проблема с е е авторизациeй через опенаи ключ не работает "
            "стас ты можешь посмотреть это до конца недели да я думаю думаю это "
            "займет часа два максимум три хорошо а по поводу документации кто берет "
            "на себя Паша ты можешь написать технический раздел я напишу я напишу "
            "окей отлично следующая встреча в пятницу в два часа дня не забудьте "
            "обновить трело до встречи прикрепите скрины из xcode и гитхаб если есть "
            "вопросы пишите в слак канал разработки всем спасибо за внимание"
        ),
    },
    {
        "name": "phone_call_mat",
        "label": "Phone call ~100 words with mat",
        "text": (
            "алло ну блять опять не слышно подожди подожди я перезвоню нет нет "
            "слышу слышу говори ну короче там ситуация такая ё-моё заказ снова "
            "задержали на складе в швейцарии не в швеции в швейцарии понял "
            "так значит что делаем я звоню логистам прямо сейчас или ты "
            "блин я же говорил что нельзя через финляндию везти надо было "
            "через польшу ладно хватит ругаться что делаем звони им прямо "
            "сейчас скажи что мы теряем деньги каждый день ок понял позвоню"
        ),
    },
    {
        "name": "dictation_note",
        "label": "Dictation note ~80 words",
        "text": (
            "заметка на завтра нужно купить кофе молоко и хлеб потом позвонить "
            "врачу записаться на приём на следующей неделе ещо ещо надо проверить "
            "договор с арендодателем там есть пункт про ё и е путаница "
            "в правописании фамилии петрёв или петрев уточнить у него лично "
            "и да не забыть оплатить интернет до пятницы иначе отключат"
        ),
    },
    {
        "name": "tech_discussion",
        "label": "Technical discussion ~120 words (AI/programming)",
        "text": (
            "значит смотри мы используем млх вискер для транскрипции но есть проблема "
            "когда два потока обращаются к видеокарте одновременно мы получаем сигсегв "
            "потому что Эм Эл Икс не потокобезопасен нужен глобальный лок "
            "я добавил мтекс в кор слеш млх_лок.пай и оборачиваю все вызовы "
            "в контекст менеджер это должно решить проблему также хочу попробовать "
            "гигаам для русских транскриптов он лучше распознаёт мат и разговорную речь "
            "мы тестировали на реальном звонке скорость похожая но качество лучше "
            "особенно для слов типа швейцария финляндия которые вискер галлюцинирует"
        ),
    },
    {
        "name": "mixed_ru_en",
        "label": "Mixed Russian/English ~100 words",
        "text": (
            "окей давай обсудим деплой на прод я уже пушнул изменения в гитхаб "
            "ветка фиче слеш рефактор логгера пул реквест открыт можешь порявьювить "
            "там я поменял формат логов на джейсон как мы договаривались "
            "ещё добавил метрики через дата дог агент настроен на порт восемь тысяч "
            "и двадцать пять кстати у нас в слаке было сообщение что хуки в гитхаб "
            "экшнс упали на джоб билд свифт нужно проверить xcode настройки "
            "там может быть проблема с сертификатом ладно погнали"
        ),
    },
]

# ---------------------------------------------------------------------------
# Chatbot markers (same as llm_rewriter.py)
# ---------------------------------------------------------------------------
CHATBOT_MARKERS = (
    "извините", "пожалуйста, укажите", "пожалуйста, предоставьте",
    "как я могу", "чем могу помочь", "к сожалению", "я не могу",
    "i'm sorry", "i apologize", "here is", "sure,",
    "конечно,", "вот исправленный",
)

# ---------------------------------------------------------------------------
# LM Studio API helpers — stdlib urllib only (no requests)
# ---------------------------------------------------------------------------

def get_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LM_STUDIO_TOKEN}",
    }


def verify_token() -> None:
    """Fail loud if LM Studio rejects the token."""
    req = urllib.request.Request(
        f"{LM_STUDIO_URL}/models",
        headers=get_headers(),
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            count = len(data.get("data", []))
            print(f"[OK] LM Studio reachable, token valid. {count} models available.")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print("FATAL: LM Studio rejected the API token (HTTP 401). Aborting.", file=sys.stderr)
        else:
            print(f"FATAL: LM Studio /v1/models returned HTTP {exc.code}. Aborting.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"FATAL: Cannot reach LM Studio: {exc}", file=sys.stderr)
        sys.exit(1)


def call_model(model: str, text: str) -> tuple[Optional[str], int, str]:
    """Returns (output_text_or_None, latency_ms, failure_reason).
    Never raises.
    NOTE: no response_format param — LM Studio API changed, it breaks baseline calls.
    """
    word_count = len(text.split())
    max_tokens = max(256, min(int(word_count * 3 * 1.3) + 50, 4096))
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream": False,
        "stop": [
            "Исправленный текст:", "Исходный текст:",
            "<end_of_turn>", "<start_of_turn>", "</s>",
        ],
        "tool_choice": "none",
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{LM_STUDIO_URL}/chat/completions",
        data=body,
        headers=get_headers(),
        method="POST",
    )

    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        if exc.code == 401:
            print("FATAL: LM Studio returned 401 mid-bench. Token revoked?", file=sys.stderr)
            sys.exit(1)
        return None, latency_ms, f"http_{exc.code}"
    except Exception as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        exc_type = type(exc).__name__
        if "timeout" in exc_type.lower() or "Timeout" in str(exc):
            return None, latency_ms, "timeout"
        if "canceled" in str(exc).lower() or "cancelled" in str(exc).lower():
            return None, latency_ms, "operation_canceled"
        return None, latency_ms, f"error:{exc_type}"

    latency_ms = int((time.monotonic() - start) * 1000)

    try:
        data = json.loads(raw)
        content = data["choices"][0]["message"].get("content") or ""
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        return None, latency_ms, f"parse_error:{exc}"

    # postprocess: strip quotes and explanatory prefixes
    s = content.strip()
    if len(s) >= 2 and s[0] in ('"', "«", "“") and s[-1] in ('"', "»", "”"):
        s = s[1:-1].strip()
    for prefix in ("Исправленный текст:", "Исправлено:", "Результат:", "Вот:"):
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix):].strip()
            break
    if "\n\n" in s:
        s = s.split("\n\n", 1)[0].strip()

    if not s:
        return None, latency_ms, "empty_response"

    return s, latency_ms, "ok"


def call_model_with_retry(model: str, text: str) -> tuple[Optional[str], int, str]:
    """Per Wave 43-44 lessons: if 'operation_canceled', wait 60s and retry once."""
    out, lat, reason = call_model(model, text)
    if reason == "operation_canceled":
        print(f"\n    [WARN] operation_canceled — waiting 60s then retrying...", flush=True)
        time.sleep(60)
        out, lat2, reason = call_model(model, text)
        lat = lat + lat2  # report total wait
    return out, lat, reason


# ---------------------------------------------------------------------------
# Quality heuristics
# ---------------------------------------------------------------------------

def score_quality(input_text: str, output: Optional[str], reason: str) -> tuple[float, list[str]]:
    """Returns (0.0-1.0 score, list of issue flags)."""
    if output is None:
        return 0.0, [f"failed:{reason}"]

    issues = []
    score = 1.0

    # Check for internal token dumps (key E2B-heretic failure mode from R20)
    internal_token_markers = ["<|", "|>", "<bos>", "<eos>", "▁", "<unused", "<pad>"]
    token_dump_count = sum(output.count(m) for m in internal_token_markers)
    if token_dump_count > 3:
        score -= 0.5
        issues.append(f"internal_token_dump(count={token_dump_count})")

    # Length ratio: ideal 0.7–1.3
    in_len = len(input_text)
    out_len = len(output)
    ratio = out_len / in_len if in_len > 0 else 0.0
    if ratio < 0.5:
        score -= 0.4
        issues.append(f"too_short({ratio:.2f})")
    elif ratio < 0.7:
        score -= 0.15
        issues.append(f"short({ratio:.2f})")
    elif ratio > 2.0:
        score -= 0.3
        issues.append(f"too_long({ratio:.2f})")
    elif ratio > 1.3:
        score -= 0.1
        issues.append(f"long({ratio:.2f})")

    # Chatbot leak check
    lower = output.lower()
    for marker in CHATBOT_MARKERS:
        if lower.startswith(marker):
            score -= 0.5
            issues.append(f"chatbot_marker:{marker[:20]}")
            break

    # Mat preservation: if input has mat words, output should too
    mat_words = ["блять", "блин", "ё-моё", "ёмоё", "хватит"]
    for w in mat_words:
        if w in input_text.lower() and w not in output.lower():
            score -= 0.05
            issues.append(f"mat_dropped:{w}")

    # <think> tag leak (reasoning model bleed)
    if "<think>" in output or "</think>" in output:
        score -= 0.3
        issues.append("think_tag_leak")

    # Language preserved: input is Russian, output should have Cyrillic
    cyrillic_in_output = sum(1 for c in output if "Ѐ" <= c <= "ӿ")
    if cyrillic_in_output < 5:
        score -= 0.4
        issues.append("no_cyrillic")

    return max(0.0, min(1.0, score)), issues


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def benchmark_model(model: str) -> list[dict]:
    """Run all prompts for one model. Returns list of result dicts."""
    print(f"\n{'='*60}")
    print(f"  MODEL: {model}")
    print(f"{'='*60}")
    results = []

    for prompt in PROMPTS:
        print(f"  Prompt: {prompt['name']} ... ", end="", flush=True)

        # Warm-up runs (not timed) — crucial for multimodal to load vision encoder
        print("(warmup) ", end="", flush=True)
        for _ in range(WARMUP_RUNS):
            call_model_with_retry(model, prompt["text"])

        # Timed runs
        latencies = []
        outputs = []
        reasons = []
        for i in range(TIMED_RUNS):
            out, lat, reason = call_model_with_retry(model, prompt["text"])
            latencies.append(lat)
            outputs.append(out)
            reasons.append(reason)
            print(f"run{i+1}={lat}ms ", end="", flush=True)

        # Use last successful output for quality scoring
        final_output = next((o for o in reversed(outputs) if o is not None), None)
        final_reason = reasons[-1]

        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) > 1 else latencies[0]
        quality, issues = score_quality(prompt["text"], final_output, final_reason)
        ok_count = sum(1 for r in reasons if r == "ok")

        print(f"-> p50={int(p50)}ms quality={quality:.2f} ok={ok_count}/{TIMED_RUNS}")
        if issues:
            print(f"    issues: {', '.join(issues)}")
        if final_output:
            preview = final_output[:120].replace("\n", " ")
            print(f"    output: {preview}...")

        results.append({
            "model": model,
            "prompt": prompt["name"],
            "prompt_label": prompt["label"],
            "latencies_ms": latencies,
            "p50_ms": p50,
            "p95_ms": p95,
            "quality_score": quality,
            "issues": issues,
            "ok_count": ok_count,
            "total_runs": TIMED_RUNS,
            "final_output": final_output,
            "final_reason": final_reason,
        })

    return results


# ---------------------------------------------------------------------------
# Aggregate stats per model
# ---------------------------------------------------------------------------

def aggregate(results: list[dict]) -> dict:
    """Aggregate per-prompt results into per-model summary."""
    by_model: dict[str, list] = {}
    for r in results:
        by_model.setdefault(r["model"], []).append(r)

    summary = {}
    for model, rows in by_model.items():
        all_lats = [lat for row in rows for lat in row["latencies_ms"]]
        all_q = [row["quality_score"] for row in rows]
        total_ok = sum(row["ok_count"] for row in rows)
        total_runs = sum(row["total_runs"] for row in rows)
        json_valid_ratio = total_ok / total_runs if total_runs > 0 else 0.0

        summary[model] = {
            "latency_p50_ms": int(statistics.median(all_lats)),
            "latency_p95_ms": int(sorted(all_lats)[int(len(all_lats) * 0.95)]),
            "quality_avg": sum(all_q) / len(all_q),
            "json_valid_ratio": json_valid_ratio,
            "rows": rows,
        }
    return summary


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def build_markdown(summary: dict, all_results: list[dict]) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        "# LLM Rewriter Benchmark R22 — Krab Ear",
        "",
        f"**Date:** {ts}  ",
        f"**Hardware:** MacBook Pro M4 Max, 36 GB RAM  ",
        f"**LM Studio:** {LM_STUDIO_URL}  ",
        f"**Runs:** {WARMUP_RUNS} warmup + {TIMED_RUNS} timed × {len(PROMPTS)} prompts  ",
        f"**Inter-model sleep:** {INTER_MODEL_SLEEP}s (TTL eviction)  ",
        "",
        "## Context",
        "",
        "- R19 winner: `gemma-4-26b-a4b-it-optiq` (1587ms p50, quality 1.00)",
        "- R20: supergemma4-26b-multimodal showed 4808ms p50 (3× slower) due to vision-encoder overhead",
        "- R20: E2B-heretic showed internal token dumps — persistent or one-off?",
        "- R22 goal: recheck both with warm-cache runs (1 warmup before timed)",
        "",
        "## Summary Table",
        "",
        "| Model | Role | latency_p50_ms | latency_p95_ms | quality_avg | ok_ratio |",
        "|-------|------|---------------|---------------|-------------|----------|",
    ]

    # Sort by p50 latency
    ordered = sorted(summary.items(), key=lambda x: x[1]["latency_p50_ms"])
    for model, stats in ordered:
        role = "**baseline**" if model == BASELINE else "candidate"
        lines.append(
            f"| `{model}` | {role} "
            f"| {stats['latency_p50_ms']} "
            f"| {stats['latency_p95_ms']} "
            f"| {stats['quality_avg']:.3f} "
            f"| {stats['json_valid_ratio']:.2f} |"
        )

    best_latency_model = min(summary, key=lambda m: summary[m]["latency_p50_ms"])
    best_quality_model = max(summary, key=lambda m: summary[m]["quality_avg"])
    baseline_stats = summary.get(BASELINE, {})

    lines += [
        "",
        "## Analysis",
        "",
        f"- **Fastest (p50):** `{best_latency_model}` — {summary[best_latency_model]['latency_p50_ms']}ms",
        f"- **Best quality:** `{best_quality_model}` — {summary[best_quality_model]['quality_avg']:.3f}",
    ]
    if baseline_stats:
        lines.append(
            f"- **Baseline:** `{BASELINE}` — p50={baseline_stats['latency_p50_ms']}ms, "
            f"quality={baseline_stats['quality_avg']:.3f}"
        )

    lines += [
        "",
        "## Per-Prompt Detail",
        "",
    ]

    for model, stats in ordered:
        lines.append(f"### `{model}`")
        lines.append("")
        lines.append("| Prompt | p50_ms | p95_ms | quality | ok/runs | issues |")
        lines.append("|--------|--------|--------|---------|---------|--------|")
        for row in stats["rows"]:
            issues_str = ", ".join(row["issues"]) if row["issues"] else "—"
            lines.append(
                f"| {row['prompt']} "
                f"| {int(row['p50_ms'])} "
                f"| {int(row['p95_ms'])} "
                f"| {row['quality_score']:.2f} "
                f"| {row['ok_count']}/{row['total_runs']} "
                f"| {issues_str} |"
            )
        lines.append("")
        # Show sample output for first prompt
        first_row = stats["rows"][0] if stats["rows"] else None
        if first_row and first_row.get("final_output"):
            preview = first_row["final_output"][:200].replace("\n", " ")
            lines.append(f"**Sample output (meeting_150w):** {preview}")
            lines.append("")

    # Verdict section
    lines += [
        "## Verdict",
        "",
    ]

    multimodal_model = "supergemma4-26b-abliterated-multimodal-mlx"
    heretic_model = "gemma-4-e2b-it-ultra-uncensored-heretic-mlx-int8-affine"

    if multimodal_model in summary:
        mm = summary[multimodal_model]
        base_p50 = baseline_stats.get("latency_p50_ms", 0) if baseline_stats else 0
        mm_p50 = mm["latency_p50_ms"]
        overhead_pct = ((mm_p50 - base_p50) / base_p50 * 100) if base_p50 > 0 else 0
        if mm_p50 <= base_p50 * 1.15:
            mm_verdict = (
                f"**VIABLE for pure text** — warm-cache eliminated vision-encoder overhead. "
                f"p50={mm_p50}ms vs baseline {base_p50}ms ({overhead_pct:+.0f}%). "
                f"Could be used for multimodal flows (voice+image) without pure-text penalty."
            )
        elif mm_p50 <= base_p50 * 1.5:
            mm_verdict = (
                f"**MARGINAL** — some overhead remains but reduced. "
                f"p50={mm_p50}ms vs baseline {base_p50}ms ({overhead_pct:+.0f}%). "
                f"Acceptable for multimodal flows, not recommended for pure-text-only pipeline."
            )
        else:
            mm_verdict = (
                f"**NOT VIABLE for pure text** — vision-encoder overhead persists even with warm cache. "
                f"p50={mm_p50}ms vs baseline {base_p50}ms ({overhead_pct:+.0f}%). "
                f"Reserve for multimodal flows only; baseline remains winner for text rewriting."
            )
        lines += [
            f"### `{multimodal_model}`",
            "",
            mm_verdict,
            "",
        ]

    if heretic_model in summary:
        h = summary[heretic_model]
        h_issues = [issue for row in h["rows"] for issue in row["issues"]]
        has_token_dump = any("internal_token_dump" in i for i in h_issues)
        if has_token_dump:
            heretic_verdict = (
                f"**STILL BROKEN** — internal token dumps confirmed persistent (not one-off from R20). "
                f"quality={h['quality_avg']:.3f}. Do not use for production rewriting."
            )
        elif h["quality_avg"] < 0.7:
            heretic_verdict = (
                f"**LOW QUALITY** — no token dumps but quality degraded ({h['quality_avg']:.3f}). "
                f"Not recommended over baseline."
            )
        else:
            heretic_verdict = (
                f"**FIXED** — internal token dump issue resolved. "
                f"quality={h['quality_avg']:.3f}, p50={h['latency_p50_ms']}ms. "
                f"Compare vs baseline for switch recommendation."
            )
        lines += [
            f"### `{heretic_model}`",
            "",
            heretic_verdict,
            "",
        ]

    # Overall switch recommendation
    best_overall = max(
        summary,
        key=lambda m: summary[m]["quality_avg"] - (summary[m]["latency_p50_ms"] / 10000)
    )
    lines += ["### Switch Recommendation", ""]

    if best_overall == BASELINE:
        lines.append(
            f"> **HOLD** — baseline `{BASELINE}` remains the best option. "
            f"p50={baseline_stats.get('latency_p50_ms', '?')}ms, "
            f"quality={baseline_stats.get('quality_avg', 0):.3f}. No switch recommended."
        )
    else:
        bq = summary[best_overall]["quality_avg"]
        bl = summary[best_overall]["latency_p50_ms"]
        bq_base = baseline_stats.get("quality_avg", 0)
        bl_base = baseline_stats.get("latency_p50_ms", 0)
        delta_q = (bq - bq_base) * 100
        delta_l = bl_base - bl
        lines.append(
            f"> **UPGRADE** to `{best_overall}` — "
            f"quality +{delta_q:.1f}pp vs baseline, "
            f"latency {'−' if delta_l > 0 else '+'}{abs(delta_l)}ms. "
            f"Set `lm_rewriter_model` in Krab Ear settings."
        )

    # Raw JSON appendix
    lines += [
        "",
        "## Raw JSON",
        "",
        "```json",
        json.dumps(
            {m: {k: v for k, v in s.items() if k != "rows"} for m, s in summary.items()},
            indent=2,
            ensure_ascii=False,
        ),
        "```",
    ]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("R22 LLM Rewriter Benchmark — Krab Ear")
    print(f"Models: {len(MODELS)} | Prompts: {len(PROMPTS)} | Runs: {WARMUP_RUNS}+{TIMED_RUNS}")
    print(f"Inter-model sleep: {INTER_MODEL_SLEEP}s")
    print()

    verify_token()

    all_results: list[dict] = []
    for i, model in enumerate(MODELS):
        if i > 0:
            print(f"\n[Sleeping {INTER_MODEL_SLEEP}s for TTL eviction before next model...]")
            time.sleep(INTER_MODEL_SLEEP)

        results = benchmark_model(model)
        all_results.extend(results)

    summary = aggregate(all_results)

    # Write markdown report
    repo_root = Path(__file__).parent.parent
    out_path = repo_root / "docs" / "llm-bench-results-R22.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md = build_markdown(summary, all_results)
    out_path.write_text(md, encoding="utf-8")
    print(f"\n{'='*60}")
    print(f"Report saved: {out_path}")
    print(f"{'='*60}")

    # Print summary table to stdout
    print()
    ordered = sorted(summary.items(), key=lambda x: x[1]["latency_p50_ms"])
    print(f"{'Model':<55} {'p50_ms':>8} {'quality':>8} {'ok_ratio':>9}")
    print("-" * 85)
    for model, stats in ordered:
        tag = " ← BASELINE" if model == BASELINE else ""
        print(
            f"{model:<55} {stats['latency_p50_ms']:>8} "
            f"{stats['quality_avg']:>8.3f} {stats['json_valid_ratio']:>9.2f}{tag}"
        )


if __name__ == "__main__":
    main()
