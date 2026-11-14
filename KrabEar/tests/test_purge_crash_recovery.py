"""Прерывание настоящего IPC purge и раннее восстановление synthetic профиля."""
from pathlib import Path
import os
import json
import tempfile

import pytest
from test_plaintext_export_integration import IsolatedBackend

pytestmark = pytest.mark.timeout(150)


@pytest.mark.parametrize('mode,exitcode', [('before-history', 86), ('after-history', 87), ('after-history-service', 88)])
@pytest.mark.parametrize('recovery_fault', [None, 'crash', 'partial'])
def test_crash_and_reopen(mode, exitcode, recovery_fault):
    with tempfile.TemporaryDirectory(prefix='ear-crash-', dir='/tmp') as directory:
        root = Path(directory).resolve()
        (root / 'purge-crash-mode').write_text(mode)
        key = os.urandom(32)
        backend = IsolatedBackend(root, crypto_key=key).start()
        try:
            backup = root / 'profile/backups/synthetic-copy.txt'
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text('synthetic')
            refused = backend.call('purge_all_data')
            assert refused['result']['error'] == 'confirmation_required'
            assert backup.exists() and not (root / 'profile/.purge-pending').exists()
            assert backend.call('add_history_item', text='synthetic-crash-record')['ok']
            first = backend.call('export_timeline_ical', plaintext_export=backend.grant())
            assert first['ok'] and first['result']['blocks'] == 1
            export = Path(first['result']['path'])
            assert export.is_relative_to(root) and export.is_file()
            shares = root / 'profile/shares/synthetic-copy.txt'
            shares.parent.mkdir(parents=True, exist_ok=True)
            shares.write_text('SYNTHETIC_BACKUP_MARKER')
            with pytest.raises(AssertionError):
                backend.call('purge_all_data', confirm=True)
            backend.process.join(15)
            assert backend.process.exitcode == exitcode
            backend.connection.close()
            backend.process = None
            (root / 'purge-crash-mode').unlink()
            assert (root / "profile/.purge-pending").is_file()
            if recovery_fault:
                fault_path = root / 'purge-recovery-fault'
                fault_path.write_text(recovery_fault)
                failed = IsolatedBackend(root, crypto_key=key)
                try:
                    with pytest.raises((AssertionError, EOFError)):
                        failed.start()
                    failed.process.join(15)
                    assert not failed.process.is_alive()
                    assert failed.process.exitcode == (89 if recovery_fault == 'crash' else 0)
                    assert not (root / 'ordinary-runtime-started').exists()
                    assert (root / 'profile/.purge-pending').is_file()
                finally:
                    failed.connection.close()
                    if failed.process.is_alive():
                        failed.process.terminate()
                        failed.process.join(5)
                fault_path.unlink()
            # Остатки разных stores, включая оборванную NDJSON-строку.
            inventory = {}
            if mode == 'before-history' and recovery_fault is None:
                for name, content in {
                    'transcript_versions.ndjson': '{"text":"synthetic interrupted',
                    'archive/archive.ndjson': '{"id":"synthetic","text":"synthetic"}\n',
                    'bookmarks.ndjson': '{"id":"synthetic"}\n',
                    'call_sessions.ndjson': '{"id":"synthetic"}\n',
                    'recording_chains.json': '{"chains":{"synthetic":{}}}',
                    'speaker_aliases.json': '{"SPEAKER_00":"synthetic"}',
                    'speaker_fingerprints.json': '{}',
                    'playback_stats.json': '{}',
                    'vocabulary.json': '{}',
                    'translation_cache.json': '{}',
                    'webhooks.json': '{}',
                    'embeddings.npy': 'synthetic',
                    'embeddings_index.json': '[]',
                }.items():
                    path = root / 'profile' / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content)
                    inventory[name] = path
                vault = root / 'vault'
                target = vault / 'Transcriptions/synthetic.md'
                target.parent.mkdir(parents=True)
                target.write_text('synthetic')
                (root / 'profile/obsidian_sync.json').write_text(json.dumps({
                    'vault_path': str(vault), 'folder': 'Transcriptions', 'enabled': True}))
                inventory['obsidian.md'] = target
                settings_backup = root / 'settings_backups/synthetic.json'
                settings_backup.parent.mkdir(exist_ok=True)
                settings_backup.write_text('{"synthetic":true}')
                inventory['settings_backup'] = settings_backup
            restarted = IsolatedBackend(root, crypto_key=key).start()
            try:
                assert restarted.control('seed_count')['count'] == 0
                assert not export.exists() and not shares.exists()
                for name, path in inventory.items():
                    if name == 'recording_chains.json':
                        assert json.loads(path.read_text()) == {'chains': {}}
                    elif name.endswith('.ndjson'):
                        assert not path.exists() or path.read_bytes() == b''
                    else:
                        assert not path.exists(), name
                assert not (root / 'profile/.purge-pending').exists()
                resumed = restarted.call('export_timeline_ical', plaintext_export=restarted.grant())
                assert resumed['ok'] and resumed['result']['blocks'] == 0
                # Последующая новая запись не удаляется при очередном старте.
                shares.parent.mkdir(parents=True, exist_ok=True)
                shares.write_text('synthetic-after-recovery')
            finally:
                restarted.close()
            again = IsolatedBackend(root, crypto_key=key).start()
            try:
                assert again.control("seed_count")["count"] == 0
                assert shares.read_text() == "synthetic-after-recovery"
            finally:
                again.close()
        finally:
            if backend.process is not None:
                backend.close()


def test_partial_result_blocks_new_ipc_until_restart_recovery():
    with tempfile.TemporaryDirectory(prefix='ear-partial-', dir='/tmp') as directory:
        root = Path(directory).resolve()
        fault = root / 'purge-crash-mode'
        fault.write_text('partial')
        key = os.urandom(32)
        backend = IsolatedBackend(root, crypto_key=key).start()
        try:
            assert backend.call('add_history_item', text='synthetic')['ok']
            grant = backend.grant()
            result = backend.call('purge_all_data', confirm=True)
            assert result['ok'] and result['result']['complete'] is False
            assert (root / 'profile/.purge-pending').exists()
            for method, params in (
                ('get_history_page', {}),
                ('add_history_item', {'text': 'must-not-be-admitted'}),
                ('export_timeline_ical', {'plaintext_export': grant}),
            ):
                denied = backend.call(method, **params)
                assert denied['ok'] is False
                assert denied['error']['code'] == 'purge_recovery_required'
        finally:
            backend.close()
        fault.unlink()
        reopened = IsolatedBackend(root, crypto_key=key).start()
        try:
            assert reopened.control('seed_count')['count'] == 0
            assert not (root / 'profile/.purge-pending').exists()
        finally:
            reopened.close()
