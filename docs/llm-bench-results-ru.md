# LLM-бенчмарк Krab Ear — резюме на русском

> 📌 **Полная техническая документация (в т.ч. для cloud routines):** [`llm-bench-results.md`](./llm-bench-results.md). Этот файл — человекочитаемая выжимка для тебя, обновляется вместе с английской версией.

**Обновлено:** 2026-04-30

## Текущее production-окружение

| Компонент | Модель | Размер | Скорость | Где живёт |
|-----------|--------|-------:|---------:|-----------|
| 🥇 **LLM Rewriter** (исправление транскрипций) | `qwen3.5-9b@6bit` | 7.7 GB | 1.4 с/запрос | LM Studio, JIT-загрузка |
| 🥇 **VL Companion** (анализ скриншотов в Telegram) | `Pixtral-12B-4bit` | 6.7 GB | 26 с/изображение | LM Studio + mlx-vlm |
| 🛠️ **Diктовка → текст** | Whisper Large v3 Turbo + GigaAM | в Krab Ear | — | Krab Ear backend |

**Total RAM budget production**: ~15 GB (rewriter + VL) на M4 Max 36 GB. Помещается комфортно даже когда параллельно работают другие приложения.

## GUI Dropdown — что доступно для переключения (10 опций, hot-swap без рестарта)

1. 🥇 `qwen3.5-9b@6bit` (7.7 GB, 1.4 с) — **default**
2. 🥈 `huihui-qwen3-14b-abl-v2` (7.8 GB, 1.9 с) — alt rewriter
3. 🥈 `huihui-qwen3-30b-...-dwq4-mlx` (17.8 GB, 4.5 с) — premium когда хватает RAM
4. 🥇 `Qwen3-8B-MLX-4bit` (4.3 GB, **0.9 с**) — speed champ (R20 winner)
5. 🥈 `aya-expanse-8b` (4.2 GB, 0.8 с) — fastest но любит paraphrase
6. 🥈 `Aya-Expanse-32B-abliterated` (16.9 GB, 7.8 с) — для длинных диктовок
7. 🥈 `Qwen3.5-27B-Text-heretic-mxfp4-mlx` (13.3 GB) — backup
8. 🥉 `qwen2.5-14b-uncensored-mlx` (7.7 GB) — backward compat
9. 🥉 `Hermes-3-Llama-8B` (4.2 GB) — fastest но brand-blind
10. 🥉 `huihui-qwen3-4b-instruct-2507-abliterated-hi-mlx` (2.6 GB) — мин-ресурс fallback

Переключение через GUI применяется **мгновенно** (без restart backend) — архитектурный fix `_handle_set_settings_with_hot_reload`.

## Архитектурные защиты от ребута

| Защита | Что делает |
|--------|-----------|
| `safe-bench.sh` wrapper | Перед каждым bench eject все модели LM Studio + kill orphan процессы + RAM check |
| Per-model RAM check | Внутри `text-bench.py` skip модель если `size + 4 GB > free RAM` (вместо краха всего bench) |
| Persistent file structure | Скрипты в `Krab Ear/scripts/llm-bench/`, venv в `~/.venv_vl/` (НЕ `/tmp/` который очищается на reboot) |
| Single-model discipline | Только одна модель загружена за раз во время бенча |

## Лимиты M4 Max 36 GB (выявлены сегодняшними двумя ребутами)

- ✅ **Безопасно** ≤14 GB модель + production rewriter (8 GB) = ~22 GB total
- ⚠️ **Рискованно** 14-25 GB модели с активным backend
- ❌ **Опасно** >25 GB (Qwen3.6-27B 25 GB → ребут, Qwen3-30B-Claude-Opus-distill 26 GB → 130 с задержка перед ребутом)

**Вывод**: вся production работа в зоне 7-15 GB. Большие модели требуют ручной очистки приложений перед бенчем.

## Brand regex (deterministic, до LLM)

Whisper транскрибирует бренды фонетически в кириллицу — мы возвращаем латиницу до LLM rewrite:

- `квен/Квен/QN14B/к Вен → Qwen`
- `LOM Studio → LM Studio`
- `ггуф/ахолув → GGUF`
- `инференс → inference`
- `Биткоин/Солана/Эфириум/Сафари/Хром/Обсидиан → Bitcoin/Solana/Ethereum/Safari/Chrome/Obsidian`
- `Ну/Да 0 X(глагол) → ОК` (Whisper artefact «ОК» транскрибируется как цифра 0)
- `0 X(императив) → ОК` (после диктовочных глаголов)
- `(слово) 0 (слово) → и` (mid-sentence союз с lookahead на единицы измерения чтоб не сломать «температура 0 градусов»)
- `0ли → или`, `припинания → препинания`

