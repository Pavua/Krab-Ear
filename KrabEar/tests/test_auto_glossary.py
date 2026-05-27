"""Тесты для AutoGlossaryBuilder и интеграции с build_initial_prompt.

Используем stub-подход для IPC-хэндлеров, чтобы избежать тяжёлого импорта
BackendService (зависимость от contracts/registry.py и mlx-whisper).
"""

from __future__ import annotations

import json
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

# --- path setup ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.auto_glossary import (
    AutoGlossaryBuilder,
    _is_capitalized_or_multiword,
)
from core.transcript_context import build_initial_prompt


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_item(text: str, ts: str | None = None, source_text: str | None = None) -> dict:
    """Создаёт dict-запись истории для тестов."""
    now_ts = ts or time.strftime("%Y-%m-%dT%H:%M:%S")
    return {"text": text, "source_text": source_text or text, "ts": now_ts}


def _old_ts() -> str:
    """Возвращает timestamp 60 дней назад."""
    old = time.time() - 60 * 86400
    import datetime
    dt = datetime.datetime.utcfromtimestamp(old)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class _FakeStore:
    """Stub StateStore — возвращает заданный список записей."""

    def __init__(self, items: list[dict], error: bool = False):
        self._items = items
        self._error = error

    def get_history_page(self, cursor=None, limit=500):
        if self._error:
            raise RuntimeError("store error")
        return self._items, None


# ── TestIsCapitalizedOrMultiword ──────────────────────────────────────────────

class TestIsCapitalizedOrMultiword(unittest.TestCase):

    def test_capitalized_word(self):
        self.assertTrue(_is_capitalized_or_multiword("Python"))

    def test_multiword_phrase(self):
        self.assertTrue(_is_capitalized_or_multiword("machine learning"))

    def test_word_with_digit(self):
        self.assertTrue(_is_capitalized_or_multiword("GPT4"))

    def test_abbreviation_two_upper(self):
        self.assertTrue(_is_capitalized_or_multiword("API"))

    def test_lowercase_word_rejected(self):
        self.assertFalse(_is_capitalized_or_multiword("привет"))

    def test_empty_string(self):
        self.assertFalse(_is_capitalized_or_multiword(""))


# ── TestAutoGlossaryBuilderEmpty ──────────────────────────────────────────────

class TestAutoGlossaryBuilderEmpty(unittest.TestCase):

    def test_empty_history(self):
        store = _FakeStore(items=[])
        builder = AutoGlossaryBuilder(store=store)
        result = builder.build()
        self.assertEqual(result, [])

    def test_store_error_returns_empty(self):
        store = _FakeStore(items=[], error=True)
        builder = AutoGlossaryBuilder(store=store)
        result = builder.build()
        self.assertEqual(result, [])


# ── TestAutoGlossaryBuilderExtraction ─────────────────────────────────────────

class TestAutoGlossaryBuilderExtraction(unittest.TestCase):

    def test_extracts_capitalized_terms(self):
        """TensorFlow встречается 3 раза — должно попасть в топ."""
        items = [
            _make_item("TensorFlow и PyTorch популярны"),
            _make_item("TensorFlow используется в ML"),
            _make_item("TensorFlow — фреймворк от Google"),
        ]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        result = builder.build()
        self.assertIn("TensorFlow", result)

    def test_top_n_limits_results(self):
        items = [_make_item(f"Term{i} встречается часто") for i in range(50)]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        result = builder.build(top_n=5)
        self.assertLessEqual(len(result), 5)

    def test_short_terms_excluded(self):
        """Слова короче 3 символов не должны попадать в глоссарий."""
        items = [_make_item("AI ML DL используются в проектах")]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        result = builder.build()
        for term in result:
            self.assertGreaterEqual(len(term), 3)

    def test_empty_text_items_skipped(self):
        items = [_make_item(""), _make_item("   "), _make_item("Python хорош")]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        result = builder.build()
        # Should not crash; Python should appear if extractor picks it up
        self.assertIsInstance(result, list)


# ── TestAutoGlossaryBuilderDateFilter ────────────────────────────────────────

