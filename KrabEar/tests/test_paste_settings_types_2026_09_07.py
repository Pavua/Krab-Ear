"""Явные defaults и bool-контракт существующих Swift-режимов вставки."""
from __future__ import annotations

import json

import pytest

from backend.settings_backup import SettingsBackup
from backend.settings_service import SettingsService
from backend.settings_validator import SettingsValidator
from backend.state_store import StateStore
from core.config import DEFAULT_SETTINGS


KEYS = ("smart_field_format_enabled", "streaming_paste_enabled")


@pytest.mark.parametrize("key", KEYS)
def test_new_install_defaults_to_disabled(key):
    assert DEFAULT_SETTINGS.get(key) is False


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("raw,expected", [
    (True, True), (False, False),
    ("true", True), ("false", False),
    ("on", True), ("off", False),
    (1, True), (0, False),
    (None, False), ("invalid", False),
])
def test_validator_produces_boolean(key, raw, expected):
    result = SettingsValidator().validate({key: raw})
    assert result.valid
    assert result.fixed[key] is expected


@pytest.mark.parametrize("key", KEYS)
def test_existing_enabled_preference_survives_defaults(tmp_path, key):
    store = StateStore(tmp_path / "data")
    store.save_settings({key: True})
    service = SettingsService(store, backup=SettingsBackup(tmp_path / "backups"))
    assert service.handle_get_settings({})[key] is True


@pytest.mark.parametrize("key", KEYS)
def test_service_disk_and_fresh_read_keep_boolean_type(tmp_path, key, monkeypatch):
    # Pydantic reload читает отдельный live settings.json и не входит
    # в контракт этих Swift-флагов; весь service/store путь остаётся реальным.
    monkeypatch.setattr("core.config.reload_settings_from_json", lambda: 0)
    store = StateStore(tmp_path / "data")
    backup = SettingsBackup(tmp_path / "backups")
    service = SettingsService(store, backup=backup)
    for raw, expected in (("true", True), ("false", False), ("true", True)):
        response = service.handle_set_settings({key: raw})
        assert response[key] is expected
        assert json.loads(store.settings_path.read_text())[key] is expected
        fresh = SettingsService(StateStore(tmp_path / "data"), backup=backup)
        assert fresh.handle_get_settings({})[key] is expected
