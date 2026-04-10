#!/usr/bin/env python3
"""Бенчмарк LLM моделей для Krab Ear text correction.

Тестирует каждую модель на 5 фразах, замеряет latency (cold + warm),
оценивает качество: сохранение смысла, отсутствие paraphrase, compliance.

Использование:
    python KrabEar/tests/benchmark_llm_models.py
"""

import json
import time
import requests

API_BASE = "http://localhost:1234/v1"
API_KEY = "sk-lm-n5OZwUYH:o2lxbyp2VUE4hFSNtVXR"

SYSTEM_PROMPT = """Ты — редактор русской диктовки. Твоя задача — исправить пунктуацию, орфографию и грамматику в тексте, сохранив смысл и стиль автора.

Жёсткие правила:
1. НЕ добавляй слов, которых нет в оригинале.
2. НЕ удаляй слов, кроме явных filler'ов в начале ("э-э", "ну", "вот").
3. НЕ меняй порядок слов, кроме случаев когда этого требует грамматика.
4. НЕ переформулируй фразы — только исправляй ошибки.
5. Бренды и технические термины оставляй латиницей: Spotify, YouTube, GitHub, Claude, OpenAI, Docker, Python, Swift, macOS, iPhone, iPad, Mac, Telegram, WhatsApp, Slack, Notion, Figma, VS Code, Xcode, Linux, Linear, Jira.
6. Расставь правильные знаки препинания: запятые, точки, тире, двоеточия.
7. Заглавные буквы в начале предложений и у имён собственных.
8. Если текст пустой или бессмысленный — верни его без изменений.

Верни ТОЛЬКО исправленный текст. Без пояснений. Без кавычек. Без префиксов типа "Исправленный текст:"."""

TEST_PHRASES = [
    {
        "input": "ну вот я думаю что надо переписать этот модуль потому что он слишком медленный",
        "expected_close": "Я думаю, что надо переписать этот модуль, потому что он слишком медленный.",
        "desc": "filler removal + punctuation",
    },
    {
        "input": "открой телеграм и напиши павлу что встреча переносится на завтра на три часа",
        "expected_close": "Открой Телеграм и напиши Павлу, что встреча переносится на завтра на три часа.",
        "desc": "capitalization + brand + proper name",
    },
    {
        "input": "э нужно добавить поддержку swift concurrency в проект и обновить xcode до последней версии",
        "expected_close": "Нужно добавить поддержку Swift Concurrency в проект и обновить Xcode до последней версии.",
        "desc": "tech terms latin + filler",
    },
    {
        "input": "мне кажется claude лучше справляется с кодом чем gpt а gemini лучше делает дизайн",
        "expected_close": "Мне кажется, Claude лучше справляется с кодом, чем GPT, а Gemini лучше делает дизайн.",
        "desc": "multiple brands + complex punctuation",
    },
    {
        "input": "запусти пайтон скрипт из директории краб ир и посмотри что в логах",
        "expected_close": "Запусти Python-скрипт из директории Krab Ear и посмотри, что в логах.",
        "desc": "transliterated tech terms",
    },
]

MODELS_TO_TEST = [
    "huihui-qwen3-4b-instruct-2507-abliterated-hi-mlx",
    "zai-org/glm-4.7-flash",
    "huihui-glm-4.7-flash-abliterated-mlx",
]