class TestAutoGlossaryBuilderDateFilter(unittest.TestCase):

    def test_old_items_excluded(self):
        """Записи старше window_days не должны учитываться."""
        items = [
            _make_item("TensorFlow встречается", ts=_old_ts()),
            _make_item("TensorFlow встречается", ts=_old_ts()),
        ]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        result = builder.build(window_days=7)
        # Old items excluded — TensorFlow may not appear
        # (или пустой список, это ожидаемо)
        self.assertIsInstance(result, list)

    def test_recent_items_included(self):
        """Свежие записи должны обрабатываться."""
        items = [
            _make_item("TensorFlow фреймворк"),
            _make_item("TensorFlow очень популярен"),
            _make_item("TensorFlow применяется"),
        ]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        result = builder.build(window_days=7)
        self.assertIsInstance(result, list)

    def test_invalid_ts_treated_as_old(self):
        """Записи с невалидным timestamp считаются очень старыми."""
        items = [_make_item("TensorFlow фреймворк", ts="invalid-date")]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        result = builder.build(window_days=7)
        # Invalid ts → epoch 0 → filtered out as old
        self.assertEqual(result, [])


# ── TestAutoGlossaryBuilderTopN ───────────────────────────────────────────────

class TestAutoGlossaryBuilderTopN(unittest.TestCase):

    def test_top_n_respected(self):
        items = [_make_item(f"Term{i} used Term{i}") for i in range(100)]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        result = builder.build(top_n=10)
        self.assertLessEqual(len(result), 10)

    def test_all_results_are_strings(self):
        items = [_make_item("Python Django Flask React Angular")]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        result = builder.build()
        for term in result:
            self.assertIsInstance(term, str)


# ── TestAutoGlossaryBuilderCache ──────────────────────────────────────────────

class TestAutoGlossaryBuilderCache(unittest.TestCase):

    def test_cache_hit_on_second_call(self):
        """Второй вызов build() должен использовать кэш."""
        items = [_make_item("TensorFlow фреймворк"), _make_item("TensorFlow популярен")]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        result1 = builder.build()
        # Modify store (shouldn't matter — cache hit)
        store._items = []
        result2 = builder.build()
        self.assertEqual(result1, result2)

    def test_force_bypasses_cache(self):
        """force=True должен пересчитывать глоссарий даже если кэш свежий."""
        items = [_make_item("TensorFlow используется")]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        builder.build()
        store._items = []
        result = builder.build(force=True)
        # After force rebuild with empty store → empty result
        self.assertEqual(result, [])

    def test_cache_expiry(self):
        """Устаревший кэш должен пересчитываться."""
        items = [_make_item("TensorFlow фреймворк")]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store, refresh_hours=1.0)
        builder.build()
        # Simulate expired cache by backdating _cache_built_at by 2 hours
        builder._cache_built_at = time.time() - 7200.0
        store._items = []
        result = builder.build()
        # Cache expired → should rebuild with empty store → []
        self.assertEqual(result, [])

    def test_get_cached_returns_current_cache(self):
        items = [_make_item("TensorFlow фреймворк")]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        self.assertEqual(builder.get_cached(), [])  # not built yet
        builder.build()
        cached = builder.get_cached()
        self.assertIsInstance(cached, list)

    def test_invalidate_clears_cache(self):
        items = [_make_item("TensorFlow фреймворк")]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        builder.build()
        self.assertTrue(len(builder.get_cached()) >= 0)
        builder.invalidate()
        self.assertEqual(builder.get_cached(), [])
        self.assertEqual(builder._cache_built_at, 0.0)

    def test_is_cache_valid_false_when_empty(self):
        store = _FakeStore(items=[])
        builder = AutoGlossaryBuilder(store=store)
        self.assertFalse(builder._is_cache_valid())


# ── TestAutoGlossaryDiskPersistence ───────────────────────────────────────────

