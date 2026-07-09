"""Unit tests for wake_word_models/train_krab.py — ТОЛЬКО чистые хелперы.

Волна 3a, T1 (см. docs/superpowers/specs/2026-07-09-wake-model-krab-training-
design.md). Кастомная русская wake-word модель «Краб» (openWakeWord) --
тренировочный CLI живёт вне ``KrabEar/`` (в ``wake_word_models/``), поэтому
путь до него подмешивается в ``sys.path`` так же, как это делают тесты
``scripts/*.py`` аудит-инструментов (см. test_audit_dead_extracted_modules.py).

Эти тесты НЕ трогают сеть/GPU/torch/openwakeword -- только чистую логику
(генерация вариаций текста, resume-маркеры, argparse, списки слов). Скрипт
``train_krab.py`` обязан импортироваться без побочных эффектов и без
торч/opewakeword-стека в окружении (mlx-masking-класс урок из CLAUDE.md) --
это проверяется явным AST-стражем ниже и неявно самим фактом того, что этот
файл гоняется на ubuntu-parity py3.12 без обучающего стека.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_wake_training_helpers_W3a.py -v
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup -- добавляем КОРЕНЬ репозитория и wake_word_models/ в sys.path
# (см. test_audit_dead_extracted_modules.py для того же паттерна с scripts/).
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WAKE_MODELS_DIR = os.path.join(PROJECT_ROOT, "wake_word_models")
for _p in (PROJECT_ROOT, WAKE_MODELS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import train_krab as tk  # noqa: E402

TRAIN_KRAB_PATH = Path(WAKE_MODELS_DIR) / "train_krab.py"


# ---------------------------------------------------------------------------
# Статический страж: модуль не должен тянуть тяжёлый ML-стек на уровне импорта
# ---------------------------------------------------------------------------

class NoHeavyTopLevelImportsTest(unittest.TestCase):
    """mlx-masking-класс регрессионный страж (CLAUDE.md): ubuntu-CI не имеет
    torch/openwakeword/numpy/scipy/huggingface_hub -- любой из них на уровне
    модуля (а не внутри функции этапа) уронит импорт на CI."""

    FORBIDDEN_TOP_LEVEL_MODULES = frozenset({
        "torch", "openwakeword", "numpy", "scipy", "huggingface_hub",
    })

    def test_no_forbidden_module_level_imports(self):
        tree = ast.parse(TRAIN_KRAB_PATH.read_text(encoding="utf-8"))
        offenders = []
        for node in tree.body:  # только top-level -- НЕ внутри def/class
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in self.FORBIDDEN_TOP_LEVEL_MODULES:
                        offenders.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                top = (node.module or "").split(".")[0]
                if top in self.FORBIDDEN_TOP_LEVEL_MODULES:
                    offenders.append(node.module)
        self.assertEqual(
            offenders, [],
            f"Тяжёлые импорты на уровне модуля train_krab.py: {offenders} -- "
            "перенесите внутрь функции конкретного этапа (lazy import)",
        )

    def test_module_importable_and_exposes_stages(self):
        # Сам факт того, что этот файл дошёл сюда через `import train_krab`
        # наверху -- уже доказательство отсутствия побочных эффектов при
        # импорте на окружении без torch/openwakeword (см. requirements.txt).
        self.assertEqual(
            tk.STAGES,
            ("corpora", "positives", "negatives", "features", "train", "export", "install"),
        )
        self.assertEqual(set(tk.STAGE_FUNCS.keys()), set(tk.STAGES))


# ---------------------------------------------------------------------------
# Генерация вариаций текста фразы
# ---------------------------------------------------------------------------

class TextVariationTests(unittest.TestCase):
    def test_build_positive_phrases_dedupes_and_preserves_order(self):
        result = tk.build_positive_phrases("Краб", ["эй, Краб", "краб", "Ещё форма"])
        self.assertEqual(result, ["Краб", "эй, Краб", "Ещё форма"])

    def test_build_positive_phrases_empty_phrase_raises(self):
        with self.assertRaises(ValueError):
            tk.build_positive_phrases("   ", [])

    def test_build_positive_phrases_skips_blank_secondary(self):
        result = tk.build_positive_phrases("Краб", ["", "   ", "эй, Краб"])
        self.assertEqual(result, ["Краб", "эй, Краб"])

    def test_normalize_phrase_for_compare_collapses_whitespace_and_case(self):
        self.assertEqual(tk.normalize_phrase_for_compare("  Эй,   Краб \n"), "эй, краб")
        self.assertEqual(tk.normalize_phrase_for_compare(""), "")
        self.assertEqual(tk.normalize_phrase_for_compare(None), "")

    def test_build_ssml_with_rate_and_pitch(self):
        ssml = tk.build_ssml("Краб", 10, -5)
        self.assertEqual(ssml, '<speak><prosody rate="+10%" pitch="-5%">Краб</prosody></speak>')

    def test_build_ssml_no_prosody_omits_attributes(self):
        ssml = tk.build_ssml("Краб", None, None)
        self.assertEqual(ssml, "<speak><prosody>Краб</prosody></speak>")

    def test_build_ssml_escapes_xml_special_chars(self):
        ssml = tk.build_ssml("A & B < C > D \" E ' F", None, None)
        self.assertIn("&amp;", ssml)
        self.assertIn("&lt;", ssml)
        self.assertIn("&gt;", ssml)
        self.assertIn("&quot;", ssml)
        self.assertIn("&apos;", ssml)

    def test_parse_percent_list(self):
        self.assertEqual(tk.parse_percent_list("-10,0,10"), [-10, 0, 10])
        self.assertEqual(tk.parse_percent_list(" -5 , 5 "), [-5, 5])
        self.assertEqual(tk.parse_percent_list(""), [])
        self.assertEqual(tk.parse_percent_list(None), [])

    def test_parse_percent_list_invalid_raises(self):
        with self.assertRaises(ValueError):
            tk.parse_percent_list("abc")

    def test_iter_prosody_grid_cartesian_product(self):
        grid = tk.iter_prosody_grid([-10, 10], [0, 5])
        self.assertEqual(grid, [(-10, 0), (-10, 5), (10, 0), (10, 5)])

    def test_iter_prosody_grid_empty_axes_default_to_none(self):
        self.assertEqual(tk.iter_prosody_grid([], []), [(None, None)])
        self.assertEqual(tk.iter_prosody_grid([1], []), [(1, None)])

    def test_build_synthesis_plan_is_deterministic_for_same_seed(self):
        plan1 = tk.build_synthesis_plan(["Краб", "эй, Краб"], ["aidar", "baya"], [(None, None)], seed=42)
        plan2 = tk.build_synthesis_plan(["Краб", "эй, Краб"], ["aidar", "baya"], [(None, None)], seed=42)
        self.assertEqual(plan1, plan2)
        self.assertEqual(len(plan1), 4)  # 2 phrases x 2 speakers x 1 prosody

    def test_build_synthesis_plan_full_cartesian_coverage(self):
        plan = tk.build_synthesis_plan(["p1", "p2"], ["s1", "s2"], [(1, 2), (3, 4)], seed=1)
        self.assertEqual(len(plan), 8)  # 2 x 2 x 2
        self.assertEqual(len(set(plan)), 8)  # все комбинации уникальны

    def test_build_synthesis_plan_requires_nonempty_inputs(self):
        with self.assertRaises(ValueError):
            tk.build_synthesis_plan([], ["s1"], [(None, None)])
        with self.assertRaises(ValueError):
            tk.build_synthesis_plan(["p1"], [], [(None, None)])

    def test_build_neutral_sentences_default_returns_full_base(self):
        result = tk.build_neutral_sentences(target_count=1)
        self.assertEqual(len(result), 1)

    def test_build_neutral_sentences_extends_past_base_length(self):
        base_len = len(tk._NEUTRAL_RU_SENTENCES)
        target = base_len + 50
        result = tk.build_neutral_sentences(target_count=target)
        self.assertEqual(len(result), target)
        # уникальность после расширения вариациями с вводными словами
        normalized = {tk.normalize_phrase_for_compare(s) for s in result}
        self.assertEqual(len(normalized), target)

    def test_deterministic_train_test_split_ratio_and_determinism(self):
        items = list(range(100))
        train1, test1 = tk.deterministic_train_test_split(items, test_ratio=0.1, seed=7)
        train2, test2 = tk.deterministic_train_test_split(items, test_ratio=0.1, seed=7)
        self.assertEqual((train1, test1), (train2, test2))
        self.assertEqual(len(test1), 10)
        self.assertEqual(len(train1) + len(test1), 100)
        self.assertEqual(set(train1) | set(test1), set(items))

    def test_deterministic_train_test_split_invalid_ratio_raises(self):
        with self.assertRaises(ValueError):
            tk.deterministic_train_test_split([1, 2, 3], test_ratio=1.0)
        with self.assertRaises(ValueError):
            tk.deterministic_train_test_split([1, 2, 3], test_ratio=-0.1)

    def test_positive_combo_split_has_no_train_test_leakage(self):
        """Регрессионный тест на утечку: комбинации синтеза (фраза, спикер,
        rate, pitch), попавшие в train, не должны повторно встречаться в test
        -- иначе test-метрики были бы оптимистично смещены (см. докстроку
        stage_positives)."""
        combos = tk.build_synthesis_plan(
            ["Краб", "эй, Краб"], list(tk.DEFAULT_SPEAKERS), tk.iter_prosody_grid([-15, 0, 15], [-10, 0, 10]),
            seed=13,
        )
        combos_train, combos_test = tk.deterministic_train_test_split(combos, test_ratio=0.1, seed=13)
        self.assertTrue(set(combos_train).isdisjoint(set(combos_test)))
        self.assertGreater(len(combos_train), 0)
        self.assertGreater(len(combos_test), 0)

    def test_compute_total_length_samples_empty_returns_minimum(self):
        self.assertEqual(tk.compute_total_length_samples([]), 32000)

    def test_compute_total_length_samples_clamps_near_minimum(self):
        # медиана 20000 + буфер 12000 = 32000 -- уже на минимуме
        self.assertEqual(tk.compute_total_length_samples([20000, 20000, 20000]), 32000)

    def test_compute_total_length_samples_rounds_and_adds_buffer(self):
        result = tk.compute_total_length_samples([40000, 41000, 42000], min_len=32000, buffer=12000)
        # медиана=41000 -> round(41000/1000)*1000=41000 -> +12000=53000
        self.assertEqual(result, 53000)

    def test_apply_limit(self):
        self.assertEqual(tk._apply_limit(4000, None), 4000)
        self.assertEqual(tk._apply_limit(4000, 3), 3)
        self.assertEqual(tk._apply_limit(4000, 0), 0)
        self.assertEqual(tk._apply_limit(4000, -5), 0)  # не уходим в отрицательные

    def test_glob_wavs_missing_dir_returns_empty(self):
        self.assertEqual(tk._glob_wavs(None), [])
        self.assertEqual(tk._glob_wavs("/definitely/does/not/exist/xyz"), [])

    def test_glob_wavs_finds_wav_recursively(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "sub").mkdir()
            (base / "a.wav").write_bytes(b"")
            (base / "sub" / "b.wav").write_bytes(b"")
            (base / "c.txt").write_bytes(b"")
            found = tk._glob_wavs(base)
            self.assertEqual(len(found), 2)
            self.assertTrue(all(f.endswith(".wav") for f in found))


# ---------------------------------------------------------------------------
# Adversarial-слова: список НЕ должен содержать «краб» как точное слово
# ---------------------------------------------------------------------------

class AdversarialWordsTests(unittest.TestCase):
    def test_adversarial_words_never_equal_target_phrase_exactly(self):
        forbidden = {tk.normalize_phrase_for_compare(tk.DEFAULT_PHRASE)}
        for w in tk._ADVERSARIAL_WORDS_RU:
            self.assertNotIn(
                tk.normalize_phrase_for_compare(w), forbidden,
                f"Адверсариал {w!r} точно совпадает с целевой фразой",
            )

    def test_adversarial_words_are_unique(self):
        normalized = [tk.normalize_phrase_for_compare(w) for w in tk._ADVERSARIAL_WORDS_RU]
        self.assertEqual(len(normalized), len(set(normalized)))

    def test_adversarial_words_nonempty_and_reasonably_sized(self):
        self.assertGreaterEqual(len(tk._ADVERSARIAL_WORDS_RU), 20)

    def test_build_adversarial_words_returns_base_list(self):
        words = tk.build_adversarial_words()
        self.assertEqual(set(words), set(tk._ADVERSARIAL_WORDS_RU))

    def test_build_adversarial_words_merges_extra_without_duplicates(self):
        words = tk.build_adversarial_words(extra=["крабы", "совершенно новое слово"])
        self.assertIn("совершенно новое слово", words)
        # "крабы" уже в базовом списке -- не должно задублироваться
        self.assertEqual(words.count("крабы"), 1)

    def test_build_adversarial_words_raises_if_extra_equals_phrase(self):
        with self.assertRaises(ValueError):
            tk.build_adversarial_words(extra=["Краб"])

    def test_validate_negative_words_raises_on_exact_match(self):
        with self.assertRaises(ValueError):
            tk.validate_negative_words(["Краб"], ["краб"])

    def test_validate_negative_words_case_and_whitespace_insensitive(self):
        with self.assertRaises(ValueError):
            tk.validate_negative_words(["  КРАБ  "], ["краб"])

    def test_validate_negative_words_passes_for_distinct_words(self):
        # не должно бросать -- слова разные
        tk.validate_negative_words(["краба", "корабль"], ["краб"])

    def test_neutral_sentences_never_contain_forbidden_root(self):
        for sentence in tk._NEUTRAL_RU_SENTENCES:
            self.assertNotIn("краб", sentence.lower())

    def test_neutral_base_sentences_are_unique(self):
        normalized = [tk.normalize_phrase_for_compare(s) for s in tk._NEUTRAL_RU_SENTENCES]
        self.assertEqual(len(normalized), len(set(normalized)))


# ---------------------------------------------------------------------------
# Парсер аргументов
# ---------------------------------------------------------------------------

class ArgParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = tk.build_arg_parser()

    def test_returns_argument_parser(self):
        self.assertIsInstance(self.parser, argparse.ArgumentParser)

    def test_defaults(self):
        ns = self.parser.parse_args(["--stage", "positives"])
        self.assertEqual(ns.phrase, tk.DEFAULT_PHRASE)
        self.assertIsNone(ns.secondary_phrase)  # нормализуется в main(), не в парсере
        self.assertEqual(ns.model_name, tk.DEFAULT_MODEL_NAME)
        self.assertEqual(ns.speakers, ",".join(tk.DEFAULT_SPEAKERS))
        self.assertEqual(ns.positives_count, 4000)
        self.assertEqual(ns.max_fp_per_hour, 1.0)
        self.assertEqual(ns.min_recall, 0.20)
        self.assertFalse(ns.force)
        self.assertIsNone(ns.limit)
        self.assertFalse(ns.verbose)
        self.assertTrue(ns.use_oww_adversarial_texts)

    def test_stage_choices_restricted_to_known_stages(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["--stage", "not-a-real-stage"])

    def test_stage_and_all_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["--stage", "corpora", "--all"])

    def test_all_flag_alone_is_accepted(self):
        ns = self.parser.parse_args(["--all"])
        self.assertTrue(ns.all)
        self.assertIsNone(ns.stage)

    def test_no_oww_adversarial_texts_flag(self):
        ns = self.parser.parse_args(["--stage", "negatives", "--no-use-oww-adversarial-texts"])
        self.assertFalse(ns.use_oww_adversarial_texts)

    def test_secondary_phrase_is_repeatable(self):
        ns = self.parser.parse_args([
            "--stage", "positives",
            "--secondary-phrase", "форма раз",
            "--secondary-phrase", "форма два",
        ])
        self.assertEqual(ns.secondary_phrase, ["форма раз", "форма два"])

    def test_data_dir_defaults_to_application_support(self):
        ns = self.parser.parse_args(["--stage", "install"])
        self.assertIn("Application Support", ns.data_dir)
        self.assertIn("KrabEar", ns.data_dir)

    def test_limit_and_force_flags(self):
        ns = self.parser.parse_args(["--stage", "positives", "--limit", "3", "--force"])
        self.assertEqual(ns.limit, 3)
        self.assertTrue(ns.force)

    def test_main_requires_stage_or_all(self):
        with self.assertRaises(SystemExit):
            tk.main([])


# ---------------------------------------------------------------------------
# Resume-маркеры
# ---------------------------------------------------------------------------

class ResumeMarkerTests(unittest.TestCase):
    def test_marker_path_naming(self):
        p = tk.marker_path(Path("/tmp/foo"), "corpora")
        self.assertEqual(p, Path("/tmp/foo/.done_corpora.json"))

    def test_is_stage_done_false_when_absent(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(tk.is_stage_done(Path(td), "corpora"))

    def test_read_marker_returns_none_when_absent(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(tk.read_marker(Path(td), "corpora"))

    def test_write_then_read_marker_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            stage_dir = Path(td) / "corpora"
            tk.write_marker(stage_dir, "corpora", {"count": 42, "path": "/x/y"})
            self.assertTrue(tk.is_stage_done(stage_dir, "corpora"))
            meta = tk.read_marker(stage_dir, "corpora")
            self.assertEqual(meta["stage"], "corpora")
            self.assertEqual(meta["count"], 42)
            self.assertEqual(meta["path"], "/x/y")
            self.assertIn("completed_at", meta)

    def test_write_marker_creates_stage_dir_if_missing(self):
        with tempfile.TemporaryDirectory() as td:
            stage_dir = Path(td) / "nested" / "corpora"
            self.assertFalse(stage_dir.exists())
            tk.write_marker(stage_dir, "corpora", {})
            self.assertTrue(stage_dir.exists())

    def test_read_marker_handles_corrupt_json_gracefully(self):
        with tempfile.TemporaryDirectory() as td:
            stage_dir = Path(td)
            marker = tk.marker_path(stage_dir, "corpora")
            marker.write_text("{not valid json", encoding="utf-8")
            self.assertIsNone(tk.read_marker(stage_dir, "corpora"))

    def test_different_stages_have_independent_markers(self):
        with tempfile.TemporaryDirectory() as td:
            stage_dir = Path(td)
            tk.write_marker(stage_dir, "train", {"x": 1})
            self.assertTrue(tk.is_stage_done(stage_dir, "train"))
            self.assertFalse(tk.is_stage_done(stage_dir, "export"))


# ---------------------------------------------------------------------------
# ProjectPaths
# ---------------------------------------------------------------------------

class ProjectPathsTests(unittest.TestCase):
    def test_from_work_dir_lays_out_subdirectories(self):
        paths = tk.ProjectPaths.from_work_dir(Path("/tmp/wwm_test_root"))
        self.assertEqual(paths.root, Path("/tmp/wwm_test_root"))
        self.assertEqual(paths.corpora, Path("/tmp/wwm_test_root/corpora"))
        self.assertEqual(paths.positives, Path("/tmp/wwm_test_root/positives"))
        self.assertEqual(paths.negatives, Path("/tmp/wwm_test_root/negatives"))
        self.assertEqual(paths.features, Path("/tmp/wwm_test_root/features"))
        self.assertEqual(paths.artifacts, Path("/tmp/wwm_test_root/artifacts"))

    def test_report_path_uses_model_name(self):
        paths = tk.ProjectPaths.from_work_dir(Path("/tmp/wwm_test_root"))
        self.assertEqual(
            paths.report_path("krab_ru"), Path("/tmp/wwm_test_root/report_krab_ru.md"),
        )
        self.assertEqual(
            paths.report_path("other_model"), Path("/tmp/wwm_test_root/report_other_model.md"),
        )


if __name__ == "__main__":
    unittest.main()
