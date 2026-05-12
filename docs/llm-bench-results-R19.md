# LLM Rewriter Benchmark R19 — Krab Ear

**Date:** 2026-05-12 04:19  
**Hardware:** MacBook Pro M4 Max, 36 GB RAM  
**LM Studio:** http://localhost:1234/v1  
**Runs:** 1 warmup + 3 timed × 5 prompts  

## Summary Table

| Model | Role | latency_p50_ms | latency_p95_ms | quality_avg | json_valid_ratio |
|-------|------|---------------|---------------|-------------|-----------------|
| `mlabonne_qwen3-8b-abliterated` | candidate | 2 | 131243 | 0.000 | 0.00 |
| `gemma-4-26b-a4b-it-assistant` | candidate | 5 | 13 | 0.000 | 0.00 |
| `gemma-4-26b-a4b-it-optiq` | **baseline** | 27 | 11691 | 0.400 | 0.40 |
| `supergemma4-26b-uncensored-mlx-v2` | candidate | 8380 | 61248 | 1.000 | 0.93 |
| `huihui-glm-4.7-flash-abliterated-mlx` | candidate | 19966 | 45538 | 0.000 | 0.00 |
| `qwen3-14b-abliterated` | candidate | 24824 | 43131 | 0.160 | 1.00 |
| `qwen3.6-27b-ud-mlx` | candidate | 120301 | 123480 | 0.200 | 0.07 |

## Analysis

- **Fastest (p50):** `mlabonne_qwen3-8b-abliterated` — 2ms
- **Best quality:** `supergemma4-26b-uncensored-mlx-v2` — 1.000
- **Baseline:** `gemma-4-26b-a4b-it-optiq` — p50=27ms, quality=0.400

## Per-Prompt Detail

### `mlabonne_qwen3-8b-abliterated`

| Prompt | p50_ms | p95_ms | quality | ok/runs | issues |
|--------|--------|--------|---------|---------|--------|
| meeting_150w | 1970 | 131243 | 0.00 | 0/3 | failed:connection_error:HTTPConnectionPool(host='localhost', port=1234): Max retries exceeded with url: /v1/chat/completions (Caused by NewConnectionError('<urllib3.connection.HTTPConnection object at 0x107b8a460>: Failed to establish a new connection: [Errno 61] Connection refused')) |
| phone_call_mat | 2 | 4 | 0.00 | 0/3 | failed:connection_error:HTTPConnectionPool(host='localhost', port=1234): Max retries exceeded with url: /v1/chat/completions (Caused by NewConnectionError('<urllib3.connection.HTTPConnection object at 0x107b941c0>: Failed to establish a new connection: [Errno 61] Connection refused')) |
| dictation_note | 1 | 2 | 0.00 | 0/3 | failed:connection_error:HTTPConnectionPool(host='localhost', port=1234): Max retries exceeded with url: /v1/chat/completions (Caused by NewConnectionError('<urllib3.connection.HTTPConnection object at 0x107377280>: Failed to establish a new connection: [Errno 61] Connection refused')) |
| tech_discussion | 3 | 29 | 0.00 | 0/3 | failed:connection_error:HTTPConnectionPool(host='localhost', port=1234): Max retries exceeded with url: /v1/chat/completions (Caused by NewConnectionError('<urllib3.connection.HTTPConnection object at 0x107b9ea30>: Failed to establish a new connection: [Errno 61] Connection refused')) |
| mixed_ru_en | 1 | 1 | 0.00 | 0/3 | failed:connection_error:HTTPConnectionPool(host='localhost', port=1234): Max retries exceeded with url: /v1/chat/completions (Caused by NewConnectionError('<urllib3.connection.HTTPConnection object at 0x107b80970>: Failed to establish a new connection: [Errno 61] Connection refused')) |

### `gemma-4-26b-a4b-it-assistant`

| Prompt | p50_ms | p95_ms | quality | ok/runs | issues |
|--------|--------|--------|---------|---------|--------|
| meeting_150w | 7 | 7 | 0.00 | 0/3 | failed:http_400 |
| phone_call_mat | 6 | 13 | 0.00 | 0/3 | failed:http_400 |
| dictation_note | 4 | 5 | 0.00 | 0/3 | failed:http_400 |
| tech_discussion | 5 | 13 | 0.00 | 0/3 | failed:http_400 |
| mixed_ru_en | 4 | 5 | 0.00 | 0/3 | failed:http_400 |

