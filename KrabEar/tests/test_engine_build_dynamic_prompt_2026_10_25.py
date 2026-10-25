"""Характеризация AudioEngine._build_dynamic_prompt (вынос из transcribe, поведение прежнее)."""
from unittest.mock import patch

from core.config import settings
from core.engine import AudioEngine


def _engine():
    return AudioEngine.__new__(AudioEngine)


def _build(eng, **over):
    kw = dict(
        is_preview=False, context_free=False, domain="casual", extra_vocabulary=None,
        history_context=None, stt_hotwords=None, resolved_lang="ru", diarize=False,
    )
    kw.update(over)
    return eng._build_dynamic_prompt(object(), **kw)


def test_preview_and_context_free_give_empty_prompt():
    eng = _engine()
    assert _build(eng, is_preview=True) == ""
    assert _build(eng, context_free=True) == ""


def test_base_prompt_has_instruction_and_domain():
    eng = _engine()
    out = _build(eng)
    assert out.startswith(f"{settings.TRANSCRIBE_PROMPT} Тематика: ") or settings.TRANSCRIBE_PROMPT in out
    assert AudioEngine.DOMAIN_PROMPTS["casual"] in out


def test_unknown_domain_falls_back_to_casual():
    eng = _engine()
    assert AudioEngine.DOMAIN_PROMPTS["casual"] in _build(eng, domain="no_such_domain")


def test_extra_vocabulary_appended():
    eng = _engine()
    assert "Ключевые слова: альфа, бета" in _build(eng, extra_vocabulary=["альфа", "бета"])


def test_context_suffix_is_prepended():
    eng = _engine()
    with patch("core.engine.build_initial_prompt", return_value="ПРЕФИКС"):
        out = _build(eng)
    assert out.startswith("ПРЕФИКС ")


def test_speaker_hint_added_when_enough_speakers():
    eng = _engine()
    eng._estimate_num_speakers = lambda audio, cache=None: 3
    eng._build_speaker_context_prompt = lambda n, lang: "HINT"
    with patch.object(settings, "STT_SPEAKER_AWARE_PROMPT_ENABLED", True), \
            patch.object(settings, "STT_DIALOGUE_HINT_THRESHOLD", 2):
        out = _build(eng, diarize=True)
    assert out.endswith("\nHINT")


def test_speaker_estimation_failure_is_swallowed():
    eng = _engine()

    def boom(audio, cache=None):
        raise RuntimeError("x")

    eng._estimate_num_speakers = boom
    with patch.object(settings, "STT_SPEAKER_AWARE_PROMPT_ENABLED", True):
        out = _build(eng, diarize=True)
    assert "HINT" not in out and out
