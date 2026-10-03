"""A5.3: реальные commit-пути временного профиля без ML/Keychain."""
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from backend.integrity_checker import CheckResult, IntegrityChecker, IntegrityReport
from backend.models import DEFAULT_SETTINGS
from backend.plaintext_export_authorization import POLICY_REVISION_KEY, PolicyState
from backend.settings_service import SettingsService
from backend.state_store import StateStore, StateStoreSettingsCorruptError

FLAGS = {"history_encryption_enabled": False, "privacy_mode_enabled": False}


def make_service(store):
    svc = SettingsService(store, backup=Mock())
    svc._reload_and_fire_hooks = Mock()
    svc._maybe_disable_sentry_for_privacy = Mock()
    return svc


@pytest.fixture
def store(tmp_path):
    value = StateStore(tmp_path / "profile")
    value.initialize_startup_plaintext_policy(new_profile=True)
    return value


@pytest.mark.parametrize("payload", [{}, {"history_encryption_enabled": False}, {**FLAGS, "privacy_mode_enabled": 0}])
def test_repair_requires_explicit_exact_bool_pair(store, payload):
    store.settings_path.write_text('{}')
    with pytest.raises(StateStoreSettingsCorruptError):
        store.save_settings(payload, validated_repair=True)
    assert store.settings_path.read_text() == '{}'


def test_same_instance_cannot_reinitialize_deleted_settings(store):
    store.settings_path.unlink()
    with pytest.raises(StateStoreSettingsCorruptError):
        store.save_settings(DEFAULT_SETTINGS)
    assert not store.settings_path.exists()


def test_internal_commit_cannot_disable_enc1_history(store):
    store.history_path.write_text('ENC1:fixture\n')
    before = store.settings_path.read_bytes()
    with store._lock(), pytest.raises(StateStoreSettingsCorruptError):
        store._save_settings_unlocked(FLAGS)
    assert store.settings_path.read_bytes() == before


def test_explicit_repair_of_corrupt_profile_preserves_enc1_guard(store):
    store.settings_path.write_text('{broken')
    store.history_path.write_text('ENC1:fixture\n')
    with pytest.raises(StateStoreSettingsCorruptError):
        store.save_settings(FLAGS, validated_repair=True)
    store.save_settings({**FLAGS, "history_encryption_enabled": True}, validated_repair=True)
    assert store.read_plaintext_policy_snapshot().state is PolicyState.KNOWN_ON


@pytest.mark.parametrize('path', ['set', 'import', 'restore'])
def test_explicit_settings_paths_repair_incomplete_profile(store, tmp_path, path):
    store.settings_path.write_text('{}')
    svc = make_service(store)
    incoming = {**FLAGS, POLICY_REVISION_KEY: 'a' * 32}
    if path == 'set':
        svc.handle_set_settings(incoming)
    elif path == 'import':
        source = tmp_path / 'import.json'
        source.write_text(json.dumps(incoming))
        svc.handle_import_settings({'file': str(source)})
    else:
        svc._backup.restore_backup.return_value = incoming
        svc.handle_restore_settings_backup({'backup_id': 'fixture'})
    snapshot = store.read_plaintext_policy_snapshot()
    assert snapshot.state is PolicyState.KNOWN_OFF
    assert snapshot.internal_revision != incoming[POLICY_REVISION_KEY]


@pytest.mark.parametrize('path', ['set', 'import', 'restore'])
@pytest.mark.parametrize('incoming', [{}, {'privacy_mode_enabled': False}, {**FLAGS, 'history_encryption_enabled': 0}])
def test_defaults_never_supply_repair_consent(store, tmp_path, path, incoming):
    store.settings_path.write_text('{}')
    svc = make_service(store)
    with pytest.raises((StateStoreSettingsCorruptError, ValueError)):
        if path == 'set':
            svc.handle_set_settings(incoming)
        elif path == 'import':
            source = tmp_path / 'import.json'
            source.write_text(json.dumps(incoming))
            svc.handle_import_settings({'file': str(source)})
        else:
            svc._backup.restore_backup.return_value = dict(incoming)
            svc.handle_restore_settings_backup({'backup_id': 'fixture'})
    assert store.settings_path.read_text() == '{}'


def test_integrity_repair_does_not_reset_unknown_to_defaults(store):
    store.settings_path.write_text('{broken')
    report = IntegrityReport('errors', checks=[CheckResult('settings_json', 'error', 'corrupt', True)])
    result = IntegrityChecker().repair(store.data_dir, report)
    assert result.fixed == 0
    assert result.skipped == 1
    assert store.settings_path.read_text() == '{broken'