### `gemma-4-26b-a4b-it-optiq`

| Prompt | p50_ms | p95_ms | quality | ok/runs | issues |
|--------|--------|--------|---------|---------|--------|
| meeting_150w | 10046 | 11691 | 1.00 | 3/3 | — |
| phone_call_mat | 8837 | 11651 | 1.00 | 3/3 | — |
| dictation_note | 27 | 42 | 0.00 | 0/3 | failed:http_400 |
| tech_discussion | 19 | 26 | 0.00 | 0/3 | failed:http_400 |
| mixed_ru_en | 18 | 19 | 0.00 | 0/3 | failed:http_400 |

### `supergemma4-26b-uncensored-mlx-v2`

| Prompt | p50_ms | p95_ms | quality | ok/runs | issues |
|--------|--------|--------|---------|---------|--------|
| meeting_150w | 8831 | 9640 | 1.00 | 3/3 | — |
| phone_call_mat | 7871 | 9361 | 1.00 | 3/3 | — |
| dictation_note | 4542 | 4662 | 1.00 | 3/3 | — |
| tech_discussion | 8380 | 18156 | 1.00 | 3/3 | — |
| mixed_ru_en | 14599 | 61248 | 1.00 | 2/3 | — |

### `huihui-glm-4.7-flash-abliterated-mlx`

| Prompt | p50_ms | p95_ms | quality | ok/runs | issues |
|--------|--------|--------|---------|---------|--------|
| meeting_150w | 26887 | 45538 | 0.00 | 0/3 | failed:empty_response |
| phone_call_mat | 17822 | 22458 | 0.00 | 0/3 | failed:empty_response |
| dictation_note | 14811 | 16800 | 0.00 | 0/3 | failed:empty_response |
| tech_discussion | 19966 | 22958 | 0.00 | 0/3 | failed:empty_response |
| mixed_ru_en | 20098 | 20366 | 0.00 | 0/3 | failed:empty_response |

### `qwen3-14b-abliterated`

| Prompt | p50_ms | p95_ms | quality | ok/runs | issues |
|--------|--------|--------|---------|---------|--------|
| meeting_150w | 38562 | 43131 | 0.20 | 3/3 | too_short(0.00), no_cyrillic |
| phone_call_mat | 25758 | 28266 | 0.00 | 3/3 | too_short(0.00), mat_dropped:блять, mat_dropped:блин, mat_dropped:ё-моё, mat_dropped:хватит, no_cyrillic |
| dictation_note | 14811 | 14882 | 0.20 | 3/3 | too_short(0.00), no_cyrillic |
| tech_discussion | 25919 | 26577 | 0.20 | 3/3 | too_short(0.00), no_cyrillic |
| mixed_ru_en | 19984 | 20448 | 0.20 | 3/3 | too_short(0.00), no_cyrillic |

### `qwen3.6-27b-ud-mlx`

| Prompt | p50_ms | p95_ms | quality | ok/runs | issues |
|--------|--------|--------|---------|---------|--------|
| meeting_150w | 120712 | 121575 | 0.00 | 0/3 | failed:timeout |
| phone_call_mat | 120408 | 123480 | 0.00 | 0/3 | failed:timeout |
| dictation_note | 120764 | 121360 | 0.00 | 0/3 | failed:timeout |
| tech_discussion | 120098 | 120964 | 1.00 | 1/3 | — |
| mixed_ru_en | 120153 | 120301 | 0.00 | 0/3 | failed:timeout |

## Recommendation

Baseline `gemma-4-26b-a4b-it-optiq`: p50=27ms, quality=0.400, json_valid=0.40

Best overall composite (quality − latency_penalty): `gemma-4-26b-a4b-it-optiq`
- p50=27ms
- quality=0.400
- json_valid_ratio=0.40

### Switch recommendation
> **HOLD** — baseline `gemma-4-26b-a4b-it-optiq` remains the best option. No switch recommended.

## Raw JSON

