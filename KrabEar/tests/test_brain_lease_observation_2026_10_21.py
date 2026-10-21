"""Strict advisory observation, isolated from the real cross-project lease."""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import brain_lease


class LeaseObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'lease'

    def observe(self):
        return brain_lease.observe_brain_lease(self.path)

    def test_missing_does_not_create_file_or_parent(self):
        self.path = self.path / 'missing' / 'lease'
        result = self.observe()
        self.assertEqual(result['state'], 'absent')
        self.assertTrue(result['known'])
        self.assertTrue(result['advisory'])
        self.assertFalse(self.path.parent.exists())

    def test_held_expired_and_empty_are_observations_only(self):
        for expires, state in [(200.0, 'held'), (99.0, 'expired')]:
            with self.subTest(state=state):
                self.path.write_text(json.dumps(dict(owner='krab', pid=123,
                                                     acquired_ts=90.0, exp_ts=expires)))
                before = self.path.stat()
                with patch.object(brain_lease.time, 'time', return_value=100.0):
                    result = self.observe()
                self.assertEqual(result['state'], state)
                self.assertTrue(result['known'])
                self.assertTrue(result['advisory'])
                self.assertEqual(result['holder']['owner'], 'krab')
                self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)
        self.path.write_bytes(b'')
        self.assertEqual(self.observe()['state'], 'empty')

    def test_contention_never_reads_without_lock(self):
        self.path.write_bytes(b'{}')
        with self.path.open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(brain_lease.os, 'read', side_effect=AssertionError('read without lock')):
                result = self.observe()
        self.assertEqual(result['state'], 'contention')
        self.assertFalse(result['known'])

    def test_malformed_and_nonfinite_payload_do_not_look_absent(self):
        cases = [b'{', b'[]', b'{}', b'{"owner":"a","owner":"b"}',
                 b'{"owner":"a","pid":true,"acquired_ts":1,"exp_ts":2}',
                 b'{"owner":"a","pid":1,"acquired_ts":1,"exp_ts":NaN}']
        for payload in cases:
            with self.subTest(payload=payload):
                self.path.write_bytes(payload)
                result = self.observe()
                self.assertEqual(result['state'], 'malformed')
                self.assertFalse(result['known'])
                self.assertNotIn('holder', result)

    def test_oversize_and_fifo_are_bounded_unknown(self):
        self.path.write_bytes(b' ' * (65536 + 1))
        self.assertFalse(self.observe()['known'])
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertEqual(self.observe()['state'], 'unavailable')

    def test_rename_during_read_reports_replaced(self):
        self.path.write_bytes(b'')
        replacement = self.path.with_name('replacement')
        replacement.write_bytes(b'')
        read = os.read

        def replace(fd, size):
            os.replace(replacement, self.path)
            return read(fd, size)

        with patch.object(brain_lease.os, 'read', side_effect=replace):
            result = self.observe()
        self.assertEqual(result['state'], 'replaced')
        self.assertFalse(result['known'])

    def test_in_place_mutation_reports_replaced(self):
        self.path.write_bytes(b'')
        read = os.read

        def mutate(fd, size):
            self.path.write_bytes(b' ')
            return read(fd, size)

        with patch.object(brain_lease.os, 'read', side_effect=mutate):
            result = self.observe()
        self.assertEqual(result['state'], 'replaced')

    def test_permission_error_and_bad_clock_are_unknown(self):
        with patch.object(brain_lease.os, 'open', side_effect=PermissionError):
            self.assertEqual(self.observe()['state'], 'unreadable')
        self.path.write_text('{"owner":"a","pid":1,"acquired_ts":1,"exp_ts":2}')
        with patch.object(brain_lease.time, 'time', return_value=float('inf')):
            self.assertFalse(self.observe()['known'])

    def test_symlink_is_not_followed(self):
        target = self.path.with_name('target')
        target.write_bytes(b'')
        self.path.symlink_to(target)
        self.assertFalse(self.observe()['known'])


if __name__ == '__main__':
    unittest.main()
