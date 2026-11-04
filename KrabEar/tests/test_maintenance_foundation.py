"""Синтетическая проверка durable основы; не тест production drain."""

from contextlib import closing
import builtins
import importlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


class MaintenanceStoreInitializationTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="ear-maintenance-test-")
        self.addCleanup(self.scratch.cleanup)
        self.store_path = Path(self.scratch.name) / "control.sqlite3"

    def load_foundation(self):
        self.assertIsNotNone(
            importlib.util.find_spec("core.maintenance_foundation"),
            "durable maintenance foundation is not implemented",
        )
        return importlib.import_module("core.maintenance_foundation")

    def test_explicit_initialization_is_closed_and_not_a_drain_grant(self):
        foundation = self.load_foundation()
        store = foundation.initialize_new(self.store_path, "synthetic-installation")
        self.addCleanup(store.close)
        status = store.status()
        self.assertEqual(status["admission"], "closed")
        self.assertIs(status["drain_authority"], False)
        self.assertEqual(status["installation_id"], "synthetic-installation")

    def test_open_existing_never_creates_a_missing_store(self):
        foundation = self.load_foundation()
        with self.assertRaises(foundation.MaintenanceStoreError):
            foundation.open_existing(self.store_path, "synthetic-installation")
        self.assertFalse(self.store_path.exists())
        self.assertFalse((self.store_path.parent / ".maintenance.lock").exists())

    def test_explicit_initialization_refuses_to_replace_existing_file(self):
        foundation = self.load_foundation()
        self.store_path.write_bytes(b"existing sentinel")
        with self.assertRaises(foundation.MaintenanceStoreError):
            foundation.initialize_new(self.store_path, "synthetic-installation")
        self.assertEqual(self.store_path.read_bytes(), b"existing sentinel")

    def test_reopen_requires_matching_identity_and_stays_closed(self):
        foundation = self.load_foundation()
        store = foundation.initialize_new(self.store_path, "synthetic-installation")
        store.close()
        with self.assertRaises(foundation.MaintenanceStoreError):
            foundation.open_existing(self.store_path, "another-installation")
        reopened = foundation.open_existing(self.store_path, "synthetic-installation")
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.status()["admission"], "closed")
        self.assertIs(reopened.status()["drain_authority"], False)

    def test_corrupt_existing_file_is_never_reinitialized(self):
        foundation = self.load_foundation()
        foundation.initialize_new(self.store_path, "synthetic-installation").close()
        self.store_path.write_bytes(b"not a sqlite database")
        self.store_path.chmod(0o600)  # Доходим до SQLite, а не отказываем по mode.
        with self.assertRaises(foundation.MaintenanceStoreError):
            foundation.open_existing(self.store_path, "synthetic-installation")
        self.assertEqual(self.store_path.read_bytes(), b"not a sqlite database")

    def test_symlink_is_refused(self):
        foundation = self.load_foundation()
        foundation.initialize_new(self.store_path, "synthetic-installation").close()
        self.store_path.unlink()
        target = self.store_path.with_name("target.sqlite3")
        target.write_bytes(b"untouched")
        self.store_path.symlink_to(target)
        with self.assertRaises(foundation.MaintenanceStoreError):
            foundation.open_existing(self.store_path, "synthetic-installation")
        self.assertEqual(target.read_bytes(), b"untouched")

    def test_unknown_schema_or_duplicate_state_is_not_accepted(self):
        foundation = self.load_foundation()
        store = foundation.initialize_new(self.store_path, "synthetic-installation")
        store.close()
        with closing(sqlite3.connect(self.store_path)) as connection:
            connection.execute("UPDATE maintenance_state SET version = 999")
            connection.commit()
        with self.assertRaises(foundation.MaintenanceStoreError):
            foundation.open_existing(self.store_path, "synthetic-installation")
        with closing(sqlite3.connect(self.store_path)) as connection:
            connection.execute("UPDATE maintenance_state SET version = 1")
            connection.execute("INSERT INTO maintenance_state SELECT * FROM maintenance_state")
            connection.commit()
        with self.assertRaises(foundation.MaintenanceStoreError):
            foundation.open_existing(self.store_path, "synthetic-installation")

    def test_second_store_cannot_enter_locked_namespace(self):
        foundation = self.load_foundation()
        first = foundation.initialize_new(self.store_path, "synthetic-installation")
        try:
            unexpected = None
            try:
                unexpected = foundation.open_existing(self.store_path, "synthetic-installation")
            except foundation.MaintenanceStoreError:
                pass
            else:
                self.fail("namespace unlocked while first store remained open")
            finally:
                if unexpected is not None:
                    unexpected.close()
        finally:
            first.close()
        reopened = foundation.open_existing(self.store_path, "synthetic-installation")
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.status()["admission"], "closed")

    def test_lock_is_held_against_another_process(self):
        foundation = self.load_foundation()
        first = foundation.initialize_new(self.store_path, "synthetic-installation")
        self.addCleanup(first.close)
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from core.maintenance_foundation import open_existing, MaintenanceStoreError\n"
            "try:\n"
            "    store = open_existing(Path(sys.argv[1]), 'synthetic-installation')\n"
            "except MaintenanceStoreError as exc:\n"
            "    sys.exit(0 if isinstance(exc.__cause__, BlockingIOError) else 2)\n"
            "store.close()\n"
            "sys.exit(1)\n"
        )
        result = subprocess.run([sys.executable, "-c", script, str(self.store_path)],
                                timeout=10, check=False)
        self.assertEqual(result.returncode, 0)

    def test_failed_close_from_other_thread_keeps_lock_until_owner_closes(self):
        foundation = self.load_foundation()
        store = foundation.initialize_new(self.store_path, "synthetic-installation")
        errors = []

        def close_from_other_thread():
            try:
                store.close()
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=close_from_other_thread)
        worker.start()
        worker.join(timeout=5)
        try:
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], sqlite3.ProgrammingError)
            with self.assertRaises(foundation.MaintenanceStoreError):
                foundation.open_existing(self.store_path, "synthetic-installation")
        finally:
            store.close()
        foundation.open_existing(self.store_path, "synthetic-installation").close()

    def test_interruption_during_open_releases_namespace_lock(self):
        foundation = self.load_foundation()
        foundation.initialize_new(self.store_path, "synthetic-installation").close()
        with patch.object(foundation, "_connect_existing", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                foundation.open_existing(self.store_path, "synthetic-installation")
        foundation.open_existing(self.store_path, "synthetic-installation").close()

    def test_failed_probe_close_keeps_namespace_locked(self):
        foundation = self.load_foundation()
        foundation.initialize_new(self.store_path, "synthetic-installation").close()
        original_connect = foundation._connect_existing
        wrappers = []
        prior_holds = set(map(id, foundation._FAILED_OPEN_HOLDS))

        class FailingClose:
            def __init__(self, connection):
                self.connection = connection
                self.fail = True

            def execute(self, *args):
                return self.connection.execute(*args)

            def close(self):
                if self.fail:
                    raise sqlite3.OperationalError("synthetic close failure")
                self.connection.close()

        def connect(path, *, read_only):
            connection = original_connect(path, read_only=read_only)
            if not read_only:
                return connection
            wrapper = FailingClose(connection)
            wrappers.append(wrapper)
            return wrapper

        try:
            with patch.object(foundation, "_connect_existing", side_effect=connect):
                with self.assertRaises(foundation.MaintenanceStoreError):
                    foundation.open_existing(self.store_path, "synthetic-installation")
            unexpected = None
            try:
                unexpected = foundation.open_existing(self.store_path, "synthetic-installation")
            except foundation.MaintenanceStoreError:
                pass
            else:
                self.fail("namespace unlocked while probe close remained unconfirmed")
            finally:
                if unexpected is not None:
                    unexpected.close()
        finally:
            for wrapper in wrappers:
                wrapper.fail = False
                wrapper.close()
            for hold in foundation._FAILED_OPEN_HOLDS[:]:
                if id(hold) not in prior_holds:
                    hold.retry_close()
                    foundation._FAILED_OPEN_HOLDS.remove(hold)
        foundation.open_existing(self.store_path, "synthetic-installation").close()

    def test_failed_write_connection_cleanup_keeps_namespace_locked(self):
        foundation = self.load_foundation()
        foundation.initialize_new(self.store_path, "synthetic-installation").close()
        original_connect = foundation._connect_existing
        wrappers = []
        prior_holds = set(map(id, foundation._FAILED_OPEN_HOLDS))

        class FailingClose:
            def __init__(self, connection):
                self.connection = connection
                self.fail = True

            def execute(self, *args):
                return self.connection.execute(*args)

            def close(self):
                if self.fail:
                    raise sqlite3.OperationalError("synthetic close failure")
                self.connection.close()

        def connect(path, *, read_only):
            connection = original_connect(path, read_only=read_only)
            if read_only:
                return connection
            wrapper = FailingClose(connection)
            wrappers.append(wrapper)
            return wrapper

        try:
            with patch.object(foundation, "_connect_existing", side_effect=connect):
                with patch.object(foundation, "_configure", side_effect=OSError("synthetic")):
                    with self.assertRaises(foundation.MaintenanceStoreError):
                        foundation.open_existing(self.store_path, "synthetic-installation")
            unexpected = None
            try:
                unexpected = foundation.open_existing(self.store_path, "synthetic-installation")
            except foundation.MaintenanceStoreError:
                pass
            else:
                self.fail("namespace unlocked while write connection close was unconfirmed")
            finally:
                if unexpected is not None:
                    unexpected.close()
        finally:
            for wrapper in wrappers:
                wrapper.fail = False
            for hold in foundation._FAILED_OPEN_HOLDS[:]:
                if id(hold) not in prior_holds:
                    hold.retry_close()
                    foundation._FAILED_OPEN_HOLDS.remove(hold)
            for wrapper in wrappers:
                wrapper.close()
        foundation.open_existing(self.store_path, "synthetic-installation").close()

    def test_interrupted_fd_close_never_closes_reused_descriptor(self):
        foundation = self.load_foundation()
        directory_fd = os.open(self.store_path.parent, os.O_RDONLY)
        lock_fd = os.open(os.devnull, os.O_RDONLY)
        hold = foundation._FailedOpenHold([], directory_fd, lock_fd)
        original_close = os.close
        sentinel = []

        def close_then_interrupt(fd):
            original_close(fd)
            if fd == lock_fd and not sentinel:
                sentinel.append(os.open(os.devnull, os.O_RDONLY))
                raise InterruptedError("synthetic close ambiguity")

        try:
            with patch.object(foundation.os, "close", side_effect=close_then_interrupt):
                with self.assertRaises(InterruptedError):
                    hold.retry_close()
            self.assertEqual(sentinel, [lock_fd])
            with self.assertRaises(foundation.MaintenanceStoreError):
                hold.retry_close()
            os.fstat(sentinel[0])
        finally:
            if sentinel:
                try:
                    original_close(sentinel[0])
                except OSError:
                    pass
            if hold.directory_fd is not None:
                original_close(hold.directory_fd)

    def test_interrupted_store_close_never_closes_reused_descriptor(self):
        foundation = self.load_foundation()
        store = foundation.initialize_new(self.store_path, "synthetic-installation")
        lock_fd = store._lock_fd
        original_close = os.close
        sentinel = []

        def close_then_interrupt(fd):
            original_close(fd)
            if fd == lock_fd and not sentinel:
                sentinel.append(os.open(os.devnull, os.O_RDONLY))
                raise InterruptedError("synthetic close ambiguity")

        try:
            with patch.object(foundation.os, "close", side_effect=close_then_interrupt):
                with self.assertRaises(InterruptedError):
                    store.close()
            self.assertEqual(sentinel, [lock_fd])
            with self.assertRaises(foundation.MaintenanceStoreError):
                store.close()
            os.fstat(sentinel[0])
        finally:
            if sentinel:
                try:
                    original_close(sentinel[0])
                except OSError:
                    pass

    def test_interruption_between_retire_and_close_remains_unknown(self):
        foundation = self.load_foundation()
        store = foundation.initialize_new(self.store_path, "synthetic-installation")
        lock_fd = store._lock_fd

        def interrupt_after_retire(obj, name, value):
            builtins.setattr(obj, name, value)
            if obj is store and name == "_lock_fd" and value is None:
                raise KeyboardInterrupt

        try:
            with patch.object(foundation, "setattr", create=True,
                              side_effect=interrupt_after_retire):
                with self.assertRaises(KeyboardInterrupt):
                    store.close()
            with self.assertRaises(foundation.MaintenanceStoreError):
                store.close()
        finally:
            for fd in (lock_fd, store._directory_fd):
                if fd is None:
                    continue
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_hold_interruption_between_retire_and_close_remains_unknown(self):
        foundation = self.load_foundation()
        directory_fd = os.open(self.store_path.parent, os.O_RDONLY)
        lock_fd = os.open(os.devnull, os.O_RDONLY)
        hold = foundation._FailedOpenHold([], directory_fd, lock_fd)

        def interrupt_after_retire(obj, name, value):
            builtins.setattr(obj, name, value)
            if obj is hold and name == "lock_fd" and value is None:
                raise KeyboardInterrupt

        try:
            with patch.object(foundation, "setattr", create=True,
                              side_effect=interrupt_after_retire):
                with self.assertRaises(KeyboardInterrupt):
                    hold.retry_close()
            with self.assertRaises(foundation.MaintenanceStoreError):
                hold.retry_close()
        finally:
            for fd in (lock_fd, hold.directory_fd):
                if fd is None:
                    continue
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_nonprivate_namespace_and_symlink_lock_are_refused(self):
        foundation = self.load_foundation()
        directory = self.store_path.parent
        directory.chmod(0o755)
        with self.assertRaises(foundation.MaintenanceStoreError):
            foundation.initialize_new(self.store_path, "synthetic-installation")
        self.assertFalse(self.store_path.exists())
        directory.chmod(0o700)
        target = directory / "lock-target"
        target.write_bytes(b"untouched")
        (directory / ".maintenance.lock").symlink_to(target)
        with self.assertRaises(foundation.MaintenanceStoreError):
            foundation.initialize_new(self.store_path, "synthetic-installation")
        self.assertEqual(target.read_bytes(), b"untouched")

    def test_expected_rows_from_view_do_not_imply_valid_schema(self):
        foundation = self.load_foundation()
        store = foundation.initialize_new(self.store_path, "synthetic-installation")
        store.close()
        with closing(sqlite3.connect(self.store_path)) as connection:
            connection.execute("DROP TABLE maintenance_state")
            connection.execute(
                "CREATE VIEW maintenance_state AS "
                "SELECT 1 AS version, 'synthetic-installation' AS installation_id, "
                "'closed' AS admission"
            )
            connection.commit()
        with self.assertRaises(foundation.MaintenanceStoreError):
            foundation.open_existing(self.store_path, "synthetic-installation")

    def test_failed_directory_sync_does_not_report_success_or_overwrite(self):
        foundation = self.load_foundation()
        with patch.object(foundation.os, "fsync", side_effect=OSError("synthetic failure")):
            with self.assertRaises(foundation.MaintenanceStoreError):
                foundation.initialize_new(self.store_path, "synthetic-installation")
        self.assertTrue(self.store_path.exists())
        with self.assertRaises(foundation.MaintenanceStoreError):
            foundation.initialize_new(self.store_path, "synthetic-installation")


if __name__ == "__main__":
    unittest.main()
