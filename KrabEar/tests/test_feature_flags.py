"""Тесты для FeatureFlags — управление feature-флагами Krab Ear.

Покрывает:
- Дефолтные значения встроенных флагов
- is_enabled / set_flag / list_flags / get_flag_info
- Персистентность в feature_flags.json
- IPC-обработчики handle_get_feature_flags / handle_set_feature_flag
- Граничные случаи: неизвестный флаг, пустое имя, некорректный enabled
"""

from __future__ import annotations
from backend.feature_flags import FeatureFlags, _BUILTIN_FLAGS

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestFeatureFlagsDefaults(unittest.TestCase):
    """Проверяем дефолтные значения встроенных флагов."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ff = FeatureFlags(data_dir=self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pipeline_v2_disabled_by_default(self) -> None:
        self.assertFalse(self.ff.is_enabled("pipeline_v2"))

    def test_auto_backup_enabled_by_default(self) -> None:
        self.assertTrue(self.ff.is_enabled("auto_backup"))

    def test_llm_rewrite_enabled_by_default(self) -> None:
        self.assertTrue(self.ff.is_enabled("llm_rewrite"))

    def test_confidence_calibration_enabled_by_default(self) -> None:
        self.assertTrue(self.ff.is_enabled("confidence_calibration"))

    def test_search_index_enabled_by_default(self) -> None:
        self.assertTrue(self.ff.is_enabled("search_index"))

    def test_webhook_notifications_disabled_by_default(self) -> None:
        self.assertFalse(self.ff.is_enabled("webhook_notifications"))

    def test_unknown_flag_returns_false(self) -> None:
        self.assertFalse(self.ff.is_enabled("nonexistent_flag_xyz"))

    def test_all_builtin_flags_present_in_list(self) -> None:
        flags = self.ff.list_flags()
        for name in _BUILTIN_FLAGS:
            self.assertIn(name, flags)


class TestFeatureFlagsSetGet(unittest.TestCase):
    """Тесты set_flag / is_enabled / list_flags."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ff = FeatureFlags(data_dir=self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_set_flag_changes_value(self) -> None:
        self.ff.set_flag("pipeline_v2", True)
        self.assertTrue(self.ff.is_enabled("pipeline_v2"))

    def test_set_flag_disable(self) -> None:
        self.ff.set_flag("auto_backup", False)
        self.assertFalse(self.ff.is_enabled("auto_backup"))

    def test_set_flag_creates_custom_flag(self) -> None:
        self.ff.set_flag("my_custom_feature", True)
        self.assertTrue(self.ff.is_enabled("my_custom_feature"))

    def test_list_flags_returns_dict_of_bools(self) -> None:
        flags = self.ff.list_flags()
        self.assertIsInstance(flags, dict)
        for value in flags.values():
            self.assertIsInstance(value, bool)

    def test_list_flags_reflects_set(self) -> None:
        self.ff.set_flag("pipeline_v2", True)
        flags = self.ff.list_flags()
        self.assertTrue(flags["pipeline_v2"])

    def test_set_flag_empty_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.ff.set_flag("", True)

    def test_set_flag_none_name_raises(self) -> None:
        with self.assertRaises((ValueError, AttributeError, TypeError)):
            self.ff.set_flag(None, True)  # type: ignore[arg-type]


