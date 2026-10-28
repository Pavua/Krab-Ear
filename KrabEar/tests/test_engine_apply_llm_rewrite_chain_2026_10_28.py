"""Характеризация AudioEngine._apply_llm_rewrite_chain (вынос из transcribe, поведение прежнее)."""
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

from core.engine import AudioEngine


class _Rewriter:
    def __init__(self, rewrite_result=None, punct=None, raise_on_rewrite=None):
        self._rewrite_result = rewrite_result
        self._punct = punct
        self._raise = raise_on_rewrite
        self._last_error = None
        self.calls = []

    def fix_punctuation_only(self, text, language=None):
        self.calls.append(("punct", text, language))
        return self._punct

    def rewrite(self, text):
        self.calls.append(("rewrite", text))
        if self._raise is not None:
            raise self._raise
        return self._rewrite_result


def _ok(text, latency=12):
    return SimpleNamespace(ok=True, text=text, fallback_reason=None, latency_ms=latency)


def _fail(reason="timeout", latency=7):
    return SimpleNamespace(ok=False, text="x", fallback_reason=reason, latency_ms=latency)


def _engine(rewriter, **flags):
    eng = AudioEngine.__new__(AudioEngine)
    eng._llm_rewriter = rewriter
    eng._last_llm_diff = None
    settings = {"llm_rewrite_enabled": False, "stt_punctuation_llm_pass_enabled": False,
                "cloud_rewriter_enabled": False, "privacy_mode_enabled": False}
    settings.update(flags)
    eng._settings_get = lambda key, default=None: settings.get(key, default)
    return eng


def _run(eng, text="т", cleaned="ч", native=False, loop=False, lang="ru"):
    reports = []
    out = eng._apply_llm_rewrite_chain(
        text, cleaned, native_punctuation=native, is_loop=loop,
        resolved_lang=lang, report=reports.append,
    )
    return out, reports


def test_everything_off_is_passthrough_without_calls():
    rw = _Rewriter()
    (text, llm_result, llm_diff), reports = _run(_engine(rw))
    assert (text, llm_result, llm_diff) == ("т", None, None)
    assert reports == [] and rw.calls == []
    assert _engine(rw)._last_llm_diff is None


def test_no_rewriter_object_is_passthrough():
    (text, llm_result, llm_diff), reports = _run(_engine(None, llm_rewrite_enabled=True))
    assert (text, llm_result, llm_diff, reports) == ("т", None, None, [])


def test_punctuation_pass_replaces_text_and_keeps_cleaned_for_rewrite():
    rw = _Rewriter(punct="т.", rewrite_result=_ok("итог"))
    eng = _engine(rw, stt_punctuation_llm_pass_enabled=True, llm_rewrite_enabled=True)
    (text, llm_result, llm_diff), reports = _run(eng)
    assert reports == ["punctuation_pass", "llm_rewrite"]
    assert rw.calls == [("punct", "т", "ru"), ("rewrite", "ч")]
    assert text == "итог" and llm_result.ok and llm_diff is not None
    assert eng._last_llm_diff is llm_diff


def test_punctuation_pass_rejected_keeps_original_text():
    rw = _Rewriter(punct=None)
    eng = _engine(rw, stt_punctuation_llm_pass_enabled=True)
    (text, llm_result, _), reports = _run(eng)
    assert text == "т" and llm_result is None and reports == ["punctuation_pass"]


def test_native_punctuation_and_loop_skip_punctuation_pass():
    for kw in ({"native": True}, {"loop": True}):
        rw = _Rewriter(punct="не должно")
        eng = _engine(rw, stt_punctuation_llm_pass_enabled=True)
        (text, _, _), reports = _run(eng, **kw)
        assert text == "т" and reports == [] and rw.calls == []


def test_loop_skips_rewrite():
    rw = _Rewriter(rewrite_result=_ok("итог"))
    eng = _engine(rw, llm_rewrite_enabled=True)
    (text, llm_result, _), reports = _run(eng, loop=True)
    assert text == "т" and llm_result is None and reports == [] and rw.calls == []


def test_privacy_mode_blocks_both_llm_stages():
    rw = _Rewriter(punct="p", rewrite_result=_ok("итог"))
    eng = _engine(rw, privacy_mode_enabled=True, llm_rewrite_enabled=True,
                  stt_punctuation_llm_pass_enabled=True)
    (text, llm_result, _), reports = _run(eng)
    assert text == "т" and llm_result is None and rw.calls == [] and reports == []