## Удалить с диска (~115 GB можно освободить)

Эти модели **дисквалифицированы** для нашего pipeline и не нужны:

| Модель | Размер | Почему удалить |
|--------|-------:|---------------|
| `granite-3.3-8b-instruct-4bit` | 4.3 GB | Цензурит мат, галлюцинирует цифры |
| `Qwen3.5-9B-abl-mlx-bypass` | 4.7 GB | `<think>` + chatbot tail |
| `Josiefied-Qwen3-30B-A3B-abliterated-v2-4bit` | 16 GB | `<think>` хардкорно |
| `Qwen3-30B-A3B-Claude-Opus-distill-abl-v2` | 26.2 GB | Эхает system prompt, упирается в лимит RAM |
| `mythomax-l2-lora-assemble-13b` | 7-9 GB | Заменён Huihui-14B-abl-v2 |
| `openhermes-2.5-mistral-7b-mlx-393a7` | 7.2 GB | Слишком агрессивно режет filler |
| `Qwen3.5-9B-mlx-vlm-mxfp4` | 5.3 GB | English output на RU prompt + reasoning leak |
| `Qwen3.5-35B-A3B-mlx-vlm-mxfp4` | 18 GB | Spam токенов `<\|im_start\|><\|im_start\|>` (broken quant) |
| gpt-oss-20b ×4 quants | ~67 GB total | OpenAI harmony format `<\|channel\|>analysis` хардкорно |
| `DeepSeek-R1-Distill-Qwen-32B-abliterated-4bit` | 17.2 GB | Hallucinates wildly (про model_call YAML вместо rewrite) |
| `Mistral-Small-3.2-24B-Instruct` | 12.6 GB | Pure chatbot mode, игнорит system prompt |

## Заблокировано upstream (ждём fix)

| Семейство | Сколько на диске | Что нужно |
|-----------|-----------------:|-----------|
| **Gemma 4** (5 моделей: e4b, 26B-A4B, 31b, Huihui-26B, SuperGemma4) | ~75 GB | mlx-vlm 0.5+ ИЛИ mlx-lm 0.32+ с `gemma4_text` модулем + chat template |
| Llama VL (`Llama-3.2-11B-Vision-abl`) | 19.9 GB | через mlx-vlm работает но качество низкое (113 с RU, generic output) |

## Cloud Routines (4/15 used)

Облачные задачи Anthropic, идут даже когда я закрыт:

- **Пн 09:00** — `mlx-llm-upstream-watcher`: проверка mlx-lm/transformers/LM Studio MLX releases для разблокировки Gemma 4 + qwen3_5
- **Пн 11:00** — `fresh-mlx-models-watcher`: HuggingFace trending check для свежих abliterated MLX моделей, рекомендует топ-3 на скачивание
- **Ср 10:00** — `disk-hygiene`: audit диска, alert если <500 GB free
- **1 число месяца 11:00** — `bench-regression`: detection регрессии production rewriter + предложения новых кандидатов

## Финальная картина после массового бенча (R26-R28, 2026-04-30 ночь)

После двух reboot, токена для HTTP API, отключения LM Studio guardrails — протестировано **~50+ моделей**.

### 🥇 Production stack (рекомендация)

| Роль | Модель | Размер | Скорость | Источник |
|------|--------|-------:|---------:|----------|
| **LLM Rewriter (default)** | `qwen3.5-9b@6bit` | 7.7 GB | 1.4 с | R19 winner |
| **Speed fallback** | `Qwen3-8B-MLX-4bit` | 4.3 GB | 0.9 с | R20 winner |
| **VL companion (Telegram)** | `Pixtral-12B-4bit` | 6.7 GB | ~26 с/img | VL Round winner |
| **Unified rewriter+VL** ⭐ | `gemma-4-e4b-it-mlx` | 6.4 GB | 0.8 с text + VL | R21 breakthrough |
| **Tiny ultra-fast** | `liquid/lfm2.5-1.2b` | 1.2 GB | 0.3 с | R25 (RU only, hallucinates) |

### 🥈 Strong alternatives

