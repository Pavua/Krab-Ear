# Phase 1.8 E2E Acceptance Test Fixtures Specification

## Overview

This document specifies the complete set of audio fixtures required for Phase 1.8 full acceptance test suite. Each fixture represents a real-world use case validating one or more acceptance criteria from Phase 1 spec Section 11.

**Total fixtures**: 33 clips (42 minutes combined)

## Audio Format Specifications

All fixtures must conform to:
- **Format**: WAV (RIFF), PCM uncompressed
- **Sample rate**: 16 kHz (standard for whisper models)
- **Channels**: Mono (1 channel)
- **Bit depth**: 16-bit
- **SNR**: >= 25 dB (signal-to-noise ratio, measured from silence detection)
- **Audio levels**: -6 dB to -3 dB peak (reasonable headroom, no clipping)
- **Speech pace**: natural conversational speed (120–160 WPM)

**Command to validate WAV format**:
```bash
ffprobe -v error -show_entries stream=sample_rate,channels -of csv=p=0 fixtures/ru_01.wav
# Expected output: 16000,1
```

---

## Fixture Groups

### Group 1: Russian Language Baseline (RU_01 — RU_10)

10 short sentences in Russian, each 3–5 seconds, natural speech. Covers common ASR use cases.

| ID | Filename | Transcript | Emotion | Duration | Notes |
|---|---|---|---|---|---|
| RU_01 | `ru_01_greeting.wav` | Привет, как дела? | Neutral | 3s | Simple greeting, low noise expectation |
| RU_02 | `ru_02_weather.wav` | На улице холодно, пойду одеваться. | Neutral | 4s | Weather observation, ~15 words |
| RU_03 | `ru_03_meeting.wav` | Завтра встреча в два часа по московскому времени. | Neutral | 4s | Business context, time specification |
| RU_04 | `ru_04_instruction.wav` | Напомни мне позвонить маме после работы. | Directive | 4s | Action request, personal context |
| RU_05 | `ru_05_question.wav` | Когда закончится проект на работе? | Inquiry | 3s | Future-oriented question |
| RU_06 | `ru_06_emotion_positive.wav` | Получилось отлично, спасибо большое за помощь! | Positive | 4s | Gratitude, positive emotion |
| RU_07 | `ru_07_emotion_negative.wav` | Это совершенно неправильно, так не может быть. | Negative | 4s | Disagreement, negative emotion |
| RU_08 | `ru_08_technical.wav` | Установи новую версию приложения из App Store. | Directive | 4s | Technical instruction, precise language |
| RU_09 | `ru_09_numbers.wav` | Курс доллара сегодня составляет сто двадцать пять рублей. | Neutral | 5s | Numerical content, financial context |
| RU_10 | `ru_10_whisper.wav` | (whispered) Тихий разговор в библиотеке | Neutral | 4s | Low-amplitude speech, VAD challenge |

**Expected accuracy threshold**: >= 85% (WER)
**Emotion detection baseline**: neutral=7, positive=1, negative=1, others=1

---

### Group 2: English Language Baseline (EN_01 — EN_10)

10 short sentences in English, each 3–5 seconds, natural speech. Validates multilingual support.

| ID | Filename | Transcript | Emotion | Duration | Notes |
|---|---|---|---|---|---|
| EN_01 | `en_01_greeting.wav` | Hi, how are you today? | Neutral | 3s | Standard greeting |
| EN_02 | `en_02_instruction.wav` | Can you send me the project report by tomorrow? | Neutral | 4s | Polite request, business context |
| EN_03 | `en_03_question.wav` | What time is the meeting tomorrow? | Inquiry | 3s | Schedule-related question |
| EN_04 | `en_04_emotion_happy.wav` | That sounds absolutely wonderful! I'm so excited! | Positive | 4s | Enthusiasm, positive emotion |
| EN_05 | `en_05_emotion_frustrated.wav` | This is not what I asked for at all. | Negative | 3s | Frustration, negative emotion |
| EN_06 | `en_06_casual.wav` | I'm just hanging out at home, nothing special. | Neutral | 4s | Casual conversation |
| EN_07 | `en_07_technical.wav` | Make sure to back up your files regularly. | Directive | 4s | Technical advice, imperative |
| EN_08 | `en_08_numbers.wav` | The GDP grew by two point five percent last year. | Neutral | 4s | Numerical/statistical content |
| EN_09 | `en_09_sentence_complexity.wav` | Although the project was challenging, we managed to deliver it on time without compromising quality. | Neutral | 5s | Complex sentence, multiple clauses |
| EN_10 | `en_10_fast_speech.wav` | (fast) Really quickly now telling you everything at once super fast okay here we go | Neutral | 4s | Rapid speech, challenging VAD |

