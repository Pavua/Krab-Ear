#!/usr/bin/env python3
"""
Скрипт для замера качества распознавания (WER).
Часть фазы EAR-2 (Quality and Observability).
"""

import requests
import json
import time
import jiwer
from pathlib import Path

# Конфигурация
API_URL = "http://127.0.0.1:5005/v1/stt/transcribe"
DATASET_PATH = Path("KrabEar/tests/golden_dataset")
RESULTS_PATH = Path("KrabEar/tests/benchmark_results.json")


def run_benchmark():
    if not DATASET_PATH.exists():
        print(f"Dataset directory not found: {DATASET_PATH}")
        return

    mapping_file = DATASET_PATH / "mapping.json"
    if not mapping_file.exists():
        print(f"Mapping file not found: {mapping_file}")
        return

    with open(mapping_file, "r") as f:
        dataset = json.load(f)

    results = []
    refs = []
    hyps = []

    print(f"Starting benchmark on {len(dataset)} items...")

    for item in dataset:
        audio_file = DATASET_PATH / item["file"]
        reference = item["text"]

        if not audio_file.exists():
            print(f"File not found: {audio_file}")
            continue

        print(f"Processing {item['file']}...", end="", flush=True)

        start = time.monotonic()
        with open(audio_file, "rb") as f:
            response = requests.post(API_URL, files={"file": f}, data={"chat_id": "benchmark", "message_id": f"bench_{time.time()}"})

        elapsed = time.monotonic() - start

        if response.status_code == 200:
            res_json = response.json()
            hypothesis = res_json.get("text", "")
            confidence = res_json.get("confidence", 0.0)

            results.append({
                "file": item["file"],
                "ref": reference,
                "hyp": hypothesis,
                "wer": jiwer.wer(reference, hypothesis),
                "confidence": confidence,
                "latency_sec": elapsed
            })

            refs.append(reference)
            hyps.append(hypothesis)
            print(f" DONE (WER: {results[-1]['wer']:.2f}, Latency: {elapsed:.2f}s)")
        else:
            print(f" FAILED (Status: {response.status_code})")

    if not results:
        print("No results to report.")
        return

    # Итоговые метрики
    avg_wer = jiwer.wer(refs, hyps)
    avg_latency = sum(r["latency_sec"] for r in results) / len(results)
    avg_confidence = sum(r["confidence"] for r in results) / len(results)

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "metrics": {
            "avg_wer": round(avg_wer, 4),
            "avg_latency_sec": round(avg_latency, 3),
            "avg_confidence": round(avg_confidence, 3),
            "total_items": len(results)
        },
        "details": results
    }

    with open(RESULTS_PATH, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 40)
    print("BENCHMARK REPORT")
    print("=" * 40)
    print(f"Average WER: {avg_wer:.2%}")
    print(f"Average Latency: {avg_latency:.2f}s")
    print(f"Average Confidence: {avg_confidence:.2%}")
    print(f"Results saved to: {RESULTS_PATH}")
    print("=" * 40)


if __name__ == "__main__":
    run_benchmark()