class TestFeatureFlagsGetFlagInfo(unittest.TestCase):
    """Тесты get_flag_info."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ff = FeatureFlags(data_dir=self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_get_flag_info_builtin_fields(self) -> None:
        info = self.ff.get_flag_info("auto_backup")
        self.assertEqual(info["name"], "auto_backup")
        self.assertIn("enabled", info)
        self.assertIn("description", info)
        self.assertIn("since_version", info)
        self.assertTrue(info["is_builtin"])

    def test_get_flag_info_enabled_matches_is_enabled(self) -> None:
        self.ff.set_flag("pipeline_v2", True)
        info = self.ff.get_flag_info("pipeline_v2")
        self.assertEqual(info["enabled"], self.ff.is_enabled("pipeline_v2"))

    def test_get_flag_info_unknown_raises_keyerror(self) -> None:
        with self.assertRaises(KeyError):
            self.ff.get_flag_info("absolutely_unknown_flag")

    def test_get_flag_info_custom_flag(self) -> None:
        self.ff.set_flag("beta_feature", True)
        info = self.ff.get_flag_info("beta_feature")
        self.assertEqual(info["name"], "beta_feature")
        self.assertTrue(info["enabled"])
        self.assertFalse(info["is_builtin"])

    def test_get_flag_info_description_nonempty_for_builtins(self) -> None:
        for flag_name in _BUILTIN_FLAGS:
            info = self.ff.get_flag_info(flag_name)
            self.assertTrue(len(info["description"]) > 0, f"Пустое описание для {flag_name}")


class TestFeatureFlagsPersistence(unittest.TestCase):
    """Тесты персистентности в JSON-файл."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_flags_persist_across_instances(self) -> None:
        ff1 = FeatureFlags(data_dir=self._data_dir)
        ff1.set_flag("pipeline_v2", True)
        ff1.set_flag("auto_backup", False)

        # Создаём новый экземпляр — должен прочитать из файла
        ff2 = FeatureFlags(data_dir=self._data_dir)
        self.assertTrue(ff2.is_enabled("pipeline_v2"))
        self.assertFalse(ff2.is_enabled("auto_backup"))

    def test_flags_file_is_valid_json(self) -> None:
        ff = FeatureFlags(data_dir=self._data_dir)
        ff.set_flag("pipeline_v2", True)

        flags_path = self._data_dir / "feature_flags.json"
        self.assertTrue(flags_path.exists())
        data = json.loads(flags_path.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)

    def test_flags_file_persists_custom_flag(self) -> None:
        ff = FeatureFlags(data_dir=self._data_dir)
        ff.set_flag("my_new_flag", True)

        ff2 = FeatureFlags(data_dir=self._data_dir)
        self.assertTrue(ff2.is_enabled("my_new_flag"))

    def test_default_flags_without_file(self) -> None:
        """Если файла нет — используются дефолты встроенных флагов."""
        flags_path = self._data_dir / "feature_flags.json"
        self.assertFalse(flags_path.exists())

        ff = FeatureFlags(data_dir=self._data_dir)
        self.assertFalse(ff.is_enabled("pipeline_v2"))
        self.assertTrue(ff.is_enabled("auto_backup"))

    def test_corrupted_file_falls_back_to_defaults(self) -> None:
        """Повреждённый файл → молча используем дефолты."""
        flags_path = self._data_dir / "feature_flags.json"
        flags_path.write_text("NOT_JSON_AT_ALL", encoding="utf-8")

        # Не должно бросать исключений
        ff = FeatureFlags(data_dir=self._data_dir)
        self.assertFalse(ff.is_enabled("pipeline_v2"))
        self.assertTrue(ff.is_enabled("auto_backup"))


class TestFeatureFlagsIPC(unittest.TestCase):
    """Тесты IPC-обработчиков handle_get_feature_flags / handle_set_feature_flag."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ff = FeatureFlags(data_dir=self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_handle_get_feature_flags_returns_list(self) -> None:
        result = self.ff.handle_get_feature_flags({})
        self.assertIn("flags", result)
        self.assertIn("count", result)
        self.assertIsInstance(result["flags"], list)
        self.assertGreater(result["count"], 0)

    def test_handle_get_feature_flags_all_builtins_present(self) -> None:
        result = self.ff.handle_get_feature_flags({})
        names = {f["name"] for f in result["flags"]}
        for builtin_name in _BUILTIN_FLAGS:
            self.assertIn(builtin_name, names)

    def test_handle_set_feature_flag_enable(self) -> None:
        result = self.ff.handle_set_feature_flag({"flag_name": "pipeline_v2", "enabled": True})
        self.assertEqual(result["flag_name"], "pipeline_v2")
        self.assertTrue(result["enabled"])
        self.assertTrue(self.ff.is_enabled("pipeline_v2"))

    def test_handle_set_feature_flag_disable(self) -> None:
        result = self.ff.handle_set_feature_flag({"flag_name": "auto_backup", "enabled": False})
        self.assertFalse(result["enabled"])
        self.assertFalse(self.ff.is_enabled("auto_backup"))

    def test_handle_set_feature_flag_missing_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.ff.handle_set_feature_flag({"enabled": True})

    def test_handle_set_feature_flag_non_bool_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.ff.handle_set_feature_flag({"flag_name": "pipeline_v2", "enabled": "yes"})

    def test_handle_set_feature_flag_result_has_ts(self) -> None:
        result = self.ff.handle_set_feature_flag({"flag_name": "pipeline_v2", "enabled": True})
        self.assertIn("ts", result)

    def test_handle_get_feature_flags_has_ts(self) -> None:
        result = self.ff.handle_get_feature_flags({})
        self.assertIn("ts", result)


class TestFeatureFlagsWhitespaceValidation(unittest.TestCase):
    """Wave 159: set_flag rejects whitespace-only flag names."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ff = FeatureFlags(data_dir=self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_set_flag_whitespace_only_rejected(self) -> None:
        """Имя из одних пробелов должно вызывать ValueError."""
        with self.assertRaises(ValueError):
            self.ff.set_flag("   ", True)

    def test_set_flag_empty_rejected(self) -> None:
        """Пустая строка вызывает ValueError (уже проходило — проверяем regression)."""
        with self.assertRaises(ValueError):
            self.ff.set_flag("", True)

    def test_set_flag_tab_only_rejected(self) -> None:
        """Имя из одного таба должно вызывать ValueError."""
        with self.assertRaises(ValueError):
            self.ff.set_flag("\t", True)

    def test_set_flag_newline_only_rejected(self) -> None:
        """Имя из символа новой строки должно вызывать ValueError."""
        with self.assertRaises(ValueError):
            self.ff.set_flag("\n", True)

    def test_set_flag_with_leading_space_rejected(self) -> None:
        """Имя с ведущим пробелом отклоняется (не нормализуется).

        Решение: reject, а не normalize — имена флагов должны быть строгими
        идентификаторами без пробелов. Caller обязан передавать clean name.
        """
        with self.assertRaises(ValueError):
            self.ff.set_flag("  my_flag", True)

    def test_set_flag_valid_name_still_works(self) -> None:
        """Обычное имя без пробелов продолжает работать."""
        self.ff.set_flag("valid_flag", True)
        self.assertTrue(self.ff.is_enabled("valid_flag"))


