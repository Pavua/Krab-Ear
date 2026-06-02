"""Tests for TranscriptionQueue per-field byte caps (BUG 4 fix, Wave-21).

MED memory-DoS: a caller could pin ~1 GB of RAM by enqueuing 1000 jobs with
~1 MiB file_path each (the IPC framing ceiling).  The fix caps file_path at
FILE_PATH_MAX_BYTES (4096) and label at LABEL_MAX_CHARS (256) via ValueError
raised in TranscriptionJob.__init__ before any memory is committed.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_transcription_queue_bytecap_w21.py -v
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.transcription_queue import (
    FILE_PATH_MAX_BYTES,
    LABEL_MAX_CHARS,
    TranscriptionJob,
    TranscriptionQueue,
)


class TestFilePathByteCap(unittest.TestCase):
    """file_path longer than FILE_PATH_MAX_BYTES must be rejected."""

    def test_oversized_file_path_raises_value_error(self):
        """A ~1 MiB file_path must raise ValueError (not be stored)."""
        huge_path = "/" + "a" * (1024 * 1024)  # ~1 MiB
        with self.assertRaises(ValueError) as ctx:
            TranscriptionJob(file_path=huge_path)
        self.assertIn(str(FILE_PATH_MAX_BYTES), str(ctx.exception))

    def test_file_path_exactly_at_limit_is_accepted(self):
        """A file_path of exactly FILE_PATH_MAX_BYTES bytes must be accepted."""
        # Build a path whose UTF-8 encoding is exactly FILE_PATH_MAX_BYTES bytes.
        # "/" prefix (1 byte) + (FILE_PATH_MAX_BYTES - 1) ASCII chars.
        path = "/" + "x" * (FILE_PATH_MAX_BYTES - 1)
        self.assertEqual(len(path.encode("utf-8")), FILE_PATH_MAX_BYTES)
        job = TranscriptionJob(file_path=path)
        self.assertEqual(job.file_path, path)

    def test_file_path_one_byte_over_limit_is_rejected(self):
        """A file_path of FILE_PATH_MAX_BYTES + 1 bytes must be rejected."""
        path = "/" + "x" * FILE_PATH_MAX_BYTES  # FILE_PATH_MAX_BYTES + 1 bytes total
        with self.assertRaises(ValueError):
            TranscriptionJob(file_path=path)

    def test_normal_file_path_is_accepted(self):
        """A typical short file_path must enqueue without error."""
        job = TranscriptionJob(file_path="/tmp/recording.m4a")
        self.assertEqual(job.file_path, "/tmp/recording.m4a")

    def test_multibyte_utf8_file_path_byte_count_enforced(self):
        """Byte count (not character count) is used for file_path cap."""
        # Each Cyrillic char = 2 UTF-8 bytes; build a path that is short in
        # characters but exceeds FILE_PATH_MAX_BYTES in bytes.
        # FILE_PATH_MAX_BYTES // 2 + 1 Cyrillic chars > FILE_PATH_MAX_BYTES bytes.
        cyrillic_path = "/" + "А" * (FILE_PATH_MAX_BYTES // 2 + 1)
        byte_len = len(cyrillic_path.encode("utf-8"))
        self.assertGreater(byte_len, FILE_PATH_MAX_BYTES)
        with self.assertRaises(ValueError):
            TranscriptionJob(file_path=cyrillic_path)


class TestLabelCharCap(unittest.TestCase):
    """label longer than LABEL_MAX_CHARS must be rejected."""

    def test_oversized_label_raises_value_error(self):
        """A 300-character label must raise ValueError."""
        huge_label = "x" * 300
        self.assertGreater(len(huge_label), LABEL_MAX_CHARS)
        with self.assertRaises(ValueError) as ctx:
            TranscriptionJob(file_path="/tmp/audio.mp3", label=huge_label)
        self.assertIn(str(LABEL_MAX_CHARS), str(ctx.exception))

    def test_label_exactly_at_limit_is_accepted(self):
        """A label of exactly LABEL_MAX_CHARS characters must be accepted."""
        label = "L" * LABEL_MAX_CHARS
        job = TranscriptionJob(file_path="/tmp/audio.mp3", label=label)
        self.assertEqual(job.label, label)

    def test_label_one_char_over_limit_is_rejected(self):
        """A label of LABEL_MAX_CHARS + 1 characters must be rejected."""
        label = "L" * (LABEL_MAX_CHARS + 1)
        with self.assertRaises(ValueError):
            TranscriptionJob(file_path="/tmp/audio.mp3", label=label)

    def test_empty_label_is_accepted(self):
        """An empty label (default) must always be accepted."""
        job = TranscriptionJob(file_path="/tmp/audio.mp3")
        self.assertEqual(job.label, "")

    def test_normal_label_is_accepted(self):
        """A typical short label must enqueue without error."""
        job = TranscriptionJob(file_path="/tmp/audio.mp3", label="Weekly meeting")
        self.assertEqual(job.label, "Weekly meeting")


class TestEnqueueByteCap(unittest.TestCase):
    """TranscriptionQueue.enqueue() must propagate byte-cap errors."""

    def setUp(self):
        self.queue = TranscriptionQueue()

    def test_enqueue_with_oversized_path_raises_and_does_not_store(self):
        """enqueue() with a 1 MiB file_path must raise ValueError and not store the job."""
        huge_path = "/" + "a" * (1024 * 1024)
        with self.assertRaises(ValueError):
            self.queue.enqueue(file_path=huge_path)
        # Queue must remain empty — the oversized job was never stored.
        self.assertEqual(self.queue.list_queue(), [])

    def test_enqueue_with_oversized_label_raises_and_does_not_store(self):
        """enqueue() with a 300-char label must raise ValueError and not store the job."""
        with self.assertRaises(ValueError):
            self.queue.enqueue(file_path="/tmp/audio.mp3", label="x" * 300)
        self.assertEqual(self.queue.list_queue(), [])

    def test_normal_job_still_enqueues_after_rejected_job(self):
        """A normal job enqueues successfully even after a rejected oversized job."""
        # First attempt with oversized path — rejected.
        try:
            self.queue.enqueue(file_path="/" + "a" * (1024 * 1024))
        except ValueError:
            pass
        # Normal job must succeed.
        job_id = self.queue.enqueue(file_path="/tmp/normal.mp3", label="ok")
        self.assertIsNotNone(job_id)
        stats = self.queue.get_queue_stats()
        self.assertEqual(stats["pending"], 1)

    def test_handle_enqueue_with_oversized_path_propagates_value_error(self):
        """handle_enqueue() must propagate ValueError for oversized file_path."""
        huge_path = "/" + "b" * (1024 * 1024)
        # handle_enqueue re-raises ValueError (only QueueFullError is caught internally).
        with self.assertRaises(ValueError):
            self.queue.handle_enqueue({"file_path": huge_path})

    def test_handle_enqueue_with_oversized_label_propagates_value_error(self):
        """handle_enqueue() must propagate ValueError for oversized label."""
        with self.assertRaises(ValueError):
            self.queue.handle_enqueue({"file_path": "/tmp/audio.mp3", "label": "x" * 300})


class TestByteCapsConstants(unittest.TestCase):
    """Sanity-check the module-level constants."""

    def test_file_path_max_bytes_value(self):
        self.assertEqual(FILE_PATH_MAX_BYTES, 4096)

    def test_label_max_chars_value(self):
        self.assertEqual(LABEL_MAX_CHARS, 256)


if __name__ == "__main__":
    unittest.main()