def test_rewrite_ok_uses_cleaned_text_as_input_and_records_diff():
    rw = _Rewriter(rewrite_result=_ok("новый текст", latency=33))
    eng = _engine(rw, llm_rewrite_enabled=True)
    (text, llm_result, llm_diff), reports = _run(eng, text="после пост-обработки", cleaned="чистый")
    assert rw.calls == [("rewrite", "чистый")]
    assert text == "новый текст" and llm_result.latency_ms == 33
    assert reports == ["llm_rewrite"]
    assert llm_diff.similarity_ratio is not None
    assert eng._last_llm_diff is llm_diff


def test_rewrite_guard_rejection_keeps_text_and_returns_failed_result():
    # chatbot/ratio guard живёт в LLMRewriter и приходит как ok=False
    rw = _Rewriter(rewrite_result=_fail("chatbot_response"))
    eng = _engine(rw, llm_rewrite_enabled=True)
    (text, llm_result, llm_diff), _ = _run(eng)
    assert text == "т" and llm_result.fallback_reason == "chatbot_response"
    assert llm_diff is None and eng._last_llm_diff is None


def test_rewriter_exception_propagates_unchanged():
    # Прежнее поведение: исключение rewriter не гасится в этом блоке.
    rw = _Rewriter(raise_on_rewrite=RuntimeError("boom"))
    eng = _engine(rw, llm_rewrite_enabled=True)
    try:
        _run(eng)
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("исключение должно подняться")


def _fake_cloud(returns=None, raises=None, calls=None):
    mod = types.ModuleType("backend.cloud_rewriter")

    def cloud_rewrite(text, lang):
        calls.append((text, lang))
        if raises:
            raise raises
        return returns

    mod.cloud_rewrite = cloud_rewrite
    return mod


def test_cloud_fallback_only_when_studio_down_and_allowed():
    calls = []
    rw = _Rewriter(rewrite_result=_fail("timeout"))
    eng = _engine(rw, llm_rewrite_enabled=True, cloud_rewriter_enabled=True)
    with patch.dict(sys.modules, {"backend.cloud_rewriter": _fake_cloud("облако", calls=calls)}), \
         patch("backend.llm_rewriter.is_studio_unavailable", return_value=True), \
         patch("backend.privacy_audit.get_privacy_audit_logger") as audit:
        (text, llm_result, llm_diff), _ = _run(eng)
    assert calls == [("ч", "ru")]
    assert text == "облако" and llm_result.ok is False and llm_diff is not None
    assert eng._last_llm_diff is llm_diff
    audit.return_value.log_event.assert_called_once()
    assert audit.return_value.log_event.call_args.kwargs["action"] == "cloud_rewrite_used"


def test_cloud_fallback_skipped_when_studio_reachable_or_disallowed():
    for studio_down, allowed in ((False, True), (True, False)):
        calls = []
        rw = _Rewriter(rewrite_result=_fail("empty_catalog"))
        eng = _engine(rw, llm_rewrite_enabled=True, cloud_rewriter_enabled=allowed)
        with patch.dict(sys.modules, {"backend.cloud_rewriter": _fake_cloud("облако", calls=calls)}), \
             patch("backend.llm_rewriter.is_studio_unavailable", return_value=studio_down):
            (text, _, llm_diff), _ = _run(eng)
        assert text == "т" and calls == [] and llm_diff is None


def test_cloud_error_and_empty_result_are_ignored():
    for kw in ({"returns": ""}, {"raises": ValueError("net")}):
        calls = []
        rw = _Rewriter(rewrite_result=_fail("timeout"))
        eng = _engine(rw, llm_rewrite_enabled=True, cloud_rewriter_enabled=True)
        with patch.dict(sys.modules, {"backend.cloud_rewriter": _fake_cloud(calls=calls, **kw)}), \
             patch("backend.llm_rewriter.is_studio_unavailable", return_value=True):
            (text, llm_result, llm_diff), _ = _run(eng)
        assert text == "т" and calls == [("ч", "ru")]
        assert llm_result.ok is False and llm_diff is None


def test_cloud_audit_failure_never_breaks_transcription():
    calls = []
    rw = _Rewriter(rewrite_result=_fail("timeout"))
    eng = _engine(rw, llm_rewrite_enabled=True, cloud_rewriter_enabled=True)
    with patch.dict(sys.modules, {"backend.cloud_rewriter": _fake_cloud("облако", calls=calls)}), \
         patch("backend.llm_rewriter.is_studio_unavailable", return_value=True), \
         patch("backend.privacy_audit.get_privacy_audit_logger", side_effect=OSError("disk")):
        (text, _, _), _ = _run(eng)
    assert text == "облако"