class TestFeatureFlagsListAll(unittest.TestCase):
    """list_flags() включает и встроенные, и пользовательские флаги."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ff = FeatureFlags(data_dir=self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_list_all_includes_custom_flag(self) -> None:
        self.ff.set_flag("custom_xyz", True)
        flags = self.ff.list_flags()
        self.assertIn("custom_xyz", flags)
        self.assertTrue(flags["custom_xyz"])

    def test_list_all_returns_copy_not_internal_dict(self) -> None:
        """Изменение возвращённого словаря не должно влиять на внутреннее состояние."""
        flags = self.ff.list_flags()
        flags["pipeline_v2"] = True
        # Внутреннее значение не изменилось
        self.assertFalse(self.ff.is_enabled("pipeline_v2"))

    def test_is_enabled_unknown_flag_always_false(self) -> None:
        """Неизвестный флаг → False, никогда не KeyError."""
        for name in ("totally_unknown", "", "  ", "UPPER_CASE"):
            result = self.ff.is_enabled(name)
            self.assertFalse(result, f"Expected False for unknown flag {name!r}")


class TestFeatureFlagsWave98(unittest.TestCase):
    """Wave 98 — дополнительное покрытие per spec."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ff = FeatureFlags(data_dir=self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # test_default_flag_state
    def test_default_flag_state_all_builtins_have_correct_defaults(self) -> None:
        """Все встроенные флаги загружены с правильными дефолтами."""
        expected = {
            "pipeline_v2": False,
            "auto_backup": True,
            "llm_rewrite": True,
            "confidence_calibration": True,
            "search_index": True,
            "webhook_notifications": False,
        }
        for name, expected_val in expected.items():
            self.assertEqual(
                self.ff.is_enabled(name),
                expected_val,
                f"Дефолт флага {name!r} должен быть {expected_val}",
            )

    # test_set_flag_persists (JSON roundtrip)
    def test_set_flag_persists_json_roundtrip(self) -> None:
        """set_flag → файл → новый экземпляр → is_enabled возвращает то же."""
        data_dir = Path(self._tmp.name)
        ff1 = FeatureFlags(data_dir=data_dir)
        ff1.set_flag("pipeline_v2", True)
        ff1.set_flag("webhook_notifications", True)
        ff1.set_flag("llm_rewrite", False)

        ff2 = FeatureFlags(data_dir=data_dir)
        self.assertTrue(ff2.is_enabled("pipeline_v2"))
        self.assertTrue(ff2.is_enabled("webhook_notifications"))
        self.assertFalse(ff2.is_enabled("llm_rewrite"))

    # test_get_unknown_flag_returns_default
    def test_get_unknown_flag_returns_default_false(self) -> None:
        """is_enabled для несуществующего флага всегда возвращает False."""
        for name in ("totally_new_flag", "DOES_NOT_EXIST", "xyzzy_123"):
            with self.subTest(flag=name):
                self.assertFalse(self.ff.is_enabled(name))

    # test_atomic_set_concurrent_writes
    def test_atomic_set_concurrent_writes(self) -> None:
        """Параллельные set_flag не должны приводить к data race или исключениям."""
        import threading
        errors = []

        def worker(flag: str, val: bool) -> None:
            try:
                self.ff.set_flag(flag, val)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"flag_{i}", i % 2 == 0))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Ошибки при параллельных write: {errors}")
        # После всех writes список флагов должен быть корректным dict[str, bool]
        flags = self.ff.list_flags()
        for v in flags.values():
            self.assertIsInstance(v, bool)

    # test_list_all_flags
    def test_list_all_flags_contains_all_builtins(self) -> None:
        """list_flags() содержит все 6 встроенных флагов."""
        flags = self.ff.list_flags()
        self.assertEqual(len(flags), len(_BUILTIN_FLAGS))
        for name in _BUILTIN_FLAGS:
            self.assertIn(name, flags)

    def test_list_all_flags_after_custom_add(self) -> None:
        """list_flags() включает пользовательский флаг после set_flag."""
        self.ff.set_flag("my_wave98_flag", True)
        flags = self.ff.list_flags()
        self.assertIn("my_wave98_flag", flags)
        self.assertTrue(flags["my_wave98_flag"])

    # test_reset_to_defaults
    def test_reset_to_defaults_via_new_instance_after_delete(self) -> None:
        """Удаление файла флагов → новый экземпляр использует дефолты."""
        data_dir = Path(self._tmp.name)
        ff1 = FeatureFlags(data_dir=data_dir)
        ff1.set_flag("pipeline_v2", True)
        ff1.set_flag("auto_backup", False)

        # Удаляем файл персистентности
        flags_path = data_dir / "feature_flags.json"
        flags_path.unlink()

        ff2 = FeatureFlags(data_dir=data_dir)
        # Должны вернуться к оригинальным дефолтам
        self.assertFalse(ff2.is_enabled("pipeline_v2"))
        self.assertTrue(ff2.is_enabled("auto_backup"))

    def test_reset_to_defaults_by_overwriting_all_builtin_flags(self) -> None:
        """Перезапись каждого флага его дефолтным значением = reset."""
        # Сначала ставим нестандартные значения
        self.ff.set_flag("pipeline_v2", True)
        self.ff.set_flag("auto_backup", False)

        # Сбрасываем по дефолтам из _BUILTIN_FLAGS
        for name, (default, _, _) in _BUILTIN_FLAGS.items():
            self.ff.set_flag(name, default)

        self.assertFalse(self.ff.is_enabled("pipeline_v2"))
        self.assertTrue(self.ff.is_enabled("auto_backup"))

    # test_invalid_flag_name_rejected (sanitization)
    def test_invalid_flag_name_empty_string_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.ff.set_flag("", True)

    def test_invalid_flag_name_none_rejected(self) -> None:
        with self.assertRaises((ValueError, AttributeError, TypeError)):
            self.ff.set_flag(None, True)  # type: ignore[arg-type]

    def test_invalid_enabled_value_string_rejected(self) -> None:
        """handle_set_feature_flag с enabled=str должен бросать ValueError."""
        with self.assertRaises(ValueError):
            self.ff.handle_set_feature_flag({"flag_name": "pipeline_v2", "enabled": "true"})

    def test_invalid_enabled_value_int_rejected(self) -> None:
        """handle_set_feature_flag с enabled=int должен бросать ValueError."""
        with self.assertRaises(ValueError):
            self.ff.handle_set_feature_flag({"flag_name": "pipeline_v2", "enabled": 1})

    def test_atomic_save_no_partial_file(self) -> None:
        """W988/W979-F1: _save() использует tmp+fsync+rename — нет частичного файла.

        Проверяем что после set_flag:
        - целевой файл существует и содержит валидный JSON;
        - tmp-файл (.json.tmp) не остался на диске (cleanup после успешного rename).
        """
        self.ff.set_flag("pipeline_v2", True)

        flags_path = self.ff._flags_path
        tmp_path = flags_path.with_suffix(flags_path.suffix + ".tmp")

        # Целевой файл должен существовать и содержать валидный JSON
        self.assertTrue(flags_path.exists(), "feature_flags.json должен существовать после set_flag")
        import json as _json
        with open(flags_path, encoding="utf-8") as fh:
            data = _json.load(fh)
        self.assertIn("pipeline_v2", data)
        self.assertTrue(data["pipeline_v2"])

        # Tmp-файл не должен оставаться после успешного сохранения
        self.assertFalse(
            tmp_path.exists(),
            f"Временный файл {tmp_path.name} не должен оставаться после успешного _save()",
        )


if __name__ == "__main__":
    unittest.main()
