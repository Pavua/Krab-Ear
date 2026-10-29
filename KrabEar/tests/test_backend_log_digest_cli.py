"""CLI-диагностика не должна объявлять отсутствие наблюдений здоровьем."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'backend_log_digest.py'


class BackendLogDigestCLITests(unittest.TestCase):
    def test_missing_log_is_unknown_and_nonzero(self):
        with tempfile.TemporaryDirectory() as scratch:
            missing = Path(scratch) / 'absent.log'
            result = subprocess.run(
                [sys.executable, '-I', '-B', str(SCRIPT), '--log', str(missing)],
                cwd=scratch, capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('UNKNOWN', result.stdout)
        self.assertNotIn('healthy', result.stdout.lower())
        self.assertNotIn('Traceback', result.stderr)

    def test_existing_clean_log_still_passes(self):
        with tempfile.TemporaryDirectory() as scratch:
            log = Path(scratch) / 'backend.log'
            log.write_text('2026-10-29 06:00:00 INFO idle\n')
            result = subprocess.run(
                [sys.executable, '-I', '-B', str(SCRIPT), '--log', str(log)],
                cwd=scratch, capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('UNKNOWN', result.stdout)
