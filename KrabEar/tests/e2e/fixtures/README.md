# E2E Test Fixtures Directory

Welcome to the Phase 1.8 acceptance test fixtures. This directory contains real (or synthesized) audio samples used to validate Krab Ear's speech recognition, language detection, emotion analysis, and robustness features.

## Quick Start

### Directory Structure

```
KrabEar/tests/e2e/fixtures/
├── README.md                         (this file)
├── .gitkeep                          (git placeholder)
├── fixtures_spec.md                  (detailed spec: all 33 clips defined)
├── ru_01_greeting.wav                (Russian fixture #1)
├── ru_01_greeting.json               (metadata for ru_01)
├── ru_02_weather.wav
├── ru_02_weather.json
├── ... (30 more fixtures)
├── edge_01_silence.wav
└── edge_01_silence.json
```

### What's Included in This Bootstrap

- **Spec document** (`fixtures_spec.md`): Complete matrix of 33 test cases across 6 groups
- **Empty directory**: Ready for Phase 1.8 agent to populate
- **Metadata template**: JSON schema for each fixture

### What's NOT Included Yet

- Actual `.wav` files (Phase 1.8 implementation generates them)
- Test runner (Phase 1.8 implements E2E test framework)

---

## For Phase 1.8 Agent: How to Populate

### Step 1: Generate Audio Fixtures

Each clip can be created via **TTS synthesis** (recommended for bootstrap) or **hand-recording**:

#### Option A: TTS Bootstrap (Fast & Reproducible)

```bash
#!/bin/bash
cd KrabEar/tests/e2e/fixtures

# Russian fixtures (using macOS Milena voice)
say -v "Milena" "Привет, как дела?" -o temp.aiff && \
  ffmpeg -i temp.aiff -acodec pcm_s16le -ar 16000 -ac 1 ru_01_greeting.wav && \
  rm temp.aiff

say -v "Milena" "На улице холодно, пойду одеваться." -o temp.aiff && \
  ffmpeg -i temp.aiff -acodec pcm_s16le -ar 16000 -ac 1 ru_02_weather.wav && \
  rm temp.aiff

# (... repeat for all 33 clips)
```

**Advantages**:
- ✅ Instant (< 5 min for all 33 clips)
- ✅ 100% reproducible (same output every run)
- ✅ Consistent acoustic characteristics
- ✅ No manual recording needed

**Disadvantages**:
- ❌ Synthetic accent (not real human variation)
- ❌ Emotion detection less challenging

#### Option B: Hand-Recorded Fixtures (More Realistic)

Record each clip with a USB microphone in a quiet environment:

```bash
# Example: record ru_01_greeting.wav manually in Audacity or QuickTime
# 1. Open Audacity
# 2. Microphone > Record audio
# 3. Speak: "Привет, как дела?"
# 4. Export > WAV, 16 kHz mono
# 5. Save as ru_01_greeting.wav
```

**Advantages**:
- ✅ Real human speech variation
- ✅ Natural emotion + prosody
- ✅ Realistic acoustic conditions

**Disadvantages**:
- ❌ Time-consuming (1–2 hours for 33 clips)
- ❌ Less reproducible (audio variation across takes)

#### Option C: Mixed Approach (Recommended)

- Monolingual fixtures (RU_01–ES_10): TTS for speed
- Long-form fixtures (LONG_01): Hand-recorded for realism
- Code-switching (MIX_01): Spliced from TTS or hand-recorded
- Edge cases (EDGE_01): Generated (silence)

### Step 2: Create Metadata Files

For each `.wav`, create a `.json` sidecar with metadata:

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
  "created_by": "phase-1.8-agent",
  "acceptance_criteria": ["AC1", "AC4", "AC7"],
  "tags": ["baseline", "short-form", "friendly"]
}
```

### Step 3: Validate Format

Verify all `.wav` files conform to spec (16 kHz, mono, 16-bit):

```bash
#!/bin/bash
for wav in *.wav; do
  echo "Checking $wav:"
  ffprobe -v error -show_entries stream=sample_rate,channels,duration -of csv=p=0 "$wav"
  # Expected: 16000,1,<duration>
done
```

### Step 4: Update Fixture Index

After populating all 33 clips, document in test runner:

```python
# KrabEar/tests/e2e/conftest.py or similar
import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures"

def load_all_fixtures():
    """Load all .wav fixtures and their metadata."""
    fixtures = {}
    for json_file in FIXTURE_DIR.glob("*.json"):
        with open(json_file) as f:
            meta = json.load(f)
            fixture_id = meta["fixture_id"]
            fixtures[fixture_id] = {
                "metadata": meta,
                "wav_path": FIXTURE_DIR / meta["filename"]
            }
    return fixtures