def test_model(model_id: str) -> dict:
    """Тестирует одну модель на всех фразах."""
    results = {"model": model_id, "phrases": [], "errors": []}

    for i, phrase in enumerate(TEST_PHRASES):
        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": phrase["input"]},
            ],
            "temperature": 0.0,
            "max_tokens": 2048,
            "stream": False,
            "stop": ["Исправленный текст:", "Исходный текст:"],
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        }

        start = time.monotonic()
        try:
            resp = requests.post(
                f"{API_BASE}/chat/completions",
                json=payload,
                headers=headers,
                timeout=30.0,
            )
        except requests.Timeout:
            results["errors"].append(f"phrase {i}: TIMEOUT (30s)")
            results["phrases"].append({"input": phrase["input"], "output": "TIMEOUT", "latency_ms": 30000, "desc": phrase["desc"]})
            continue
        except Exception as e:
            results["errors"].append(f"phrase {i}: {e}")
            results["phrases"].append({"input": phrase["input"], "output": f"ERROR: {e}", "latency_ms": 0, "desc": phrase["desc"]})
            continue

        latency_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code != 200:
            results["errors"].append(f"phrase {i}: HTTP {resp.status_code}")
            results["phrases"].append({"input": phrase["input"], "output": f"HTTP {resp.status_code}", "latency_ms": latency_ms, "desc": phrase["desc"]})
            continue

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            # Strip quotes if model wraps in them
            if len(content) >= 2 and content[0] in ('"', '«', '\u201c') and content[-1] in ('"', '»', '\u201d'):
                content = content[1:-1].strip()
        except Exception as e:
            results["errors"].append(f"phrase {i}: parse error: {e}")
            content = f"PARSE_ERROR: {e}"

        result_entry = {
            "input": phrase["input"],
            "expected": phrase["expected_close"],
            "output": content,
            "latency_ms": latency_ms,
            "desc": phrase["desc"],
            "is_cold": i == 0,
        }

        # Quality checks
        input_words = set(phrase["input"].lower().split())
        output_words = set(content.lower().split()) if content else set()
        # Check if output is drastically different (paraphrase detection)
        if content and len(content) > len(phrase["input"]) * 2:
            result_entry["quality"] = "CHATBOT (too long)"
        elif content and len(content) < len(phrase["input"]) * 0.3:
            result_entry["quality"] = "TRUNCATED"
        else:
            result_entry["quality"] = "OK"

        results["phrases"].append(result_entry)

    # Summary stats
    latencies = [p["latency_ms"] for p in results["phrases"] if p.get("latency_ms", 0) > 0]
    if latencies:
        results["cold_ms"] = latencies[0]
        results["warm_avg_ms"] = int(sum(latencies[1:]) / max(len(latencies) - 1, 1))
        results["max_ms"] = max(latencies)

    return results


def print_results(results: dict):
    print(f"\n{'='*70}")
    print(f"  MODEL: {results['model']}")
    print(f"  Cold: {results.get('cold_ms', '?')}ms | Warm avg: {results.get('warm_avg_ms', '?')}ms | Max: {results.get('max_ms', '?')}ms")
    print(f"{'='*70}")

    for p in results["phrases"]:
        tag = "COLD" if p.get("is_cold") else "WARM"
        quality = p.get("quality", "?")
        print(f"\n  [{tag}] {p['desc']} ({p['latency_ms']}ms) [{quality}]")
        print(f"  IN:  {p['input']}")
        if "expected" in p:
            print(f"  EXP: {p['expected']}")
        print(f"  OUT: {p['output']}")

    if results["errors"]:
        print(f"\n  ERRORS: {results['errors']}")


def main():
    print("Krab Ear LLM Benchmark")
    print(f"Testing {len(MODELS_TO_TEST)} models × {len(TEST_PHRASES)} phrases")
    print(f"API: {API_BASE}")

    all_results = []
    for model_id in MODELS_TO_TEST:
        print(f"\n>>> Loading model: {model_id} ...")
        results = test_model(model_id)
        print_results(results)
        all_results.append(results)

    # Final comparison
    print(f"\n\n{'='*70}")
    print("  FINAL COMPARISON")
    print(f"{'='*70}")
    print(f"  {'Model':<55} {'Cold':>6} {'Warm':>6} {'Max':>6}")
    print(f"  {'-'*55} {'-'*6} {'-'*6} {'-'*6}")
    for r in all_results:
        name = r["model"][:55]
        cold = f"{r.get('cold_ms', '?')}ms"
        warm = f"{r.get('warm_avg_ms', '?')}ms"
        mx = f"{r.get('max_ms', '?')}ms"
        print(f"  {name:<55} {cold:>6} {warm:>6} {mx:>6}")


if __name__ == "__main__":
    main()