def test_profile_creator_must_win_atomic_mkdir(tmp_path):
    profile = tmp_path / 'racing-profile'
    original = Path.mkdir
    once = False

    def concurrent_create(path, *args, **kwargs):
        nonlocal once
        if path == profile and not once:
            once = True
            original(path)
        return original(path, *args, **kwargs)

    with patch.object(Path, 'mkdir', concurrent_create):
        store = StateStore(profile)
    assert store._created_data_dir is False
    assert store.initialize_startup_plaintext_policy(new_profile=False) == 'startup_policy_left_unknown'


def test_legacy_migration_does_not_use_unsafe_path_reader(tmp_path):
    profile = tmp_path / 'legacy'
    profile.mkdir()
    (profile / 'settings.json').write_text(json.dumps({**FLAGS, 'custom': 'retained'}))
    store = StateStore(profile)
    with patch.object(Path, 'read_bytes', side_effect=AssertionError('unsafe reread')):
        store.initialize_startup_plaintext_policy(new_profile=False)
    assert json.loads(store.settings_path.read_text())['custom'] == 'retained'
    assert store.read_plaintext_policy_snapshot().state is PolicyState.KNOWN_OFF


@pytest.mark.parametrize('path', ['direct', 'set', 'import', 'restore', 'reset', 'history_restore'])
def test_supported_paths_replace_incoming_revision(store, tmp_path, path):
    incoming = {**FLAGS, POLICY_REVISION_KEY: 'a' * 32}
    previous = store.read_plaintext_policy_snapshot().internal_revision
    svc = make_service(store)
    if path == 'direct':
        store.save_settings(incoming)
    elif path == 'reset':
        store.save_settings(DEFAULT_SETTINGS)
    elif path == 'set':
        svc.handle_set_settings(incoming)
    elif path == 'import':
        source = tmp_path / 'import.json'
        source.write_text(json.dumps(incoming))
        svc.handle_import_settings({'file': str(source)})
    elif path == 'restore':
        svc._backup.restore_backup.return_value = incoming
        svc.handle_restore_settings_backup({'backup_id': 'fixture'})
    else:
        from backend.history_service import HistoryService
        source = store.data_dir / 'backups' / 'backup_fixture'
        source.mkdir(parents=True)
        (source / 'history.ndjson').write_text('')
        (source / 'settings.json').write_text(json.dumps(incoming))
        HistoryService(store).handle_restore_history({'backup_path': str(source), 'restore_settings': True})
    snapshot = store.read_plaintext_policy_snapshot()
    assert snapshot.state is PolicyState.KNOWN_OFF
    assert snapshot.internal_revision not in (previous, incoming[POLICY_REVISION_KEY])


@pytest.mark.parametrize('inherited', ['history.ndjson', 'backups/restored.json', 'vocabulary.txt'])
def test_fresh_directory_with_inherited_state_cannot_initialize(tmp_path, inherited):
    store = StateStore(tmp_path / 'fresh')
    target = store.data_dir / inherited
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('restored data')
    assert store.initialize_startup_plaintext_policy(new_profile=True) == 'startup_policy_left_unknown'
    assert not store.settings_path.exists()


def test_settings_commit_syncs_file_then_directory(store):
    import os
    import stat
    synced = []
    original = os.fsync

    def record(fd):
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        return original(fd)

    with patch('backend.state_store.os.fsync', side_effect=record):
        store.save_settings(FLAGS)
    assert synced == [False, True]


def test_settings_backup_rejects_duplicate_policy_keys(tmp_path):
    from backend.settings_backup import SettingsBackup
    backups = SettingsBackup(backup_dir=tmp_path / 'backups')
    backups._dir.mkdir(parents=True)
    (backups._dir / 'fixture.json').write_text(
        '{"history_encryption_enabled":true,"history_encryption_enabled":false,"privacy_mode_enabled":false}'
    )
    with pytest.raises((ValueError, StateStoreSettingsCorruptError)):
        backups.restore_backup('fixture')


def test_invalid_backup_validation_does_not_write_rollback(store):
    previous = store.settings_path.read_bytes()
    svc = make_service(store)
    svc._backup.restore_backup.return_value = dict(FLAGS)
    svc.cached_settings()
    svc._validator.validate = Mock(return_value=Mock(valid=False, errors=['invalid']))
    with pytest.raises(ValueError):
        svc.handle_restore_settings_backup({'backup_id': 'fixture'})
    assert store.settings_path.read_bytes() == previous


def test_explicit_startup_marker_is_single_use(store):
    store.settings_path.unlink()
    assert store.initialize_startup_plaintext_policy(new_profile=True) == 'startup_policy_left_unknown'
    assert not store.settings_path.exists()