```

---

## For Test Writers: Using Fixtures

Example parametrized test:

```python
import pytest
from pathlib import Path
import json

@pytest.mark.e2e
class TestPhase1Acceptance:
    
    @pytest.fixture(scope="session")
    def fixtures(self):
        """Load all fixtures from directory."""
        fixture_dir = Path(__file__).parent / "fixtures"
        fixtures = {}
        for json_file in fixture_dir.glob("*.json"):
            with open(json_file) as f:
                meta = json.load(f)
                fixtures[meta["fixture_id"]] = {
                    "metadata": meta,
                    "wav_path": fixture_dir / meta["filename"]
                }
        return fixtures
    
    @pytest.mark.parametrize("fixture_group", [
        "RU_01", "RU_02", "RU_03", "RU_04", "RU_05",
        "RU_06", "RU_07", "RU_08", "RU_09", "RU_10",
    ])
    def test_russian_stt_accuracy(self, fixture_group, fixtures, backend_service):
        """Test Russian STT against Group 1 fixtures."""
        fixture = fixtures[fixture_group]
        wav_path = fixture["wav_path"]
        metadata = fixture["metadata"]
        
        # Transcribe audio
        result = backend_service.transcribe_file(
            wav_path,
            language="ru"
        )
        
        # Validate against reference
        reference = metadata["transcript_reference"]
        wer = calculate_wer(result["transcript"], reference)
        
        assert wer >= metadata["expected_wer_threshold"], \
            f"{fixture_group}: WER {wer:.2f} < {metadata['expected_wer_threshold']}"
    
    def test_phase1_acceptance_coverage(self, fixtures):
        """Verify all 10 acceptance criteria have fixture coverage."""
        ac_coverage = {f"AC{i}": False for i in range(1, 11)}
        
        for fixture in fixtures.values():
            for ac in fixture["metadata"].get("acceptance_criteria", []):
                ac_coverage[ac] = True
        
        uncovered = [ac for ac, covered in ac_coverage.items() if not covered]
        assert not uncovered, f"Uncovered ACs: {uncovered}"
```

---

## Maintenance & Refresh

### When to Regenerate Fixtures

- **Never** if tests are passing consistently (fixtures are stable)
- **Occasionally** if you want to test against real human speech (hand-record new batch)
- **Always** if switching from TTS to human recordings (validate against reference)

### How to Refresh

```bash
# Option: Regenerate all TTS fixtures
cd KrabEar/tests/e2e/fixtures
rm *.wav  # Remove old
for spec in fixtures_spec.md; do
  # Parse spec, synthesize each sentence
  # (script in Phase 1.8 implementation)
done
```

### Fixture Versioning

Track fixture generation method in `.json` sidecar:

```json
{
  "...",
  "generation_method": "tts_say_v1",  // or "hand_recorded", "spliced"
  "tts_voice": "Milena",               // if TTS
  "generated_date": "2026-04-20",
  "generator_version": "phase-1.8-v1"
}
```

---

## Troubleshooting

### `ffmpeg not found`
```bash
brew install ffmpeg
```

### `No speech detected` on fixture X
Check if fixture was properly exported as 16 kHz mono WAV:
```bash
ffprobe -v error -show_entries stream=sample_rate,channels -of csv=p=0 ru_01_greeting.wav
# If output is NOT "16000,1", re-export in Audacity/ffmpeg
```

### Emotion detection unreliable on TTS
TTS voices have limited prosody variation. Consider:
- Hand-record emotion fixtures (RU_06–RU_07, EN_04–EN_05, etc.)
- Lower `expected_accuracy_threshold` for TTS fixtures (0.70 instead of 0.75)
- Document TTS limitation in fixture metadata

### Test fails with "fixture file not found"
Ensure `.json` sidecar `"filename"` field matches actual `.wav` filename exactly (case-sensitive).

---

## File Size Reference

**Expected total size** (all 33 TTS fixtures):
- ~1.5 MB (3–5 sec clips at 16 kHz 16-bit mono ≈ 96–160 KB each)
- Keep under 5 MB in git (no LFS needed for bootstrap)

---

## Phase Ownership

| Phase | Owner | Task | Status |
|---|---|---|---|
| **1.8 Bootstrap** | ← Current PR | Spec + directory | ✅ Complete |
| **1.8 Implementation** | Phase 1.8 agent | Populate .wav + .json | Pending |
| **1.8 Integration** | Phase 1.8 agent | E2E test runners + CI | Pending |

---

## Questions?

Refer to `fixtures_spec.md` for complete fixture matrix, metadata schema, and acceptance criteria mapping. Each fixture group (RU, EN, ES, code-switching, long-form, edge case) is fully documented.
