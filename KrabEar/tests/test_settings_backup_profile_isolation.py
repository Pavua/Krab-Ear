"""Резервные копии принадлежат профилю, задавшему настройки."""
from types import SimpleNamespace

from backend.settings_service import SettingsService


def test_settings_backups_are_separate_for_two_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('KRAB_EAR_SETTINGS_BACKUP_DIR', raising=False)
    first = SettingsService(SimpleNamespace(data_dir=tmp_path / 'first'))
    second = SettingsService(SimpleNamespace(data_dir=tmp_path / 'second'))
    first._backup.create_backup({'label': 'first'}, reason='synthetic')
    second._backup.create_backup({'label': 'second'}, reason='synthetic')
    first_file, = (tmp_path / 'first' / 'settings_backups').glob('*.json')
    second_file, = (tmp_path / 'second' / 'settings_backups').glob('*.json')
    assert 'first' in first_file.read_text()
    assert 'second' in second_file.read_text()


def test_default_profile_keeps_legacy_backup_location(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('KRAB_EAR_SETTINGS_BACKUP_DIR', raising=False)
    profile = tmp_path / 'home' / 'Library' / 'Application Support' / 'KrabEar'
    service = SettingsService(SimpleNamespace(data_dir=profile))
    service._backup.create_backup({'label': 'synthetic'})
    assert len(list((profile / 'settings_backups').glob('*.json'))) == 1


def test_explicit_backup_override_is_honored(tmp_path, monkeypatch):
    override = tmp_path / 'explicit-backups'
    monkeypatch.setenv('KRAB_EAR_SETTINGS_BACKUP_DIR', str(override))
    service = SettingsService(SimpleNamespace(data_dir=tmp_path / 'profile'))
    service._backup.create_backup({'label': 'synthetic'})
    assert len(list(override.glob('*.json'))) == 1
