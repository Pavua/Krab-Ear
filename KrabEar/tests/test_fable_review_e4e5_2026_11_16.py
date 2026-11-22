"""Ревью ветки E4/E5 (2026-11-16): регрессии, найденные поверх 5969c3ba.

F4. `hf_token` / `stt_gigaam_hf_token` стали строковыми полями валидатора с ЖЁСТКОЙ ошибкой на
    None. `set_settings` / import / restore валидируют СЛИТЫЙ словарь, а чтение оставляло null в
    кэше — поэтому у пользователя с `hf_token: null` на диске (ровно та аудитория, которой E5 п.1
    чинил старт) падало любое изменение любой настройки. Чтение обязано деградировать к «токена
    нет»; запись не-строки по-прежнему отвергается, а сам валидатор значения не подменяет
    (контракт test_settings_text_schema_2026_09_08).

F6. Сиблинги E5 п.4: `bool("false") is True` в разрушающих параметрах — `delete_originals`
    (удаление исходных записей при слиянии) и `overwrite` (перезапись профиля нормализации).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_merger import RecordingMerger  # noqa: E402
from backend.service import BackendService  # noqa: E402
from backend.settings_backup import SettingsBackup  # noqa: E402
from backend.settings_service import SettingsService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from core.normalization_profiles import NormalizationProfileRegistry  # noqa: E402

_TOKEN_FIELDS = ("hf_token", "stt_gigaam_hf_token")


class StoredNullTokenDoesNotBlockSettingsTests(unittest.TestCase):
    """F4: настоящие StateStore + SettingsService на временном каталоге."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.data_dir = root / "data"
        self.data_dir.mkdir()
        self.store = StateStore(self.data_dir)
        self.svc = SettingsService(
            store=self.store, backup=SettingsBackup(backup_dir=root / "backups"),
        )

    def _poison(self, **fields):
        """Пишет null прямо в settings.json — как у установки, сохранённой до валидации типа."""
        # Первое сохранение создаёт settings.json с валидной policy штатным путём.
        # Свежему хранилищу нужна явная пара policy-флагов (validated repair).
        self.svc.handle_set_settings(
            {"privacy_mode_enabled": False, "history_encryption_enabled": False},
        )
        path = self.data_dir / "settings.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.update(fields)
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.svc.invalidate_cache()

    def test_cached_settings_heals_stored_null(self):
        for bad in (None, 123, ["x"]):
            with self.subTest(value=bad):
                self._poison(hf_token=bad, stt_gigaam_hf_token=bad)
                cached = self.svc.cached_settings()
                for field in _TOKEN_FIELDS:
                    self.assertEqual(cached.get(field), "")

    def test_unrelated_set_settings_succeeds_with_stored_null(self):
        self._poison(hf_token=None, stt_gigaam_hf_token=None)
        before = bool(self.svc.cached_settings().get("play_start_sound", True))
        self.svc.handle_set_settings({"play_start_sound": not before})
        self.svc.invalidate_cache()
        self.assertEqual(self.svc.cached_settings().get("play_start_sound"), not before)
        # Сохранение заодно вылечило файл на диске.
        on_disk = json.loads((self.data_dir / "settings.json").read_text(encoding="utf-8"))
        for field in _TOKEN_FIELDS:
            self.assertEqual(on_disk.get(field), "")

    def test_explicit_null_write_is_still_rejected(self):
        with self.assertRaises(ValueError):
            self.svc.handle_set_settings({"hf_token": None})


class DestructiveBoolParamsTests(unittest.TestCase):
    """F6: строка «false» не должна включать разрушающее действие."""

    def test_merge_delete_originals_string_false_is_false(self):
        merger = RecordingMerger()
        for raw, expected in (("false", False), ("0", False), (False, False),
                              ("true", True), (True, True)):
            with self.subTest(value=raw):
                with mock.patch.object(merger, "merge_items", return_value={"ok": True}) as spy:
                    merger.handle_merge_recordings(
                        {"item_ids": ["a", "b"], "delete_originals": raw}, store=object(),
                    )
                self.assertIs(spy.call_args.kwargs["delete_originals"], expected)

    def test_add_normalization_profile_string_false_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = NormalizationProfileRegistry(data_dir=Path(tmp))

            class _Stub:
                _norm_profiles = registry
                _coerce_bool = staticmethod(BackendService._coerce_bool)

            add = BackendService._handle_add_normalization_profile
            builtin = next(p["name"] for p in registry.list_profiles() if p.get("builtin"))
            # Гард встроенных профилей снимается только настоящим согласием на перезапись.
            for refusal in ("false", "0", "no", None):
                with self.subTest(overwrite=refusal):
                    with self.assertRaises(ValueError):
                        add(_Stub(), {"name": builtin, "rules": [], "overwrite": refusal})
            add(_Stub(), {"name": builtin, "rules": [], "overwrite": "true"})


if __name__ == "__main__":
    unittest.main()
