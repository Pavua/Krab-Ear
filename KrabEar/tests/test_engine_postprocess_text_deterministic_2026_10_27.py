"""Характеризация AudioEngine._postprocess_text_deterministic (вынос из transcribe, поведение прежнее)."""
from contextlib import ExitStack
from unittest.mock import patch

from core.config import settings
from core.engine import AudioEngine


class _Rec:
    """Общий журнал вызовов фейковых стадий."""

    def __init__(self):
        self.calls = []


def _stage(rec, name, suffix=""):
    class _Fake:
        def __init__(self, **kw):
            rec.calls.append((name, "init", sorted(kw)))
            self._kw = kw

        def _apply(self, text, **kw):
            rec.calls.append((name, "run", kw))
            return text + suffix

        process = normalize = expand = correct = _apply

    return _Fake


def _run(eng, rec, raw="сырой", lang="ru", profile="soft", suffixes=None,
         number=False, datetime_=False, cleanup_to="чистый"):
    suffixes = suffixes or {}
    reports = []
    with ExitStack() as st:
        st.enter_context(patch("core.engine.TextUtils.cleanup_transcript",
                               side_effect=lambda t, profile: cleanup_to))
        st.enter_context(patch("core.voice_commands.VoiceCommandProcessor",
                               _stage(rec, "voice", suffixes.get("voice", ""))))
        st.enter_context(patch("core.text_snippet_expander.TextSnippetExpander",
                               _stage(rec, "snippets", suffixes.get("snippets", ""))))
        st.enter_context(patch("core.phonetic_vocabulary.PhoneticVocabulary",
                               _stage(rec, "phonetic", suffixes.get("phonetic", ""))))
        st.enter_context(patch("core.number_normalizer.NumberNormalizer",
                               _stage(rec, "number", suffixes.get("number", ""))))
        st.enter_context(patch("core.datetime_normalizer.DateTimeNormalizer",
                               _stage(rec, "datetime", suffixes.get("datetime", ""))))
        st.enter_context(patch.object(settings, "NUMBER_NORMALIZATION_ENABLED", number))
        st.enter_context(patch.object(settings, "DATETIME_NORMALIZATION_ENABLED", datetime_))
        out = eng._postprocess_text_deterministic(
            raw, cleanup_profile=profile, resolved_lang=lang, report=reports.append,
        )
    return out, reports


def _engine(settings_get=None, snippets=None, phonetic=None):
    eng = AudioEngine.__new__(AudioEngine)
    eng._settings_get = settings_get or (lambda key, default=None: default)
    if snippets is not None:
        eng._snippets_provider = snippets
    if phonetic is not None:
        eng._phonetic_provider = phonetic
    return eng


def test_all_optional_stages_off_returns_cleaned_text_twice():
    rec = _Rec()
    out, reports = _run(_engine(), rec)
    assert out == ("чистый", "чистый")
    assert reports == ["cleanup"]
    assert [c[0] for c in rec.calls] == ["voice", "voice"]  # только init+run голосовых команд


def test_stage_order_text_chain_and_reports():
    rec = _Rec()
    eng = _engine(snippets=lambda: [], phonetic=lambda: [])
    out, reports = _run(
        eng, rec, number=True, datetime_=True,
        suffixes={"voice": "+v", "snippets": "+s", "phonetic": "+p", "number": "+n", "datetime": "+d"},
    )
    assert out == ("чистый", "чистый+v+s+p+n+d")
    assert reports == [
        "cleanup", "voice_commands", "text_snippets", "phonetic_vocab",
        "number_normalization", "datetime_normalization",
    ]
    runs = [c[0] for c in rec.calls if c[1] == "run"]
    assert runs == ["voice", "snippets", "phonetic", "number", "datetime"]


def test_unchanged_stage_does_not_report():
    rec = _Rec()
    eng = _engine(snippets=lambda: [])
    out, reports = _run(eng, rec, suffixes={"voice": "+v"})
    assert out == ("чистый", "чистый+v")
    assert reports == ["cleanup", "voice_commands"]


def test_providers_are_passed_to_stages_and_missing_ones_skip():
    rec = _Rec()
    sentinel = object()
    eng = _engine(snippets=sentinel)
    _run(eng, rec)
    inits = {c[0]: c[2] for c in rec.calls if c[1] == "init"}
    assert inits["snippets"] == ["settings_get", "snippets_provider"]
    assert "phonetic" not in inits


def test_runtime_toggle_enables_number_normalization_when_static_is_off():
    rec = _Rec()
    eng = _engine(settings_get=lambda key, default=None: True if key == "number_normalization_enabled" else default)
    out, reports = _run(eng, rec, suffixes={"number": "+n"})
    assert out[1] == "чистый+n"
    assert "number_normalization" in reports
    assert "datetime_normalization" not in reports


def test_language_falls_back_to_settings_when_not_resolved():
    rec = _Rec()
    with patch.object(settings, "TRANSCRIBE_LANGUAGE", "es"):
        _run(_engine(), rec, lang=None, number=True)
    langs = [c[2].get("language") for c in rec.calls if c[1] == "run"]
    assert langs == ["es", "es"]


def test_cleanup_profile_and_raw_text_reach_text_utils():
    rec = _Rec()
    eng = _engine()
    with patch("core.engine.TextUtils.cleanup_transcript", return_value="x") as cleanup, \
            patch("core.voice_commands.VoiceCommandProcessor", _stage(rec, "voice")):
        eng._postprocess_text_deterministic(
            "сырой", cleanup_profile="strict", resolved_lang="ru", report=lambda s: None,
        )
    cleanup.assert_called_once_with("сырой", profile="strict")
