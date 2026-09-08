"""Finite-only lease contract; всегда private lock_path, без GPU и live файлов."""
from __future__ import annotations

import json
import math
import os
from types import SimpleNamespace

import pytest

import backend.brain_lease as lease
from backend.settings_backup import SettingsBackup
from backend.settings_service import SettingsService
from backend.settings_validator import SettingsValidator
from backend.state_store import StateStore
from core.config import DEFAULT_SETTINGS

KEY = 'llm_brain_lease_ttl_sec'
SECRET = 'invalid-ttl-secret-must-not-be-logged'
BAD = [float('nan'), float('inf'), float('-inf'), 'NaN', 'Infinity', '-Infinity',
       '1e9999', None, [], {'private': SECRET}, SECRET, pytest.param(10 ** 400, id='overflow-int')]


@pytest.mark.parametrize('raw', BAD)
def test_settings_normalize_invalid_ttl_to_default(raw):
    result = SettingsValidator().validate({KEY: raw})
    assert result.valid
    assert result.fixed[KEY] == DEFAULT_SETTINGS[KEY] == lease._DEFAULT_TTL_SEC == 30.0
    assert math.isfinite(result.fixed[KEY])
    assert result.warnings
    assert SECRET not in ' '.join(result.errors + result.warnings)
    json.dumps(result.fixed, allow_nan=False)


@pytest.mark.parametrize('raw', [0, -3, 0.01, 30, 1e100, '30.25', True, False])
def test_finite_durations_keep_existing_numeric_meaning(raw):
    result = SettingsValidator().validate({KEY: raw})
    assert result.valid and not result.warnings
    assert type(result.fixed[KEY]) is float
    assert result.fixed[KEY] == float(raw)


@pytest.mark.parametrize('raw', BAD)
def test_invalid_producer_input_never_creates_path(tmp_path, raw, caplog):
    path = tmp_path / 'uncreated' / 'brain.lock'
    assert lease.acquire_brain_lease('test-owner', ttl_sec=raw, lock_path=path) is True
    assert not path.parent.exists()
    assert SECRET not in caplog.text


@pytest.mark.parametrize('raw', BAD)
def test_invalid_producer_input_preserves_old_bytes(tmp_path, raw):
    path = tmp_path / 'brain.lock'
    old = json.dumps({'owner': 'test-owner', 'pid': 1, 'acquired_ts': 10, 'exp_ts': 1e100})
    path.write_text(old)
    assert lease.acquire_brain_lease('test-owner', ttl_sec=raw, lock_path=path) is True
    assert path.read_text() == old


@pytest.mark.parametrize('now,ttl', [
    (float('nan'), 30), (float('inf'), 30),
    (float('-inf'), 30), (1e308, 1e308),
])
def test_invalid_clock_or_overflow_sum_precedes_filesystem_mutation(tmp_path, monkeypatch, now, ttl):
    monkeypatch.setattr(lease, 'time', SimpleNamespace(time=lambda: now))
    path = tmp_path / 'uncreated' / 'brain.lock'
    assert lease.acquire_brain_lease('test-owner', ttl_sec=ttl, lock_path=path) is True
    assert not path.parent.exists()


@pytest.mark.parametrize('ttl', [0.0, -5.0, 0.01, 30.0, 1e100, '30.25', True, False])
def test_valid_producer_preserves_expiry_and_immediate_expiration(tmp_path, monkeypatch, ttl):
    monkeypatch.setattr(lease, 'time', SimpleNamespace(time=lambda: 1000.0))
    path = tmp_path / 'brain.lock'
    assert lease.acquire_brain_lease('test-owner', ttl_sec=ttl, lock_path=path)
    payload = json.loads(path.read_text())
    assert payload['acquired_ts'] == 1000.0
    assert payload['exp_ts'] == 1000.0 + float(ttl)
    assert math.isfinite(payload['exp_ts'])
    assert (lease.current_lease_holder(lock_path=path) is not None) == (float(ttl) > 0)


@pytest.mark.parametrize('field', ['exp_ts', 'acquired_ts'])
@pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf'), 'NaN', 'Infinity', '1e9999', None])
def test_invalid_timestamps_never_look_like_a_holder(tmp_path, monkeypatch, field, bad):
    monkeypatch.setattr(lease, 'time', SimpleNamespace(time=lambda: 1000.0))
    path = tmp_path / 'brain.lock'
    payload = {'owner': 'foreign', 'pid': 7, 'acquired_ts': 900.0, 'exp_ts': 1100.0}
    payload[field] = bad
    path.write_text(json.dumps(payload))
    assert lease.current_lease_holder(lock_path=path) is None
    assert lease.acquire_brain_lease('test-owner', ttl_sec=30, lock_path=path) is True
    assert json.loads(path.read_text())['owner'] == 'test-owner'