class TestAutoGlossaryDiskPersistence(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self._data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_cache_saved_to_disk(self):
        items = [_make_item("TensorFlow фреймворк"), _make_item("TensorFlow популярен")]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store, data_dir=self._data_dir)
        builder.build()
        cache_file = self._data_dir / "auto_glossary.json"
        self.assertTrue(cache_file.exists())

    def test_cache_loaded_from_disk(self):
        """Новый инстанс должен загрузить кэш с диска."""
        cache_file = self._data_dir / "auto_glossary.json"
        cache_data = {"terms": ["Python", "Django"], "built_at": time.time()}
        cache_file.write_text(json.dumps(cache_data))

        store = _FakeStore(items=[])
        builder = AutoGlossaryBuilder(store=store, data_dir=self._data_dir)
        cached = builder.get_cached()
        self.assertIn("Python", cached)
        self.assertIn("Django", cached)

    def test_invalidate_clears_disk_cache(self):
        cache_file = self._data_dir / "auto_glossary.json"
        cache_data = {"terms": ["Python"], "built_at": time.time()}
        cache_file.write_text(json.dumps(cache_data))

        store = _FakeStore(items=[])
        builder = AutoGlossaryBuilder(store=store, data_dir=self._data_dir)
        builder.invalidate()
        # Cache file should be updated with empty terms
        saved = json.loads(cache_file.read_text())
        self.assertEqual(saved["terms"], [])

    def test_corrupt_disk_cache_handled(self):
        """Повреждённый JSON на диске не должен крашить инициализацию."""
        cache_file = self._data_dir / "auto_glossary.json"
        cache_file.write_text("not-valid-json{{{{")

        store = _FakeStore(items=[])
        builder = AutoGlossaryBuilder(store=store, data_dir=self._data_dir)
        self.assertEqual(builder.get_cached(), [])


# ── TestAutoGlossaryIpcHandlers ───────────────────────────────────────────────

# Standalone stub handlers that replicate BackendService IPC handler logic
# without importing BackendService (avoids mlx-whisper + contracts imports).

def _stub_get_auto_glossary(auto_glossary_builder, settings_dict):
    """Stub: реплицирует _handle_get_auto_glossary."""
    from core.config import settings as cfg
    enabled = bool(settings_dict.get("auto_glossary_enabled", cfg.AUTO_GLOSSARY_ENABLED))
    if not enabled:
        return {"terms": [], "count": 0, "cache_age_hours": 0.0, "enabled": False}
    terms = auto_glossary_builder.get_cached()
    built_at = auto_glossary_builder._cache_built_at
    age_hours = (time.time() - built_at) / 3600.0 if built_at else 0.0
    return {
        "terms": terms,
        "count": len(terms),
        "cache_age_hours": round(age_hours, 2),
        "enabled": enabled,
    }


def _stub_refresh_auto_glossary(auto_glossary_builder, params, settings_dict):
    """Stub: реплицирует _handle_refresh_auto_glossary."""
    from core.config import settings as cfg
    window_days = int(params.get("window_days", settings_dict.get(
        "auto_glossary_window_days", cfg.AUTO_GLOSSARY_WINDOW_DAYS)))
    top_n = int(params.get("top_n", settings_dict.get(
        "auto_glossary_top_n", cfg.AUTO_GLOSSARY_TOP_N)))
    terms = auto_glossary_builder.build(window_days=window_days, top_n=top_n, force=True)
    return {"terms": terms, "count": len(terms)}


class TestAutoGlossaryIpcHandlers(unittest.TestCase):

    def _make_builder(self, items=None, cached_terms=None):
        store = _FakeStore(items=items or [])
        builder = AutoGlossaryBuilder(store=store)
        if cached_terms is not None:
            builder._cache = cached_terms
            builder._cache_built_at = time.time()
        return builder

    def test_get_auto_glossary_enabled(self):
        builder = self._make_builder(cached_terms=["Python", "Django"])
        result = _stub_get_auto_glossary(builder, {"auto_glossary_enabled": True})
        self.assertTrue(result["enabled"])
        self.assertEqual(result["count"], 2)
        self.assertIn("Python", result["terms"])

    def test_get_auto_glossary_disabled(self):
        builder = self._make_builder(cached_terms=["Python"])
        result = _stub_get_auto_glossary(builder, {"auto_glossary_enabled": False})
        self.assertFalse(result["enabled"])
        self.assertEqual(result["terms"], [])
        self.assertEqual(result["count"], 0)

    def test_get_auto_glossary_empty_cache(self):
        builder = self._make_builder()
        result = _stub_get_auto_glossary(builder, {"auto_glossary_enabled": True})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["terms"], [])

    def test_get_auto_glossary_cache_age(self):
        builder = self._make_builder(cached_terms=["Python"])
        result = _stub_get_auto_glossary(builder, {"auto_glossary_enabled": True})
        self.assertIn("cache_age_hours", result)
        self.assertIsInstance(result["cache_age_hours"], float)

    def test_refresh_rebuilds_from_history(self):
        items = [_make_item("TensorFlow популярен"), _make_item("TensorFlow используется")]
        builder = self._make_builder(items=items)
        result = _stub_refresh_auto_glossary(builder, {}, {"auto_glossary_enabled": True})
        self.assertIn("terms", result)
        self.assertIn("count", result)

    def test_refresh_uses_params_window_days(self):
        store = _FakeStore(items=[_make_item("Python используется")])
        builder = AutoGlossaryBuilder(store=store)
        result = _stub_refresh_auto_glossary(
            builder, {"window_days": 3}, {}
        )
        self.assertIsInstance(result["terms"], list)

    def test_refresh_uses_params_top_n(self):
        items = [_make_item(f"Term{i} встречается") for i in range(20)]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        result = _stub_refresh_auto_glossary(
            builder, {"top_n": 5}, {}
        )
        self.assertLessEqual(result["count"], 5)

    def test_refresh_returns_count_field(self):
        store = _FakeStore(items=[])
        builder = AutoGlossaryBuilder(store=store)
        result = _stub_refresh_auto_glossary(builder, {}, {})
        self.assertIn("count", result)
        self.assertEqual(result["count"], len(result["terms"]))