```json
{
  "gemma-4-26b-a4b-it-optiq": {
    "latency_p50_ms": 27,
    "latency_p95_ms": 11691,
    "quality_avg": 0.4,
    "json_valid_ratio": 0.4
  },
  "supergemma4-26b-uncensored-mlx-v2": {
    "latency_p50_ms": 8380,
    "latency_p95_ms": 61248,
    "quality_avg": 1.0,
    "json_valid_ratio": 0.9333333333333333
  },
  "qwen3-14b-abliterated": {
    "latency_p50_ms": 24824,
    "latency_p95_ms": 43131,
    "quality_avg": 0.15999999999999998,
    "json_valid_ratio": 1.0
  },
  "huihui-glm-4.7-flash-abliterated-mlx": {
    "latency_p50_ms": 19966,
    "latency_p95_ms": 45538,
    "quality_avg": 0.0,
    "json_valid_ratio": 0.0
  },
  "qwen3.6-27b-ud-mlx": {
    "latency_p50_ms": 120301,
    "latency_p95_ms": 123480,
    "quality_avg": 0.2,
    "json_valid_ratio": 0.06666666666666667
  },
  "gemma-4-26b-a4b-it-assistant": {
    "latency_p50_ms": 5,
    "latency_p95_ms": 13,
    "quality_avg": 0.0,
    "json_valid_ratio": 0.0
  },
  "mlabonne_qwen3-8b-abliterated": {
    "latency_p50_ms": 2,
    "latency_p95_ms": 131243,
    "quality_avg": 0.0,
    "json_valid_ratio": 0.0
  }
}
```


---

## R19 Simple Re-bench (Wave 43 inline)

# R19 Simple Bench — 2 models
Date: 2026-05-12 04:56

| Model | p50_avg_ms | quality_avg | notes |
|---|---|---|---|
| `supergemma4-26b-uncensored-mlx-v2` | 1591 | 1.00 | — |
| `gemma-4-26b-a4b-it-optiq` | 1587 | 1.00 | — |

## Per-prompt sample outputs (supergemma)
- **dictation_note** (1770ms, q=1.0): `Заметка на завтра: нужно купить кофе, молоко и хлеб; потом позвонить врачу, записаться на приём.`
- **phone_short** (1632ms, q=1.0): `Алло, слушай, у нас вчера на проде упал API. Я уже посмотрел логи: там какая-то фигня с авторизацией.`
- **tech_short** (1575ms, q=1.0): `Значит, используем MLH Whisper для транскрипции, но есть проблема: когда два потока обращаются одновременно, получаем SI`
- **mixed_ru_en** (1474ms, q=1.0): `Окей, давай обсудим деплой на прод. Я уже пушнул в GitHub, ветка feature/refactor-logger.`
- **meeting_short** (1506ms, q=1.0): `Так, сегодня на встрече обсуждаем три вещи: интеграция с Python, развёртывание на Mac и авторизация через OpenAI ключ.`

## Final Conclusion (Wave 43)

**Production switch: NOT recommended.** After warmup, baseline `gemma-4-26b-a4b-it-optiq` and `supergemma4-26b-uncensored-mlx-v2` are statistically identical (1587ms vs 1591ms p50). Baseline preserves technical terminology more reliably ("MLX Whisper" vs "MLH Whisper" in tech_short prompt).

**Original partial bench was misleading** — `response_format: {"type": "json_object"}` is no longer accepted by LM Studio (requires `json_schema` or `text`). Production `llm_rewriter.py` doesn't use `response_format`, so this was a bench-only bug. Baseline failures on short prompts in the first table are artifacts of that bug, not real production issues.

**30s cold-load gap** for baseline after TTL eviction is real UX issue — partially mitigated by Wave 43 LM Studio retry-with-backoff in `llm_rewriter.py`.

**R20 candidates** for future bench (newly downloaded, not yet tested):
- `Jiunsong/supergemma4-26b-abliterated-multimodal-mlx-4bit` (multimodal!)
- `mlx-community/gemma-4-31B-it-assistant-bf16` (31B parameters, bf16 quality)
- `zecanard/gemma-4-E2B-it-ultra-uncensored-heretic-MLX-8bit-int8-affine` (8bit affine)