def test_bad_settings_source_refuses_before_restoring_history(store):
    from backend.history_service import HistoryService
    source = store.data_dir / 'backups' / 'backup_bad_settings'
    source.mkdir(parents=True)
    store.history_path.write_text('{"id":"current","text":"keep"}\n')
    before = store.history_path.read_bytes()
    (source / 'history.ndjson').write_text('{"id":"backup","text":"replacement"}\n')
    (source / 'settings.json').write_text('{broken')
    with pytest.raises((StateStoreSettingsCorruptError, ValueError)):
        HistoryService(store).handle_restore_history({'backup_path': str(source), 'restore_settings': True})
    assert store.history_path.read_bytes() == before


def test_failed_initialization_cannot_be_laundered_by_removing_inherited_state(tmp_path):
    store = StateStore(tmp_path / 'fresh')
    store.history_path.write_text('inherited')
    assert store.initialize_startup_plaintext_policy(new_profile=True) == 'startup_policy_left_unknown'
    store.history_path.write_text('')
    with pytest.raises(StateStoreSettingsCorruptError):
        store.save_settings(DEFAULT_SETTINGS)
    assert not store.settings_path.exists()


@pytest.mark.parametrize('path', ['set', 'import', 'preset', 'notification', 'recommended'])
def test_stale_partial_update_cannot_revert_other_process_policy(store, tmp_path, path):
    from backend.ipc_errors import IpcOperationalError
    svc = make_service(store)
    assert svc.cached_settings()['history_encryption_enabled'] is False
    writer = StateStore(store.data_dir)
    writer.save_settings({**FLAGS, 'history_encryption_enabled': True})
    before = store.settings_path.read_bytes()
    with pytest.raises(IpcOperationalError, match='changed'):
        if path == 'set':
            svc.handle_set_settings({'auto_paste': False})
        elif path == 'import':
            source = tmp_path / 'unrelated.json'
            source.write_text('{"auto_paste":false}')
            svc.handle_import_settings({'file': str(source)})
        elif path == 'preset':
            svc.handle_apply_profile_preset({'profile': 'meeting'})
        elif path == 'notification':
            svc.handle_set_notification_preferences({'notifications_enabled': False})
        else:
            svc._detect_tier_for_recommended_setup = Mock(return_value='M')
            svc.handle_apply_recommended_setup(
                {'dry_run': False, 'keys': ['auto_dedup_enabled']},
                probe_llm_fn=Mock(), sensevoice_cached_fn=Mock(),
            )
    assert store.settings_path.read_bytes() == before
    assert store.read_plaintext_policy_snapshot().state is PolicyState.KNOWN_ON


@pytest.mark.parametrize('path', ['set', 'import'])
@pytest.mark.parametrize('raw', [
    json.dumps({'history_encryption_enabled': False, POLICY_REVISION_KEY: 'a' * 32}),
    json.dumps({**FLAGS, POLICY_REVISION_KEY: 'invalid-revision'}),
    '{"history_encryption_enabled":true,"history_encryption_enabled":false,"privacy_mode_enabled":false,"_plaintext_export_policy_revision":"' + 'a' * 32 + '"}',
    '{broken',
    '[]',
])
def test_explicit_repair_uses_observed_raw_revision_even_when_policy_unknown(store, tmp_path, path, raw):
    store.settings_path.write_text(raw)
    svc = make_service(store)
    if path == 'set':
        svc.handle_set_settings(FLAGS)
    else:
        source = tmp_path / 'repair.json'
        source.write_text(json.dumps(FLAGS))
        svc.handle_import_settings({'file': str(source)})
    assert store.read_plaintext_policy_snapshot().state is PolicyState.KNOWN_OFF


def test_duplicate_policy_json_is_not_cached_as_known_settings(store):
    store.settings_path.write_text(
        '{"history_encryption_enabled":true,"history_encryption_enabled":false,"privacy_mode_enabled":false,"_plaintext_export_policy_revision":"' + 'a' * 32 + '"}'
    )
    assert POLICY_REVISION_KEY not in store.load_settings()


def test_load_settings_keeps_io_failures_visible(store):
    with patch.object(Path, 'read_text', side_effect=PermissionError('denied')):
        with pytest.raises(PermissionError):
            store.load_settings()