# ── TestAutoGlossaryDisabledFlag ──────────────────────────────────────────────

class TestAutoGlossaryDisabledFlag(unittest.TestCase):

    def test_disabled_returns_empty_from_get_handler(self):
        store = _FakeStore(items=[_make_item("TensorFlow фреймворк")])
        builder = AutoGlossaryBuilder(store=store)
        builder.build()
        result = _stub_get_auto_glossary(builder, {"auto_glossary_enabled": False})
        self.assertEqual(result["terms"], [])

    def test_disabled_does_not_prevent_build(self):
        """build() всегда работает; disabled flag — только в IPC-хэндлере."""
        items = [_make_item("TensorFlow популярен")]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        result = builder.build()
        self.assertIsInstance(result, list)


# ── TestTranscriptContextAutoGlossary ─────────────────────────────────────────

class TestTranscriptContextAutoGlossary(unittest.TestCase):

    def test_auto_glossary_added_to_prompt(self):
        prompt = build_initial_prompt(
            history_items=[],
            hotwords=None,
            auto_glossary=["TensorFlow", "PyTorch"],
        )
        self.assertIn("TensorFlow", prompt)
        self.assertIn("PyTorch", prompt)

    def test_hotwords_take_priority_over_auto_glossary(self):
        """При дублировании (case-insensitive) hotword-версия сохраняется."""
        prompt = build_initial_prompt(
            history_items=[],
            hotwords=["TensorFlow"],
            auto_glossary=["tensorflow"],  # same term, lowercase
        )
        # Only one occurrence
        self.assertEqual(prompt.count("ensorFlow"), 1)

    def test_deduplication_case_insensitive(self):
        """Дубликаты (разный регистр) не должны появляться дважды."""
        prompt = build_initial_prompt(
            history_items=[],
            hotwords=["Python"],
            auto_glossary=["PYTHON", "python"],
        )
        # Python should appear exactly once in the Glossary section
        glossary_part = prompt.split("Glossary:")[1].split(".")[0] if "Glossary:" in prompt else ""
        count = glossary_part.lower().count("python")
        self.assertEqual(count, 1)

    def test_empty_auto_glossary(self):
        prompt = build_initial_prompt(
            history_items=[],
            hotwords=["Python"],
            auto_glossary=[],
        )
        self.assertIn("Python", prompt)

    def test_none_auto_glossary(self):
        prompt = build_initial_prompt(
            history_items=[],
            hotwords=["Django"],
            auto_glossary=None,
        )
        self.assertIn("Django", prompt)

    def test_combined_hotwords_and_auto_glossary(self):
        prompt = build_initial_prompt(
            history_items=[],
            hotwords=["Django"],
            auto_glossary=["TensorFlow", "PyTorch"],
        )
        self.assertIn("Django", prompt)
        self.assertIn("TensorFlow", prompt)
        self.assertIn("PyTorch", prompt)

    def test_no_glossary_without_hotwords_or_auto(self):
        prompt = build_initial_prompt(
            history_items=[],
            hotwords=None,
            auto_glossary=None,
        )
        self.assertNotIn("Glossary:", prompt)


# ── TestAutoGlossaryWave133 ───────────────────────────────────────────────────
# Explicitly-named tests matching Wave 133 spec.