**Expected accuracy threshold**: >= 85% WER
**Emotion detection baseline**: neutral=6, positive=1, negative=1, others=2

---

### Group 3: Spanish Language Baseline (ES_01 — ES_10)

10 short sentences in Spanish, each 3–5 seconds, natural speech. Validates ES language support.

| ID | Filename | Transcript | Emotion | Duration | Notes |
|---|---|---|---|---|---|
| ES_01 | `es_01_greeting.wav` | Hola, ¿cómo estás? | Neutral | 3s | Spanish greeting, ¿/ punctuation |
| ES_02 | `es_02_weather.wav` | Hace mucho calor hoy, necesito agua fría. | Neutral | 4s | Weather, common expressions |
| ES_03 | `es_03_formal.wav` | Le agradezco su tiempo y atención. | Formal | 4s | Formal register, courtesy |
| ES_04 | `es_04_emotion_happy.wav` | ¡Qué día más hermoso! ¡Estoy muy feliz! | Positive | 4s | Exclamation, positive emotion |
| ES_05 | `es_05_question.wav` | ¿A qué hora llegará el autobús? | Inquiry | 3s | Transportation-related question |
| ES_06 | `es_06_instruction.wav` | Por favor, cierra la puerta cuando salgas. | Directive | 4s | Polite instruction |
| ES_07 | `es_07_emotion_angry.wav` | No puedo creer que hayas hecho esto! | Negative | 3s | Anger, negative emotion |
| ES_08 | `es_08_numbers.wav` | La población de México es de ciento treinta millones. | Neutral | 4s | Large numbers, demographic data |
| ES_09 | `es_09_colloquial.wav` | Vale, tío, nos vemos luego en el bar. | Casual | 4s | Colloquial Spanish (Spain), "vale", "tío" |
| ES_10 | `es_10_whisper.wav` | (whispered) Conversación silenciosa en secreto | Neutral | 3s | Whispered speech, dynamic range |

**Expected accuracy threshold**: >= 85% WER
**Emotion detection baseline**: neutral=5, positive=1, negative=1, formal=1, casual=1, others=1

---

### Group 4: Code-Switching (MIX_01)

1 conversation with 5 sentences alternating Russian ↔ English. Tests language detection + context switching.

| ID | Filename | Transcript | Language Mix | Duration | Notes |
|---|---|---|---|---|---|
| MIX_01 | `mix_01_ru_en_conversation.wav` | **Sentence 1** (RU): Привет, как дела? **Sentence 2** (EN): I'm doing great, thanks! **Sentence 3** (RU): Слушай, мне нужна твоя помощь. **Sentence 4** (EN): Of course, what do you need? **Sentence 5** (RU): Спасибо, это очень важно для меня. | RU+EN | 15s | Natural code-switching, simulates bilingual speaker |

**Expected behavior**: STT model should detect both languages within same clip; translation service should handle mixed input or fall back to primary language detection.
**Accuracy threshold**: >= 80% per language segment (more lenient due to switching complexity)

---

### Group 5: Long-Form Content (LONG_01)

1 Russian monologue (~1 minute), tests 4-minute transcript recycling + streaming performance.

| ID | Filename | Transcript (summary) | Language | Duration | Notes |
|---|---|---|---|---|---|
| LONG_01 | `long_01_ru_monologue.wav` | Multi-paragraph story about a day at work: morning commute, office meetings, team discussion, lunch break, afternoon tasks, evening conclusion. Includes natural pauses, emotion shifts, and topical coherence. | RU | 60s | Tests streaming STT, audio chunking, context retention for long utterances |