def test_unknown_policy_without_explicit_backup_flags_refuses_before_history_copy(store):
    from backend.history_service import HistoryService
    store.settings_path.write_text('{"history_encryption_enabled":false}')
    source = store.data_dir / 'backups' / 'backup_incomplete_settings'
    source.mkdir(parents=True)
    store.history_path.write_text('{"id":"current","text":"keep"}\n')
    before = store.history_path.read_bytes()
    (source / 'history.ndjson').write_text('{"id":"backup","text":"replacement"}\n')
    (source / 'settings.json').write_text('{"stt_language":"es"}')
    with pytest.raises(StateStoreSettingsCorruptError):
        HistoryService(store).handle_restore_history({'backup_path': str(source), 'restore_settings': True})
    assert store.history_path.read_bytes() == before


@pytest.mark.parametrize('path', ['template_add', 'template_remove', 'translation_add', 'translation_remove', 'glossary_apply', 'purge_glossary'])
def test_direct_rmw_writers_cannot_overwrite_intervening_policy_commit(store, path):
    from backend.ipc_errors import IpcOperationalError
    store.save_settings({**FLAGS, 'translation_glossary': {'existing': 'value'},
                         'call_quick_templates': [{'name': 'existing', 'text': 'text', 'source_lang': 'ru', 'target_lang': 'es'}]})
    writer = StateStore(store.data_dir)
    original_save = store.save_settings
    writes = []

    def intervene(settings, **kwargs):
        writer.save_settings({**writer.load_settings(), 'history_encryption_enabled': True})
        writes.append(writer.settings_path.read_bytes())
        return original_save(settings, **kwargs)

    with patch.object(store, 'save_settings', side_effect=intervene):
        if path.startswith('template_'):
            from backend.call_assist_service import CallAssistService
            service = CallAssistService(store, recorder=Mock(), transcriber=Mock())
            with pytest.raises(IpcOperationalError, match='changed'):
                if path == 'template_add':
                    service.handle_add_template({'name': 'new', 'text': 'new phrase'})
                else:
                    service.handle_remove_template({'name': 'existing'})
        elif path.startswith('translation_'):
            from backend.translation_service import TranslationService
            service = TranslationService(Mock(), store, store.load_settings, Mock())
            with pytest.raises(IpcOperationalError, match='changed'):
                if path == 'translation_add':
                    service._locked_set_glossary_item('new', 'nuevo')
                else:
                    service._locked_remove_glossary_item('existing')
        elif path == 'glossary_apply':
            from backend.glossary_auto_learn import GlossaryAutoLearnService
            service = GlossaryAutoLearnService(store, store.load_settings, Mock())
            with pytest.raises(IpcOperationalError, match='changed'):
                service.handle_apply_glossary_suggestions({
                    'selected_ids': ['new'], 'suggestions': [{'source_term': 'new', 'target_term': 'nuevo'}],
                })
        else:
            from backend.history_service import HistoryService
            # Вся файловая работа purge ограничена временным профилем. Keychain
            # здесь принципиально не вызывается: проверяем только commit glossary.
            with patch('backend.crypto_keystore.delete_history_key', return_value=True), patch(
                'backend.crypto_keystore.get_or_create_history_key', side_effect=AssertionError('no Keychain'),
            ):
                result = HistoryService(store).handle_purge_all_data({'confirm': True})
            assert 'translation_glossary' in result['errors']
            assert result['complete'] is False
    assert len(writes) == 1
    assert store.settings_path.read_bytes() == writes[0]
    assert store.read_plaintext_policy_snapshot().state is PolicyState.KNOWN_ON


@pytest.mark.parametrize('source_settings, source_history', [
    ({'history_encryption_enabled': 'false', 'privacy_mode_enabled': False}, '{"id":"backup","text":"replacement"}\n'),
    (FLAGS, 'ENC1:fixture\n'),
])
def test_restore_checks_proposed_flags_and_prospective_enc1_before_copy(store, source_settings, source_history):
    from backend.history_service import HistoryService
    source = store.data_dir / 'backups' / 'backup_invalid_proposed_policy'
    source.mkdir(parents=True)
    store.history_path.write_text('{"id":"current","text":"keep"}\n')
    before = store.history_path.read_bytes()
    (source / 'history.ndjson').write_text(source_history)
    (source / 'settings.json').write_text(json.dumps(source_settings))
    with pytest.raises(StateStoreSettingsCorruptError):
        HistoryService(store).handle_restore_history({'backup_path': str(source), 'restore_settings': True})
    assert store.history_path.read_bytes() == before


def test_pretty_serialized_output_must_fit_snapshot_cap_before_commit(store):
    before = store.settings_path.read_bytes()
    cap = len(before) + 100
    incoming = {**FLAGS, 'extra': ['x'] * 64}
    assert len(json.dumps(incoming).encode()) < cap
    with patch('backend.state_store.MAX_POLICY_BYTES', cap):
        with pytest.raises(StateStoreSettingsCorruptError, match='large'):
            store.save_settings(incoming)
    assert store.settings_path.read_bytes() == before