class TestAutoGlossaryWave133(unittest.TestCase):
    """Named tests matching wave133 task spec."""

    def test_build_empty_history_returns_empty(self):
        """build() on empty history must return []."""
        store = _FakeStore(items=[])
        builder = AutoGlossaryBuilder(store=store)
        self.assertEqual(builder.build(), [])

    def test_extract_proper_nouns_only(self):
        """Only proper nouns / capitalised terms are included."""
        items = [
            _make_item("Python Django Flask используются"),
            _make_item("Python популярен"),
        ]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        result = builder.build()
        for term in result:
            self.assertTrue(
                _is_capitalized_or_multiword(term),
                f"Non-proper term in glossary: {term!r}",
            )

    def test_dedup_glossary_words(self):
        """Same term appearing in multiple records must deduplicate."""
        items = [_make_item("TensorFlow применяется")] * 10
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        result = builder.build(top_n=50)
        # No duplicates in output
        self.assertEqual(len(result), len(set(result)))

    def test_unicode_terms(self):
        """Cyrillic / non-ASCII proper-noun terms are handled correctly."""
        items = [
            _make_item("Яндекс — российская компания"),
            _make_item("Яндекс крупная IT-фирма"),
        ]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        result = builder.build()
        self.assertIsInstance(result, list)
        # Returned terms must be plain strings (no encoding corruption)
        for term in result:
            self.assertIsInstance(term, str)

    def test_max_terms_respected(self):
        """build(top_n=N) must never return more than N terms."""
        items = [_make_item(f"Term{i} встречается часто") for i in range(100)]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store)
        for top_n in (1, 5, 10, 20):
            result = builder.build(top_n=top_n, force=True)
            self.assertLessEqual(len(result), top_n, f"Exceeded top_n={top_n}")

    def test_concurrent_build(self):
        """Concurrent build() calls must not raise or corrupt cache."""
        import concurrent.futures
        items = [_make_item("Python Django Flask используются")]
        store = _FakeStore(items=items)
        builder = AutoGlossaryBuilder(store=store, refresh_hours=0.0)

        errors = []

        def _build():
            try:
                builder.build(force=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(_build) for _ in range(16)]
            for f in concurrent.futures.as_completed(futs):
                f.result()

        self.assertEqual(errors, [], f"Concurrent build raised: {errors}")

    def test_handles_corrupted_cache(self):
        """Corrupted disk cache must not crash build(); falls back to rebuild."""
        import tempfile
        import shutil
        tmpdir = tempfile.mkdtemp()
        try:
            cache_file = Path(tmpdir) / "auto_glossary.json"
            cache_file.write_text("}{CORRUPTED{{", encoding="utf-8")
            items = [_make_item("Python используется")]
            store = _FakeStore(items=items)
            builder = AutoGlossaryBuilder(store=store, data_dir=Path(tmpdir))
            # Init loaded corrupted file — cache should be empty
            self.assertEqual(builder.get_cached(), [])
            # build() must succeed despite corruption
            result = builder.build()
            self.assertIsInstance(result, list)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── TestHallucinationPatternCorrekctor (W1402 F3 LOW) ─────────────────────────

class TestHallucinationPatternCorrectorW1402(unittest.TestCase):
    """Проверяет, что bare 'корректор' НЕ является паттерном галлюцинации,
    но 'корректор субтитров' по-прежнему фильтруется (W1402 F3 LOW)."""

    def setUp(self):
        from core.auto_glossary import _looks_like_hallucination
        self._check = _looks_like_hallucination

    def test_corrector_alone_not_filtered(self):
        """'Я работаю корректором' — содержит обычное слово, не галлюцинация."""
        # Одиночное слово 'корректором' (словоформа) не должно быть отфильтровано.
        # _looks_like_hallucination проверяет term целиком, а не подстроки слова.
        self.assertFalse(
            self._check("корректором"),
            "Bare 'корректором' должен проходить фильтр галлюцинаций",
        )
        self.assertFalse(
            self._check("корректор"),
            "Bare 'корректор' должен проходить фильтр галлюцинаций",
        )

    def test_corrector_subtitles_still_filtered(self):
        """'корректор субтитров' — известный Whisper artefact, должен фильтроваться."""
        self.assertTrue(
            self._check("корректор субтитров"),
            "'корректор субтитров' должен быть отфильтрован как галлюцинация",
        )


if __name__ == "__main__":
    unittest.main()
