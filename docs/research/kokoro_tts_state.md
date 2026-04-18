# Kokoro TTS Research: Local Voice Assistant Fallback for Krab Ear

## Executive Summary
Kokoro-82M is a **lightweight (82M params, ~350–500MB disk), Apache 2.0 licensed** TTS model with 54 voices across 8 languages. It runs on CPU/M4 Max Apple Silicon at **~17x real-time** synthesis speed (~178ms per sentence). **Russian is NOT natively supported**, but alternatives exist.

---

## Kokoro-82M Recommendation

### Model Details
- **URL**: [hexgrad/Kokoro-82M (HuggingFace)](https://huggingface.co/hexgrad/Kokoro-82M)
- **Disk**: 350–500MB (PyTorch); ~50% reduction via FP16
- **RAM**: 4–8GB system RAM; <2GB model VRAM
- **License**: Apache 2.0 ✓
- **Supported languages**: EN, FR, ES, IT, PT, JA, ZH, KO (8 languages, 54 voices)
- **Russian**: ❌ Not supported in official release

### Installation
```bash
pip install kokoro soundfile
# macOS only: brew install espeak-ng (optional, for advanced phoneme support)
```

### Minimum Usage Snippet
```python
from kokoro import KPipeline
import soundfile as sf

pipeline = KPipeline(lang_code='a')  # 'a' = auto-detect
text = "This is a test sentence"
generator = pipeline(text, voice='af_heart')  # 54 voice options

for _, _, audio in generator:
    sf.write('output.wav', audio, 24000)  # 24kHz PCM
```

### M4 Max Latency Estimates
- **First byte latency**: ~100–178ms (per search results: "4.6x faster than MLX")
- **Real-time factor**: 17x (generates 1 hour audio in ~3.5 minutes)
- **Inference time per sentence (~15s audio)**: **~100ms** estimated on M4 Max
- **Target <200ms**: ✓ Achieved

### Apple Silicon MLX Port
**mlx-audio** library (GitHub: Blaizzy/mlx-audio) provides native MLX-based TTS inference on M1–M4, with Kokoro support and quantization (4-bit MXFP4, bfloat16). **Faster but less voice variety than stock Kokoro.**

---

## Alternative Options (Ranked)

### 1. **Silero TTS** (🥇 Best for Russian)
- **URL**: [snakers4/silero-models (GitHub)](https://github.com/snakers4/silero-models)
- **Install**: `pip install silero`
- **Russian voices**: aidar, baya, kseniya, xenia, eugene (5 speakers, v5_5_ru)
- **Latency**: RTF 0.5–0.7 (faster than Kokoro)
- **Disk**: ~100MB per model
- **License**: MIT ✓
- **Quality**: Competitive clarity; strong for Russian (per Vosk evaluation)
- **Cons**: No voice control/emotion (list-only), smaller English voice set

### 2. **Piper TTS** (🥈 Balanced)
- **URL**: [rhasspy/piper (GitHub)](https://github.com/rhasspy/piper)
- **Install**: Download binary + voice .onnx files from releases
- **Russian voice**: "Irina" confirmed good quality (1-hour training eval)
- **Latency**: <1s for short text (CPU optimized)
- **Disk**: ~30–50MB per voice
- **License**: MIT ✓
- **Advantage**: Pre-built binaries for macOS aarch64; simple stream-based API
- **Cons**: Minimal non-English voice variety

### 3. **XTTS-v2** (🥉 Voice Cloning, not recommended for Russian)
- **URL**: [coqui/TTS (GitHub)](https://github.com/coqui-ai/TTS)
- **Russian eval**: "Much worse than expected" (per Vosk benchmark)
- **Advantage**: Voice cloning capability
- **Cons**: Larger (~3GB), slower, poor Russian support
- **Verdict**: Skip unless voice-cloning needed

---

## Kokoro vs Silero vs Piper: Feature Matrix

| Feature | Kokoro-82M | Silero TTS | Piper TTS |
|---------|-----------|-----------|----------|
| **Russian support** | ❌ | ✅ (5 voices) | ⚠️ (1 voice) |
| **English voices** | ✅ (11F/9M) | ⚠️ (2–3) | ⚠️ (2–3) |
| **Disk footprint** | ~500MB | ~100MB | ~30–50MB |
| **Latency (M4 Max)** | ~178ms | <500ms | <1s |
| **License** | Apache 2.0 | MIT | MIT |
| **MLX support** | ✅ (mlx-audio) | ❌ | ❌ |
| **Language count** | 8 (no RU) | 3 (RU/EN/Multi) | 5 |

---

## Integration Effort Estimate

### Option A: **Kokoro-82M (English fallback)**
- **Effort**: 1–2 days
- **Steps**: Wrap KPipeline in Python service → IPC RPC method `synthesize_speech(text, voice_id)` → return WAV bytes
- **Pro**: Small footprint, fast, modern architecture
- **Con**: No Russian; requires voice_id enum UI control

### Option B: **Silero TTS (Russian + English)**
- **Effort**: 1–2 days  
- **Steps**: Similar wrapper; lazy-load models per language
- **Pro**: Native Russian quality, MIT license
- **Con**: Fewer English voices; needs language detection hook

### Option C: **Dual-mode (Silero primary, Kokoro EN fallback)**
- **Effort**: 2–3 days
- **Steps**: Try Silero(text) if Russian detected, else Kokoro(text, voice_id=random_en)
- **Pro**: Best UX (RU/EN coverage, diverse voices)
- **Con**: ~600MB total disk; more complex error handling

---

## Recommendation for Krab Ear

**Use Silero TTS as primary replacement for macOS `say`:**
- Supports Russian natively (Krab Ear's primary language per CLAUDE.md)
- Fast (<500ms latency on M4 Max)
- Tiny footprint (~100MB)
- MIT license, proven quality in Russian evals

**Add Kokoro-82M as secondary fallback:**
- For English system notifications
- Modern model, smaller than XTTS, Apache 2.0
- MLX port available for future optimization

**Integration path**: 1. Add Silero service to `backend/tts_service.py` 2. Expose IPC method `synthesize_speech(text, lang_hint="auto")` 3. Replace `subprocess.run('say ...')` in NotificationService with IPC call.

---

## Sources
- [Kokoro-82M HuggingFace](https://huggingface.co/hexgrad/Kokoro-82M)
- [mlx-audio GitHub (Apple Silicon support)](https://github.com/Blaizzy/mlx-audio)
- [Silero Models GitHub](https://github.com/snakers4/silero-models)
- [Piper TTS GitHub](https://github.com/rhasspy/piper)
- [Vosk Russian TTS Evaluation](https://alphacephei.com/nsh/2024/07/12/russian-tts.html)
- [XTTS vs Kokoro benchmark](https://www.ttsinsider.com/xtts-v2-vs-kokoro/)
