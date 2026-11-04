"""Однократный seed не отменяет удаление; только disposable JSON, без моделей."""
import json
from pathlib import Path
import tempfile
import unittest

from backend.phonetic_vocab_service import PhoneticVocabService


class PhoneticSeedMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ear-phonetic-migration-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'phonetic_vocab.json'
        self.service = PhoneticVocabService(self.root)

    def legacy_store(self, include_new=False):
        entries = [{'canonical': 'openclaw', 'variants': ['openclow']}]
        if include_new:
            entries.append({'canonical': 'receta médica', 'variants': ['Reset America']})
        self.path.write_text(json.dumps({'entries': entries, 'extension': {'keep': True}}))

    def assert_deleted_after_list_and_reopen(self):
        for service in (self.service, PhoneticVocabService(self.root)):
            entries = service.handle_list_phonetic_entries({})['entries']
            canonicals = {entry['canonical'] for entry in entries}
            self.assertNotIn('receta médica', canonicals)
            self.assertIn('openclaw', canonicals, 'контроль: marker остаётся')

    def test_fresh_seed_remove_list_reopen(self):
        self.service.get_entries()
        self.service.handle_remove_phonetic_entry({'canonical': 'receta médica'})
        self.assert_deleted_after_list_and_reopen()

    def test_legacy_migrate_remove_list_reopen(self):
        self.legacy_store()
        self.assertIn('receta médica', {e['canonical'] for e in self.service.get_entries()})
        self.service.handle_remove_phonetic_entry({'canonical': 'receta médica'})
        self.assert_deleted_after_list_and_reopen()

    def test_legacy_remove_before_first_list_is_respected(self):
        self.legacy_store(include_new=True)
        self.service.handle_remove_phonetic_entry({'canonical': 'receta médica'})
        self.assert_deleted_after_list_and_reopen()

    def test_custom_crud_does_not_erase_migration_marker(self):
        self.service.get_entries()
        self.service.handle_remove_phonetic_entry({'canonical': 'receta médica'})
        self.service.handle_add_phonetic_entry({'canonical': 'demo', 'variants': ['demmo']})
        self.service.handle_remove_phonetic_entry({'canonical': 'demo'})
        self.assert_deleted_after_list_and_reopen()

    def test_migration_is_durable_and_does_not_rewrite_on_read(self):
        self.legacy_store()
        self.service.get_entries()
        document = json.loads(self.path.read_text())
        self.assertEqual(document.get('curated_seed_version'), 1)
        self.assertEqual(document.get('extension'), {'keep': True})
        migrated = self.path.read_bytes()
        PhoneticVocabService(self.root).get_entries()
        self.assertEqual(self.path.read_bytes(), migrated)

    def test_invalid_document_is_not_overwritten_by_migration(self):
        self.path.write_text('{not valid JSON')
        before = self.path.read_bytes()
        self.assertEqual(self.service.get_entries(), [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_future_version_is_preserved_by_crud(self):
        self.path.write_text(json.dumps({'entries': [], 'curated_seed_version': 99}))
        self.service.handle_add_phonetic_entry({'canonical': 'demo', 'variants': ['demmo']})
        self.assertEqual(json.loads(self.path.read_text()).get('curated_seed_version'), 99)

    def test_purge_keeps_same_process_empty(self):
        self.service.get_entries()
        self.service.clear_all()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.service.get_entries(), [])
        self.assertFalse(self.path.exists())


class PhoneticSeedSharedLockTests(unittest.TestCase):
    def test_migration_snapshot_is_covered_by_shared_os_lock(self):
        import fcntl
        from unittest.mock import patch

        with tempfile.TemporaryDirectory(prefix='ear-phonetic-lock-') as directory:
            root = Path(directory)
            (root / 'phonetic_vocab.json').write_text(json.dumps({
                'entries': [{'canonical': 'openclaw', 'variants': ['openclow']}]}))
            service = PhoneticVocabService(root)
            original = service._load_document
            checked = []

            def observe_snapshot():
                document = original()
                with (root / 'phonetic_vocab.lock').open('a+') as contender:
                    acquired = False
                    try:
                        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except BlockingIOError:
                        pass
                    finally:
                        if acquired:
                            fcntl.flock(contender.fileno(), fcntl.LOCK_UN)
                    self.assertFalse(acquired, 'legacy snapshot доступен второму writer без lock')
                checked.append(True)
                return document

            with patch.object(service, '_load_document', side_effect=observe_snapshot):
                service.get_entries()
            self.assertTrue(checked)

    def test_concurrent_rest_migration_cannot_undo_backend_remove(self):
        import threading
        from unittest.mock import patch

        with tempfile.TemporaryDirectory(prefix='ear-phonetic-two-writers-') as directory:
            root = Path(directory)
            (root / 'phonetic_vocab.json').write_text(json.dumps({
                'entries': [{'canonical': 'openclaw', 'variants': ['openclow']},
                            {'canonical': 'receta médica', 'variants': ['Reset America']}]}))
            rest, backend = PhoneticVocabService(root), PhoneticVocabService(root)
            loaded, resume, deleting, deleted = (threading.Event() for _ in range(4))
            errors = []
            original = rest._load_document
            first = [True]

            def paused_snapshot():
                document = original()
                if first[0]:
                    first[0] = False
                    loaded.set()
                    if not resume.wait(2):
                        raise TimeoutError('test snapshot release')
                return document

            def migrate():
                try:
                    rest.get_entries()
                except BaseException as exc:
                    errors.append(exc)

            def remove():
                deleting.set()
                try:
                    backend.handle_remove_phonetic_entry({'canonical': 'receta médica'})
                    deleted.set()
                except BaseException as exc:
                    errors.append(exc)

            with patch.object(rest, '_load_document', side_effect=paused_snapshot):
                reader = threading.Thread(target=migrate)
                writer = threading.Thread(target=remove)
                reader.start()
                try:
                    self.assertTrue(loaded.wait(1))
                    writer.start()
                    self.assertTrue(deleting.wait(1))
                    # В старом коде удаление успевает сохраниться перед stale REST save.
                    deleted.wait(0.1)
                finally:
                    resume.set()
                    reader.join(3)
                    if writer.ident is not None:
                        writer.join(3)
                self.assertFalse(reader.is_alive())
                self.assertFalse(writer.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(deleted.is_set())
            self.assertNotIn('receta médica', {e['canonical'] for e in backend.get_entries()})


    def test_lock_timeout_is_bounded_and_does_not_write(self):
        import fcntl
        from unittest.mock import patch
        import backend.phonetic_vocab_service as module

        with tempfile.TemporaryDirectory(prefix='ear-phonetic-busy-') as directory:
            root = Path(directory)
            service = PhoneticVocabService(root)
            with (root / 'phonetic_vocab.lock').open('a+') as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX)
                with patch.object(module, '_STORE_LOCK_TIMEOUT_SEC', 0.02):
                    with self.assertRaises(TimeoutError):
                        service.get_entries()
                self.assertFalse((root / 'phonetic_vocab.json').exists())

    def test_symlink_lock_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory(prefix='ear-phonetic-symlink-') as directory:
            root = Path(directory)
            target = root / 'untouched'
            target.write_text('control')
            (root / 'phonetic_vocab.lock').symlink_to(target)
            with self.assertRaises(OSError):
                PhoneticVocabService(root).get_entries()
            self.assertEqual(target.read_text(), 'control')
            self.assertFalse((root / 'phonetic_vocab.json').exists())

    def test_real_process_migration_then_remove_preserves_deletion(self):
        import os
        import select
        import subprocess
        import sys

        worker = '''
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from backend.phonetic_vocab_service import PhoneticVocabService
service = PhoneticVocabService(Path(sys.argv[2]))
if sys.argv[3] == 'migrate':
    original = service._load_document
    first = [True]
    def paused():
        document = original()
        if first[0]:
            first[0] = False
            print('snapshot', flush=True)
            if sys.stdin.readline().strip() != 'resume':
                raise RuntimeError('test parent closed')
        return document
    service._load_document = paused
    service.get_entries()
else:
    print('deleting', flush=True)
    service.handle_remove_phonetic_entry({'canonical': 'receta médica'})
print('done', flush=True)
'''
        with tempfile.TemporaryDirectory(prefix='ear-phonetic-processes-') as directory:
            root = Path(directory)
            document = root / 'phonetic_vocab.json'
            document.write_text(json.dumps({'entries': [
                {'canonical': 'openclaw', 'variants': ['openclow']},
                {'canonical': 'receta médica', 'variants': ['Reset America']}]}))
            processes = []

            def start(role):
                process = subprocess.Popen(
                    [sys.executable, '-I', '-B', '-c', worker,
                     str(Path(__file__).resolve().parents[1]), str(root), role],
                    cwd=root, env={'PATH': os.defpath, 'HOME': str(root)},
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                processes.append(process)
                return process

            def expect_line(process, expected):
                self.assertTrue(select.select([process.stdout], [], [], 3)[0])
                self.assertEqual(process.stdout.readline(), expected)

            try:
                rest = start('migrate')
                expect_line(rest, b'snapshot\n')
                backend = start('remove')
                expect_line(backend, b'deleting\n')
                self.assertFalse(select.select([backend.stdout], [], [], 0.1)[0],
                                 'delete completed while REST held migration snapshot')
                rest.stdin.write(b'resume\n')
                rest.stdin.flush()
                for process in processes:
                    _, errors = process.communicate(timeout=3)
                    self.assertEqual(process.returncode, 0, errors.decode())
                self.assertNotIn('receta médica',
                                 {e['canonical'] for e in json.loads(document.read_text())['entries']})
            finally:
                for process in processes:
                    if process.returncode is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=2)
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()


if __name__ == '__main__':
    unittest.main()