**Full text** (for reference):
```
Сегодня утром я проснулся в семь часов и сразу занялся зарядкой. 
После завтрака я поехал в офис на метро, народу было немало. 
На работе прошла планёрка с командой, обсуждали квартальные цели. 
Потом я сосредоточился на коде, пока не пришло время обеда. 
На обеде я пообщался с коллегами из соседнего отдела. 
Вернулся и продолжил работу над проектом до вечера. 
В целом день прошёл продуктивно и я доволен результатами.
```

**Expected accuracy threshold**: >= 82% WER (more lenient for long-form)
**Segmentation test**: verify system correctly breaks audio into chunks without losing context

---

### Group 6: Edge Cases (EDGE_01)

1 silence-only clip (~5 seconds), tests silence detection + privacy-preserving recording behavior.

| ID | Filename | Transcript | Audio Content | Duration | Notes |
|---|---|---|---|---|---|
| EDGE_01 | `edge_01_silence.wav` | (empty — silence only) | Ambient noise only (< -30 dB), no speech | 5s | Tests VAD robustness; should return empty transcript without error |

**Expected behavior**: System should detect no speech activity and return empty result (or placeholder message "No speech detected") without crashing or reporting false positives.

---

## Test Case Matrix

### Acceptance Criteria Coverage Map

| AC# | Fixture Group(s) | Validation Goal | Expected Outcome |
|---|---|---|---|
| AC1 (STT in RU) | RU_01–RU_10, LONG_01 | Russian speech recognition accuracy | >= 85% WER on all clips |
| AC2 (STT in EN) | EN_01–EN_10 | English speech recognition accuracy | >= 85% WER on all clips |
| AC3 (STT in ES) | ES_01–ES_10 | Spanish speech recognition accuracy | >= 85% WER on all clips |
| AC4 (Language detection) | RU_01–ES_10, MIX_01 | Auto-detect language from audio stream | Correct lang code for 100% of monolingual clips; >= 80% for mixed |
| AC5 (Code-switching handling) | MIX_01 | Handle bilingual input gracefully | Both languages transcribed; user can control output language |
| AC6 (Emotion/mood detection) | RU_06–RU_07, EN_04–EN_05, ES_04, ES_07 | Extract emotional tone from speech | >= 70% accuracy on labeled emotion dataset |
| AC7 (Text cleanup/normalization) | RU_09, EN_08, ES_08 | Remove artifacts, fix common errors | Cleaned text matches reference without semantic loss |
| AC8 (Speaker diarization) | (Future: DIAR_01 group, not included in Phase 1.8 bootstrap) | Multi-speaker segmentation | — |
| AC9 (Long-form streaming) | LONG_01 | Handle 60+ second recordings with 4-min recycle | Full transcript + zero loss; recycle timer resets correctly |
| AC10 (Edge cases / robustness) | EDGE_01, RU_10, EN_10 | Graceful handling of challenging audio | No crashes; reasonable fallback behavior |

---

## Metadata per Fixture

Each fixture requires a `.json` sidecar in the same directory:

**Example: `ru_01_greeting.json`**
```json
{
  "fixture_id": "RU_01",
  "filename": "ru_01_greeting.wav",
  "language": "ru",
  "transcript_reference": "Привет, как дела?",
  "emotion_labels": ["neutral"],
  "duration_seconds": 3,
  "sample_rate": 16000,
  "channels": 1,
  "snr_db": 28,
  "background_noise_type": "office",
  "speech_pace_wpm": 140,
  "expected_wer_threshold": 0.85,
  "notes": "Simple greeting, low noise expectation",
  "created_date": "2026-04-17",
  "created_by": "user_or_tool",
  "acceptance_criteria": ["AC1", "AC4", "AC7"],
  "tags": ["baseline", "short-form", "friendly"]
}
```

---

## Recording / Synthesis Guidelines

### Option A: User-Provided Recordings
1. Record each clip with a quality USB microphone or built-in MacBook mic in a quiet environment.
2. Use `ffmpeg` or Audacity to normalize and export as 16 kHz mono WAV.
3. Verify with `ffprobe` (sample rate, channels, duration).
4. Place in `KrabEar/tests/e2e/fixtures/` alongside `.json` metadata.

### Option B: macOS `say` Command Bootstrap (Automated)
For rapid bootstrap and reproducibility, use macOS Text-to-Speech (TTS):