@pytest.mark.parametrize('path', ['add', 'remove'])
def test_translation_cas_refusal_preserves_cached_glossary_and_invalidates(store, path):
    from backend.ipc_errors import IpcOperationalError
    from backend.translation_service import TranslationService
    store.save_settings({**FLAGS, 'translation_glossary': {'existing': 'value'}})
    cached = store.load_settings()
    original_glossary = dict(cached['translation_glossary'])
    invalidate = Mock()
    service = TranslationService(Mock(), store, lambda: dict(cached), invalidate)
    writer = StateStore(store.data_dir)
    writer.save_settings({**writer.load_settings(), 'history_encryption_enabled': True})
    with pytest.raises(IpcOperationalError, match='changed'):
        if path == 'add':
            service._locked_set_glossary_item('new', 'nuevo')
        else:
            service._locked_remove_glossary_item('existing')
    assert cached['translation_glossary'] == original_glossary
    invalidate.assert_called_once_with()


@pytest.mark.parametrize('path', ['set', 'import', 'restore', 'history_restore'])
@pytest.mark.parametrize('flag', ['privacy_mode_enabled', 'history_encryption_enabled'])
def test_raw_nonbool_policy_cannot_be_normalized_into_false(store, tmp_path, path, flag):
    from backend.history_service import HistoryService
    store.save_settings({**FLAGS, 'privacy_mode_enabled': True})
    before = store.settings_path.read_bytes()
    store.history_path.write_text('{"id":"current","text":"keep"}\n')
    history_before = store.history_path.read_bytes()
    incoming = {**FLAGS, flag: 'not-a-bool'}
    svc = make_service(store)
    with pytest.raises((StateStoreSettingsCorruptError, ValueError)):
        if path == 'set':
            svc.handle_set_settings(incoming)
        elif path == 'import':
            source = tmp_path / 'malformed.json'
            source.write_text(json.dumps(incoming))
            svc.handle_import_settings({'file': str(source)})
        elif path == 'restore':
            svc._backup.restore_backup.return_value = incoming
            svc.handle_restore_settings_backup({'backup_id': 'fixture'})
        else:
            source = store.data_dir / 'backups' / 'backup_raw_invalid'
            source.mkdir(parents=True)
            (source / 'settings.json').write_text(json.dumps(incoming))
            (source / 'history.ndjson').write_text('{"id":"replacement"}\n')
            HistoryService(store).handle_restore_history({'backup_path': str(source), 'restore_settings': True})
    assert store.settings_path.read_bytes() == before
    assert store.history_path.read_bytes() == history_before
    svc._backup.create_backup.assert_not_called()


@pytest.mark.parametrize('path', ['restore', 'history_restore'])
@pytest.mark.parametrize('missing', ['privacy_mode_enabled', 'history_encryption_enabled'])
def test_full_restore_requires_raw_policy_pair_on_known_profile(store, path, missing):
    from backend.history_service import HistoryService
    store.save_settings({**FLAGS, 'privacy_mode_enabled': True})
    before = store.settings_path.read_bytes()
    store.history_path.write_text('{"id":"keep"}\n')
    history_before = store.history_path.read_bytes()
    incoming = dict(FLAGS)
    del incoming[missing]
    svc = make_service(store)
    with pytest.raises((StateStoreSettingsCorruptError, ValueError)):
        if path == 'restore':
            svc._backup.restore_backup.return_value = incoming
            svc.handle_restore_settings_backup({'backup_id': 'fixture'})
        else:
            source = store.data_dir / 'backups' / 'backup_raw_missing'
            source.mkdir(parents=True)
            (source / 'settings.json').write_text(json.dumps(incoming))
            (source / 'history.ndjson').write_text('{"id":"replacement"}\n')
            HistoryService(store).handle_restore_history({'backup_path': str(source), 'restore_settings': True})
    assert store.settings_path.read_bytes() == before
    assert store.history_path.read_bytes() == history_before
    svc._backup.create_backup.assert_not_called()


def test_direct_partial_commit_preserves_existing_policy_flags(store):
    store.save_settings({**FLAGS, 'history_encryption_enabled': True, 'privacy_mode_enabled': True})
    store.save_settings({'stt_language': 'es'})
    snapshot = store.read_plaintext_policy_snapshot()
    assert snapshot.state is PolicyState.KNOWN_ON
    assert snapshot.privacy_mode_enabled is True
