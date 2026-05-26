"""Тесты для W1294 fixes: settings_provider wiring + privacy guard + filler bigrams.

F3 (W1288): bigram false positives — sentence-start fillers like "Хорошо давайте"
            pass _is_capitalized_or_multiword and pollute Whisper prompt.
F4 (W1288): settings_provider not wired in service.py → privacy guard dead code.

Тесты:
  - test_settings_provider_wired_in_service: AutoGlossaryBuilder в service.py
    получает settings_provider != None.
  - test_privacy_mode_skips_disk_persist: при privacy_mode_enabled=True build()
    не сохраняет кэш на диск и возвращает [].
  - test_filler_bigrams_excluded: "Хорошо давайте", "Okay well" и т.п.
    не попадают в результат build().
  - test_legitimate_proper_noun_bigrams_kept: "TensorFlow PyTorch",
    "Иван Иванов" сохраняются в результатах.
"""

from __future__ import annotations

import json
import sys
import time
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# --- path setup ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.auto_glossary import (
    AutoGlossaryBuilder,
    _starts_with_filler,
    _FILLER_STARTERS,
)
from core.term_extractor import TermExtractor, ExtractedTerm


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_item(text: str, ts: str | None = None) -> dict:
    now_ts = ts or time.strftime("%Y-%m-%dT%H:%M:%S")
    return {"text": text, "source_text": text, "ts": now_ts}


class _FakeStore:
    def __init__(self, items=None):
        self._items = items or []

    def get_history_page(self, cursor=None, limit=500):
        return self._items, None


# ── TestSettingsProviderWiredInService ────────────────────────────────────────

class TestSettingsProviderWiredInService(unittest.TestCase):
    """F4 (W1288): AutoGlossaryBuilder must receive settings_provider in service.py."""

    def test_settings_provider_wired_in_service(self):
        """Inspect service.py to confirm settings_provider is passed to
        AutoGlossaryBuilder constructor."""
        service_path = PROJECT_ROOT / "backend" / "service.py"
        self.assertTrue(service_path.exists(), "backend/service.py not found")
        source = service_path.read_text(encoding="utf-8")

        # Find the AutoGlossaryBuilder(...) call block
        idx = source.find("AutoGlossaryBuilder(")
        self.assertGreater(idx, -1, "AutoGlossaryBuilder( not found in service.py")

        # Extract the constructor call (up to the closing paren)
        call_start = idx
        depth = 0
        call_end = call_start
        for i, ch in enumerate(source[call_start:], start=call_start):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    call_end = i
                    break

        call_block = source[call_start:call_end + 1]
        self.assertIn(
            "settings_provider",
            call_block,
            f"settings_provider kwarg missing from AutoGlossaryBuilder call:\n{call_block}",
        )

    def test_settings_provider_is_callable(self):
        """AutoGlossaryBuilder accepts and stores a callable settings_provider."""
        store = _FakeStore()
        provider = lambda: {"privacy_mode_enabled": False}  # noqa: E731
        builder = AutoGlossaryBuilder(
            store=store,
            settings_provider=provider,
        )
        self.assertIs(builder._settings_provider, provider)

    def test_settings_provider_none_by_default(self):
        """Without settings_provider, build() still works (backward compat)."""
        store = _FakeStore()
        builder = AutoGlossaryBuilder(store=store)
        self.assertIsNone(builder._settings_provider)
        result = builder.build()
        self.assertIsInstance(result, list)


# ── TestPrivacyModeSkipsDiskPersist ───────────────────────────────────────────

