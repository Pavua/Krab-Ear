"""Строгий строковый контракт C5 без сетевых запросов и live settings."""
from __future__ import annotations

import json

import pytest

from backend.settings_backup import SettingsBackup
from backend.settings_service import SettingsService
from backend.settings_validator import SettingsValidator
from backend.state_store import StateStore
from core.config import DEFAULT_SETTINGS

KEYS = (
    'llm_brain_model',
    'cloud_rewriter_base_url',
    'cloud_rewriter_custom_model',
    'cloud_rewriter_openai_model',
    'cloud_rewriter_anthropic_model',
    'cloud_rewriter_api_key',
)
SENTINEL = 'private-credential-value-must-not-appear'


@pytest.mark.parametrize('key', KEYS)
@pytest.mark.parametrize('value', [None, 17, 1.25, False, [], {'credential': SENTINEL}])
def test_wrong_text_type_is_rejected_without_coercion(key, value):
    result = SettingsValidator().validate({key: value})
    assert not result.valid
    assert any(key in error for error in result.errors)
    assert SENTINEL not in ' '.join(result.errors + result.warnings)
    assert result.fixed[key] == value  # Никаких скрытых default/provider/auth substitutions.


@pytest.mark.parametrize('key', KEYS)
@pytest.mark.parametrize('value', ['', '   ', ' model/name:version ', 'ключ\nс переносом'])
def test_existing_strings_are_preserved_exactly(key, value):
    result = SettingsValidator().validate({key: value})
    assert result.valid
    assert result.fixed[key] == value
    assert not result.errors and not result.warnings


def test_defaults_conform_and_partial_patch_does_not_add_keys():
    assert all(isinstance(DEFAULT_SETTINGS[key], str) for key in KEYS)
    assert SettingsValidator().validate({key: DEFAULT_SETTINGS[key] for key in KEYS}).valid
    assert not (set(KEYS) & SettingsValidator().validate({'future_unknown_setting': 17}).fixed.keys())


@pytest.fixture
def local_settings(tmp_path, monkeypatch):
    monkeypatch.setattr('core.config.reload_settings_from_json', lambda: 0)
    store = StateStore(tmp_path / 'data')
    store.save_settings({**{key: 'existing-value' for key in KEYS},
                         'cloud_rewriter_enabled': False, 'llm_brain_preload_on_stop': False})
    service = SettingsService(store, backup=SettingsBackup(tmp_path / 'backups'))
    return service, store


@pytest.mark.parametrize('key', KEYS)
def test_bad_patch_preserves_file_existing_values_and_flags(local_settings, key, caplog):
    service, store = local_settings
    before = store.settings_path.read_bytes()
    with pytest.raises(ValueError) as error:
        service.handle_set_settings({key: {'credential': SENTINEL}})
    assert SENTINEL not in str(error.value)
    assert SENTINEL not in caplog.text
    assert store.settings_path.read_bytes() == before
    assert service.cached_settings()[key] == 'existing-value'
    assert service.handle_get_settings({})['cloud_rewriter_enabled'] is False
    assert service.handle_get_settings({})['llm_brain_preload_on_stop'] is False


@pytest.mark.parametrize('key', KEYS)
def test_good_patch_round_trips_through_disk(local_settings, key):
    service, store = local_settings
    for value in [' chosen/model ', '']:
        response = service.handle_set_settings({key: value})
        expected_public = 'REDACTED' if key == 'cloud_rewriter_api_key' and value else value
        assert response[key] == expected_public
        assert json.loads(store.settings_path.read_text())[key] == value
        service.invalidate_cache()
        assert service.handle_get_settings({})[key] == expected_public
        assert service.cached_settings()[key] == value


@pytest.mark.parametrize('key', [k for k in KEYS if k != 'cloud_rewriter_api_key'])
def test_bad_import_is_rejected_before_store_write(local_settings, tmp_path, key):
    service, store = local_settings
    before = store.settings_path.read_bytes()
    source = tmp_path / 'incoming.json'
    source.write_text(json.dumps({key: None}))
    with pytest.raises(ValueError):
        service.handle_import_settings({'file': str(source)})
    assert store.settings_path.read_bytes() == before


def test_import_keeps_existing_secret_skip_policy(local_settings, tmp_path, caplog):
    service, store = local_settings
    source = tmp_path / 'incoming.json'
    source.write_text(json.dumps({'cloud_rewriter_api_key': {'credential': SENTINEL}}))
    result = service.handle_import_settings({'file': str(source)})
    assert result['skipped'] == 1
    assert service.cached_settings()['cloud_rewriter_api_key'] == 'existing-value'
    assert SENTINEL not in caplog.text


@pytest.mark.parametrize('key', [k for k in KEYS if k != 'cloud_rewriter_api_key'])
def test_bad_backup_restore_preserves_existing_settings(local_settings, key, caplog):
    service, store = local_settings
    before = json.loads(store.settings_path.read_text())
    corrupt = {**before, key: {'credential': SENTINEL}}
    backup_id = service._backup.create_backup(corrupt, reason='invalid_type_test')
    with pytest.raises(ValueError) as error:
        service.handle_restore_settings_backup({'backup_id': backup_id})
    assert json.loads(store.settings_path.read_text()) == before
    assert service.cached_settings()[key] == 'existing-value'
    assert SENTINEL not in str(error.value) and SENTINEL not in caplog.text


def test_legacy_bad_type_blocks_partial_save_until_explicit_repair(local_settings):
    service, store = local_settings
    store.save_settings({**service.cached_settings(), 'cloud_rewriter_custom_model': None})
    service.invalidate_cache()
    before = store.settings_path.read_bytes()
    with pytest.raises(ValueError):
        service.handle_set_settings({'auto_paste': False})
    assert store.settings_path.read_bytes() == before
    repaired = service.handle_set_settings({'cloud_rewriter_custom_model': 'repaired-model'})
    assert repaired['cloud_rewriter_custom_model'] == 'repaired-model'
    assert repaired['auto_paste'] is True