@pytest.mark.parametrize('now', [float('nan'), float('inf'), float('-inf')])
def test_invalid_reader_clock_returns_unknown(tmp_path, monkeypatch, now):
    path = tmp_path / 'brain.lock'
    path.write_text(json.dumps({'owner': 'foreign', 'pid': 7, 'acquired_ts': 900.0, 'exp_ts': 1100.0}))
    monkeypatch.setattr(lease, 'time', SimpleNamespace(time=lambda: now))
    assert lease.current_lease_holder(lock_path=path) is None


@pytest.mark.parametrize('with_acquired', [False, True])
def test_numeric_strings_and_optional_acquired_timestamp_stay_compatible(tmp_path, monkeypatch, with_acquired):
    monkeypatch.setattr(lease, 'time', SimpleNamespace(time=lambda: 1000.0))
    payload = {'owner': 'foreign', 'pid': 'legacy-pid', 'exp_ts': '1100.0'}
    if with_acquired:
        payload['acquired_ts'] = '900.0'
    path = tmp_path / 'brain.lock'
    path.write_text(json.dumps(payload))
    assert lease.current_lease_holder(lock_path=path) == payload
    assert lease.acquire_brain_lease('test-owner', ttl_sec=30, lock_path=path) is False
    assert json.loads(path.read_text()) == payload


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf')])
def test_writer_rejects_nonstandard_numbers_without_touching_old_payload(tmp_path, bad):
    path = tmp_path / 'brain.lock'
    path.write_text('old bytes')
    fd = os.open(path, os.O_RDWR)
    try:
        with pytest.raises(ValueError):
            lease._write_payload(fd, {'exp_ts': bad}, path)
    finally:
        os.close(fd)
    assert path.read_text() == 'old bytes'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['brain.lock']


def test_legacy_settings_cache_is_json_safe_without_implicit_disk_write(tmp_path, monkeypatch):
    monkeypatch.setattr('core.config.reload_settings_from_json', lambda: 0)
    store = StateStore(tmp_path / 'data')
    store.save_settings({KEY: float('nan'), 'llm_brain_preload_on_stop': False})
    raw_before = store.settings_path.read_bytes()
    service = SettingsService(store, backup=SettingsBackup(tmp_path / 'backups'))
    settings = service.handle_get_settings({})
    assert settings[KEY] == 30.0
    json.dumps(settings, allow_nan=False)
    assert store.settings_path.read_bytes() == raw_before
    for raw in ['Infinity', {'private': SECRET}]:
        result = service.handle_set_settings({KEY: raw})
        assert result[KEY] == 30.0
        assert json.loads(store.settings_path.read_text())[KEY] == 30.0
        assert result['llm_brain_preload_on_stop'] is False


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), 'Infinity'])
@pytest.mark.parametrize('same_owner', [False, True])
def test_release_can_clear_own_invalid_timestamps_but_never_foreign(tmp_path, bad, same_owner):
    path = tmp_path / 'brain.lock'
    payload = {'owner': 'test-owner' if same_owner else 'foreign', 'pid': 7,
               'acquired_ts': 900.0, 'exp_ts': bad}
    raw = json.dumps(payload)
    path.write_text(raw)
    lease.release_brain_lease('test-owner', lock_path=path)
    assert path.read_text() == ('' if same_owner else raw)


def test_import_normalizes_bad_ttl_and_preserves_flags(tmp_path, monkeypatch):
    monkeypatch.setattr('core.config.reload_settings_from_json', lambda: 0)
    store = StateStore(tmp_path / 'data')
    store.save_settings({KEY: 90.0, 'llm_brain_preload_on_stop': False})
    service = SettingsService(store, backup=SettingsBackup(tmp_path / 'backups'))
    incoming = tmp_path / 'incoming.json'
    incoming.write_text(json.dumps({KEY: 'Infinity'}))
    report = service.handle_import_settings({'file': str(incoming)})
    assert report['errors']  # existing import report calls numeric-fix warnings errors
    assert json.loads(store.settings_path.read_text())[KEY] == 30.0
    assert service.handle_get_settings({})['llm_brain_preload_on_stop'] is False


@pytest.mark.parametrize('field', ['pid', 'extra_diagnostic'])
@pytest.mark.parametrize('value', [float('nan'), float('inf')])
def test_non_time_metadata_does_not_reclaim_foreign_active_lease(tmp_path, monkeypatch, field, value):
    monkeypatch.setattr(lease, 'time', SimpleNamespace(time=lambda: 1000.0))
    payload = {'owner': 'foreign', 'pid': 7, 'acquired_ts': 900.0, 'exp_ts': 1100.0}
    payload[field] = value
    path = tmp_path / 'brain.lock'
    raw = json.dumps(payload)
    path.write_text(raw)
    holder = lease.current_lease_holder(lock_path=path)
    assert holder is not None and holder['owner'] == 'foreign'
    assert lease.acquire_brain_lease('test-owner', ttl_sec=30, lock_path=path) is False
    assert path.read_text() == raw