- `gemma-4-26b-a4b-it-OptiQ` (14.5 GB, 0.95 с) — Google official, лучший brand recognition («инференс → inference»). No VL.
- `Huihui-Qwen3-30B-A3B-Instruct-2507-abl-dwq4` (17.8 GB, 4.5 с) — R17 premium pick.
- `Aya-Expanse-32B-abliterated` (16.9 GB, 7.8 с) — длинные диктовки.
- `mistralai/devstral-small-2-2512` (13.2 GB, 1.3 с) — Mistral coder, partial mat.

### 🗑️ DELETE list (можно освободить ~150+ GB)

R22-R28 expansions to existing DELETE:
- **WhiteRabbitNeo-V3-7B** (7.6 GB) — hallucinates VL
- **microsoft/phi-4-reasoning-plus** (7.7 GB) — reasoning-bound
- **gemma-4-e4b-agentic-opus-reasoning-geminicli** (10.2 GB) — wrong format (torchSafetensors)
- **gemma-4-26b-a4b-jang_2l-crack** + **JANG_4M-CRACK** + **JANG_4M-Uncensored** (~46 GB total) — broken switch_mlp MoE conversion
- **yandexgpt-5-lite-8b-pretrain** (4.6 GB) — echoes system, ES→FR
- **Qwen3.6-27B-OptiQ** (15.4 GB) — OptiQ quant unsupported для Qwen3.6
- **SuperGemma4-26B-uncensored** (13.3 GB) — token spam, broken chat template
- **Huihui-gemma-4-26B-A4B-abliterated** (14.6 GB) — echo + token spam
- **Qwen3.6-35B-A3B-Abliterated-Heretic** (22.9 GB) — reasoning ON, crash on mat, slow

Plus раньше: gpt-oss-20b ×4 quants (~67 GB), DeepSeek-R1-Distill-32B (17 GB), Mistral-Small-3.2-24B (13 GB), granite-3.3-8b (4 GB), mythomax (~9 GB), Qwen3-30B-Claude-Opus-distill (26 GB), Josiefied-Qwen3-30B (16 GB), Qwen3.5-35B-A3B-mlx-vlm (18 GB).

**Итого** ~250+ GB можно освободить.

### Архитектурные открытия

- **LM Studio MLX 1.7.0 имеет proprietary Gemma 4 patches** — где raw mlx-vlm падает, LM Studio JIT работает.
- **Reasoning models split content/reasoning_content** в OpenAI API (Qwen3.6-Heretic, Phi-4-reasoning) — bench scripts должны читать оба поля.
- **OptiQ quant** (Optimal Quantization) поддерживается в LM Studio для Gemma 4, но НЕ для Qwen3.6.
- **dealignai community Gemma 4 cracks** все broken (switch_mlp MoE conversion issues).
- **Heretic abliteration** — partial: убирает простые safety triggers, но crash on heavy mat possible.

---

## Uncensored модели (2026-04-30)

### Протестированные (полный аудит)

| # | Модель | Размер | Формат | Mat | ES | Speed | Verdict |
|---|--------|-------:|--------|-----|:--:|------:|---------|
| 🥇 | **Saiga Nemo 12B** (IlyaGusev) | 7.0 GB | GGUF | 🏆 natural («заебало») | ❌→RU | 2.2 с | **RU-only uncensored** (natural mat!) |
| 🥈 | **Qwen3-8B-abliterated** (mlabonne) | 4.7 GB | GGUF | ✅ verbatim | ✅ | 3.3 с | **Bilingual uncensored** (RU+ES) |
| 🥉 | **Qwen3-14B-abliterated** | 8.4 GB | GGUF | ✅ verbatim | ✅ | 3.5 с | **Quality bilingual** (best brands) |
| 4 | **Qwen2.5-14B-abl-v2** (imatrix) | 8.9 GB | GGUF | ✅ mild para | ✅ | 5.3 с | imatrix quality |
| 5 | **Saiga Gemma3 12B** (IlyaGusev) | 6.8 GB | GGUF | ✅ natural | ❌→RU | 4.8 с | Most "evil" (generates phishing HTML) |
| 6 | gemma-4-e4b-it-mlx | 6.4 GB | MLX | ✅ мат OK | ✅ | 0.8 с | Best unified rewriter+VL |
| 7 | DarkIdol-Llama-3.1-8B | 4.6 GB | GGUF | ✅ zero refusals | ❌ EN | 2.5 с | Dark creative EN only |
| 8 | Unholy-v2-13B | 7.3 GB | GGUF | ✅ lock picking OK | ❌ EN | — | Genuine EN uncensored |
| 9 | GLM-4.7-Flash-abl | 15.7 GB | MLX | ✅ мат OK | ❌ EN reason | 7 с | Partial uncensored, crash |
| 10 | Dolphin 2.9 | 4.2 GB | MLX | ? | ? | — | ❌ broken (token spam) |