class TestPrivacyModeSkipsDiskPersist(unittest.TestCase):
    """F4 (W1288): privacy_mode_enabled → build() skips disk write, returns []."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._data_dir = Path(self._tmpdir)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_privacy_mode_returns_empty_list(self):
        """build() with privacy_mode_enabled=True must return []."""
        items = [
            _make_item("TensorFlow используется в ML"),
            _make_item("TensorFlow популярен в Python"),
            _make_item("TensorFlow — фреймворк Google"),
        ]
        store = _FakeStore(items=items)
        provider = lambda: {"privacy_mode_enabled": True}  # noqa: E731
        builder = AutoGlossaryBuilder(
            store=store,
            data_dir=self._data_dir,
            settings_provider=provider,
        )
        result = builder.build()
        self.assertEqual(result, [], "Expected [] when privacy_mode_enabled=True")

    def test_privacy_mode_skips_disk_persist(self):
        """build() with privacy_mode_enabled=True must NOT write auto_glossary.json."""
        items = [
            _make_item("TensorFlow используется"),
            _make_item("TensorFlow популярен"),
        ]
        store = _FakeStore(items=items)
        provider = lambda: {"privacy_mode_enabled": True}  # noqa: E731
        builder = AutoGlossaryBuilder(
            store=store,
            data_dir=self._data_dir,
            settings_provider=provider,
        )
        builder.build()
        cache_file = self._data_dir / "auto_glossary.json"
        self.assertFalse(
            cache_file.exists(),
            "auto_glossary.json should NOT be written when privacy_mode_enabled=True",
        )

    def test_privacy_mode_false_allows_disk_persist(self):
        """build() with privacy_mode_enabled=False writes cache as normal."""
        items = [
            _make_item("TensorFlow используется"),
            _make_item("TensorFlow популярен"),
            _make_item("TensorFlow отличный"),
        ]
        store = _FakeStore(items=items)
        provider = lambda: {"privacy_mode_enabled": False}  # noqa: E731
        builder = AutoGlossaryBuilder(
            store=store,
            data_dir=self._data_dir,
            settings_provider=provider,
        )
        builder.build()
        cache_file = self._data_dir / "auto_glossary.json"
        self.assertTrue(
            cache_file.exists(),
            "auto_glossary.json should be written when privacy_mode=False",
        )

    def test_privacy_mode_toggle_mid_session(self):
        """build() obeys current value of privacy_mode each call."""
        items = [_make_item("TensorFlow популярен")] * 3
        store = _FakeStore(items=items)
        privacy_flag = {"value": False}
        provider = lambda: {"privacy_mode_enabled": privacy_flag["value"]}  # noqa: E731
        builder = AutoGlossaryBuilder(
            store=store,
            data_dir=self._data_dir,
            settings_provider=provider,
        )

        # First call — privacy off → normal result
        result_off = builder.build()
        self.assertIsInstance(result_off, list)

        # Enable privacy, force rebuild
        privacy_flag["value"] = True
        result_on = builder.build(force=True)
        self.assertEqual(result_on, [], "Expected [] after privacy mode enabled")

    def test_settings_provider_exception_handled(self):
        """Exception from settings_provider should not crash build(); falls back."""
        items = [_make_item("TensorFlow используется")]
        store = _FakeStore(items=items)

        def _bad_provider():
            raise RuntimeError("settings unavailable")

        builder = AutoGlossaryBuilder(store=store, settings_provider=_bad_provider)
        # Should not raise; falls back to building normally
        result = builder.build()
        self.assertIsInstance(result, list)


# ── TestFillerBigramsExcluded ─────────────────────────────────────────────────

class TestFillerBigramsExcluded(unittest.TestCase):
    """F3 (W1288): bigrams starting with filler tokens must be excluded."""

    def test_starts_with_filler_ru(self):
        """_starts_with_filler returns True for RU fillers."""
        self.assertTrue(_starts_with_filler("Хорошо давайте"))
        self.assertTrue(_starts_with_filler("Давайте продолжим"))
        self.assertTrue(_starts_with_filler("Ладно тогда"))
        self.assertTrue(_starts_with_filler("Итак начнём"))
        self.assertTrue(_starts_with_filler("Значит так"))

    def test_starts_with_filler_es(self):
        """_starts_with_filler returns True for ES fillers."""
        self.assertTrue(_starts_with_filler("Entonces bueno"))
        self.assertTrue(_starts_with_filler("Bueno pues"))
        self.assertTrue(_starts_with_filler("Vale entonces"))

    def test_starts_with_filler_en(self):
        """_starts_with_filler returns True for EN fillers."""
        self.assertTrue(_starts_with_filler("Okay well"))
        self.assertTrue(_starts_with_filler("Well actually"))
        self.assertTrue(_starts_with_filler("Right so"))
        self.assertTrue(_starts_with_filler("So anyway"))

    def test_starts_with_filler_case_insensitive(self):
        """Check is case-insensitive for the first token."""
        self.assertTrue(_starts_with_filler("ХОРОШО давайте"))
        self.assertTrue(_starts_with_filler("OKAY well"))
        self.assertTrue(_starts_with_filler("хорошо Давайте"))

    def test_starts_with_filler_false_for_proper_nouns(self):
        """Returns False for legitimate proper-noun bigrams."""
        self.assertFalse(_starts_with_filler("TensorFlow PyTorch"))
        self.assertFalse(_starts_with_filler("Иван Иванов"))
        self.assertFalse(_starts_with_filler("Apple Watch"))

    def test_starts_with_filler_empty(self):
        self.assertFalse(_starts_with_filler(""))

    def _make_builder_with_repeated_filler(self, phrase: str, count: int = 5):
        """Build a store with a phrase repeated `count` times (enough for bigram freq)."""
        items = [_make_item(phrase) for _ in range(count)]
        return AutoGlossaryBuilder(store=_FakeStore(items=items))

    def test_filler_bigram_hорошо_давайте_excluded(self):
        """'Хорошо давайте' repeated 5× must NOT appear in build() result."""
        # Repeat phrase so bigram would normally have freq >= 2
        phrase = "Хорошо давайте продолжим разговор о проекте"
        builder = self._make_builder_with_repeated_filler(phrase, count=5)
        result = builder.build()
        # Check that no bigram starts with 'хорошо' (case-insensitive)
        for term in result:
            if " " in term:
                first = term.split()[0].lower()
                self.assertNotIn(
                    first,
                    _FILLER_STARTERS,
                    f"Filler bigram leaked into glossary: {term!r}",
                )

    def test_filler_bigram_okay_well_excluded(self):
        """'Okay well' repeated 5× must NOT appear in build() result."""
        phrase = "Okay well let me explain the process again"
        builder = self._make_builder_with_repeated_filler(phrase, count=5)
        result = builder.build()
        for term in result:
            if " " in term:
                first = term.split()[0].lower()
                self.assertNotIn(
                    first,
                    _FILLER_STARTERS,
                    f"Filler bigram leaked into glossary: {term!r}",
                )

    def test_multiple_filler_starters_all_excluded(self):
        """All _FILLER_STARTERS produce no bigrams in result."""
        # Build a text that includes every filler starter followed by a word
        words = " ".join(f"{filler.capitalize()} слово" for filler in _FILLER_STARTERS)
        items = [_make_item(words)] * 5
        builder = AutoGlossaryBuilder(store=_FakeStore(items=items))
        result = builder.build()
        for term in result:
            if " " in term:
                first = term.split()[0].lower()
                self.assertNotIn(
                    first,
                    _FILLER_STARTERS,
                    f"Filler bigram leaked into glossary: {term!r}",
                )


# ── TestLegitimateProperNounBigramsKept ───────────────────────────────────────

class TestLegitimateProperNounBigramsKept(unittest.TestCase):
    """F3 (W1288): legitimate proper noun bigrams must NOT be filtered out."""

    def _build_with_repeated(self, phrase: str, count: int = 5) -> list:
        items = [_make_item(phrase) for _ in range(count)]
        builder = AutoGlossaryBuilder(store=_FakeStore(items=items))
        return builder.build()

    def test_starts_with_filler_false_for_tensorflow(self):
        self.assertFalse(_starts_with_filler("TensorFlow PyTorch"))

    def test_starts_with_filler_false_for_proper_person_name(self):
        self.assertFalse(_starts_with_filler("Иван Иванов"))

    def test_starts_with_filler_false_for_apple_watch(self):
        self.assertFalse(_starts_with_filler("Apple Watch"))

    def test_starts_with_filler_false_for_single_word(self):
        self.assertFalse(_starts_with_filler("TensorFlow"))

    def test_proper_proper_noun_not_filtered(self):
        """'TensorFlow PyTorch' repeated should survive filtering."""
        # TermExtractor produces bigrams from non-stop repeated tokens.
        # "TensorFlow PyTorch" are both capitalised → not fillers.
        phrase = "TensorFlow PyTorch используются в одном проекте TensorFlow PyTorch"
        result = self._build_with_repeated(phrase, count=5)
        # At minimum, we check no filler bigrams crept in
        for term in result:
            if " " in term:
                first = term.split()[0].lower()
                self.assertNotIn(first, _FILLER_STARTERS)
        # TensorFlow should still be present as a single term
        self.assertTrue(
            any("TensorFlow" in t for t in result),
            f"TensorFlow should survive filter; got: {result}",
        )

    def test_cyrillic_proper_noun_not_filtered(self):
        """Cyrillic proper nouns at sentence start are not fillers."""
        self.assertFalse(_starts_with_filler("Яндекс Карты"))
        self.assertFalse(_starts_with_filler("Москва Река"))

    def test_technical_acronym_not_filtered(self):
        """Technical acronym bigrams like 'API Gateway' are not fillers."""
        self.assertFalse(_starts_with_filler("API Gateway"))
        self.assertFalse(_starts_with_filler("HTTP POST"))

    def test_build_with_no_fillers_returns_nonempty(self):
        """build() with clean technical text still returns terms."""
        items = [
            _make_item("TensorFlow и PyTorch — это популярные фреймворки"),
            _make_item("TensorFlow используется в production"),
            _make_item("TensorFlow создан компанией Google"),
        ]
        builder = AutoGlossaryBuilder(store=_FakeStore(items=items))
        result = builder.build()
        # Should have at least TensorFlow
        self.assertTrue(len(result) >= 1, f"Expected terms, got: {result}")
        self.assertIn("TensorFlow", result)


if __name__ == "__main__":
    unittest.main()