```bash
# Russian example
say -v "Milena" "Привет, как дела?" -o ru_01_greeting.aiff
ffmpeg -i ru_01_greeting.aiff -acodec pcm_s16le -ar 16000 -ac 1 ru_01_greeting.wav
rm ru_01_greeting.aiff

# English example
say -v "Samantha" "Hi, how are you today?" -o en_01_greeting.aiff
ffmpeg -i en_01_greeting.aiff -acodec pcm_s16le -ar 16000 -ac 1 en_01_greeting.wav
rm en_01_greeting.aiff

# Spanish example
say -v "Monica" "Hola, ¿cómo estás?" -o es_01_greeting.aiff
ffmpeg -i es_01_greeting.aiff -acodec pcm_s16le -ar 16000 -ac 1 es_01_greeting.wav
rm es_01_greeting.aiff
```

**Tradeoffs**:
- ✅ **Pro**: Instant, reproducible, exact same transcription every test run (no noise variation).
- ❌ **Con**: Synthetic accent may not match real-world acoustic variation; emotion detection may be less sensitive.

### Option C: Mixed Approach (Recommended for Phase 1.8)
- RU_01–RU_10: Use `say -v "Milena"` (Russian TTS)
- EN_01–EN_10: Use `say -v "Samantha"` (US English TTS)
- ES_01–ES_10: Use `say -v "Monica"` (Spanish TTS)
- MIX_01: Hand-recorded or spliced from two TTS narrations
- LONG_01: Hand-recorded (human monologue more realistic)
- EDGE_01: Generate silence via `ffmpeg -f lavfi -i anullsrc=r=16000:cl=mono -t 5 edge_01_silence.wav`

---

## Fixture Discovery & Loading (E2E Test Integration)

E2E tests will auto-discover fixtures from `KrabEar/tests/e2e/fixtures/`:

```python
import json
import os
from pathlib import Path

def load_fixtures(fixture_dir="KrabEar/tests/e2e/fixtures"):
    """Auto-discover all .wav fixtures and their metadata."""
    fixtures = {}
    for json_file in Path(fixture_dir).glob("*.json"):
        with open(json_file) as f:
            metadata = json.load(f)
            fixture_id = metadata["fixture_id"]
            wav_path = Path(fixture_dir) / metadata["filename"]
            if wav_path.exists():
                fixtures[fixture_id] = {
                    "metadata": metadata,
                    "wav_path": str(wav_path)
                }
    return fixtures
```

---

## Timeline & Ownership

| Phase | Owner | Task | Target Date |
|---|---|---|---|
| **1.8 Bootstrap** | This PR | Spec + directory structure | 2026-04-17 |
| **1.8 Implementation** | Phase 1.8 agent | Populate fixtures (TTS or recorded) | 2026-04-20 |
| **1.8 Integration** | Phase 1.8 agent | Implement E2E test runners + CI | 2026-04-25 |
| **Acceptance** | QA / User | Validate against real workflows | 2026-05-01 |

---

## Checklist for Phase 1.8 Agent

- [ ] Generate or record all 33 clips (RU: 11, EN: 10, ES: 10, MIX: 1, EDGE: 1)
- [ ] Create `.json` sidecar for each clip with metadata
- [ ] Verify all `.wav` files: `ffprobe` check sample_rate=16000, channels=1
- [ ] Verify all `.json` files are valid JSON and match their `.wav` counterparts
- [ ] Implement `load_fixtures()` in test harness
- [ ] Create parametrized test cases for each fixture group
- [ ] Add fixture list to CI pipeline (`.github/workflows/ci.yml`)
- [ ] Document fixture refresh process (when/how to regenerate if needed)
- [ ] Update `fixtures/README.md` with final implementation notes

---

## Summary

**Total fixtures**: 33 clips
**Total duration**: ~42 minutes
**Coverage**: 10 acceptance criteria (AC1–AC10)
**Languages**: RU (11), EN (10), ES (10), Code-switched (1), Edge case (1)
**Format**: WAV 16 kHz mono PCM
**Metadata**: JSON sidecar per clip
**Bootstrap**: Directory structure + spec only (Phase 1.8 agent populates content)