### Вывод
- **Для RU-only uncensored**: Saiga Nemo 12B (natural RU mat, 7 GB, 2.2 с)
- **Для bilingual (RU+ES) uncensored**: Qwen3-8B-abliterated (4.7 GB, мат verbatim, ES preserved)
- **Для production rewriter**: qwen3.5-9b@6bit MLX остаётся default (1.4 с, abliterated enough)
- **Для «злых» задач (attack code, фишинг)**: Saiga Gemma3 12B (реально генерирует HTML фишинговых страниц)

### Security tools установлены
nmap, sqlmap, nuclei, subfinder, ffuf, gobuster — все через brew в /opt/homebrew/bin/.

### HexStrike AI (150+ pentest tools)
Зарегистрирован как MCP server в user-level Claude Code settings. Запускается через `hexstrike_server.py`.

---

## Старая секция: Что в работе (R21, 2026-04-30 ранее)

### 🎯 Главное открытие: Gemma 4 РАБОТАЕТ через LM Studio!

`lmstudio-community/gemma-4-E4B-it-MLX-4bit` (6.4 GB на диске):
- **Avg latency ~800 ms** — БЫСТРЕЕ нашего production default (Qwen3.5-9B-6bit, 1.4 с)
- Все 5 prompts чистые: brands ✅, mat ✅, ES сохранён, без `<think>` / artefacts
- Минус: «инференс» оставила кириллицей (Gemma-3-12b-it-qat правильно перевела в `inference`)

**Почему важно**: LM Studio MLX 1.7.0 имеет **proprietary Gemma 4 patches**, которых нет в open-source mlx-vlm/mlx-lm. Раньше мы считали «5 Gemma 4 моделей на диске (~75 GB) заблокированы upstream». Теперь видно: **через LM Studio JIT они могут работать!** Стоит попробовать остальные 4: Huihui-gemma-4-26B (14.6 GB), gemma-4-26B-OptiQ (14.6 GB), gemma-4-31b-abl (16.1 GB), SuperGemma4-26B (13.3 GB).

### Остальные downloads (в работе)

- `Youssofal/Qwen3.6-35B-A3B-Abliterated-Heretic-MLX-4bit` (~19 GB) — 🔄 ~9/19 GB
- `mlx-community/Qwen3.6-27B-OptiQ-4bit` (~14 GB) — ⏳ очередь

После завершения downloads — bench через `safe-bench.sh`. Главные кандидаты на смену production default:
1. **Gemma 4 E4B** (6.4 GB, 0.8 с) — уже verified, можно A/B тестить
2. **Qwen3.6-35B-Heretic** (19 GB, MoE 3B-active) — после download
3. **Qwen3.6-27B-OptiQ** (14 GB) — после download

Если кто-то лучше Qwen3.5-9B-6bit (наш default) — hot-swap сделаю мгновенно.

## История раундов (краткая)

- **R1-R16** (26-28 апреля): 30+ моделей. Лидер был qwen2.5-14b-uncensored-mlx
- **R17** (29 апреля 00:30): Huihui-Qwen3-30B-A3B-Instruct-2507-abliterated-dwq4 (17.8 GB, 4.5 с)
- **R18** (29 апреля 17:45): Huihui-Qwen3-14B-abl-v2 (7.8 GB, 1.9 с) — 56% меньше при том же качестве
- **R19** (29 апреля 18:10): **`qwen3.5-9b@6bit`** (7.7 GB, 1.4 с) — текущий default
- **R19b** (29 апреля 18:20): 5 моделей все ❌ (gpt-oss harmony format, DeepSeek hallucinations, Mistral chatbot mode)
- **VL Round** (29 апреля 18:30): **Pixtral-12B-4bit** для main Krab Telegram
- **R20** (29 апреля 23:50): Qwen3-8B-MLX-4bit (4.3 GB, **0.9 с**) — новый speed champ; Gemma-3 + Qwen3.5-9B-8bit ❌ ES→RU перевод
- **R21** (30 апреля): downloads в работе, скоро бенч
