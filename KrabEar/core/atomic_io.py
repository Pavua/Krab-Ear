"""Atomic file write helper (crash-safe + concurrent-writer-safe).

`atomic_write_text` writes to a UNIQUE temp file in the same directory
(`tempfile.mkstemp`), fsyncs it, then `os.replace`s it onto the target — an
atomic rename on the same filesystem. Crash/power-loss between truncate and
write can never zero the target (the target is only ever swapped whole), and a
unique temp per call removes the shared-`.tmp` race where two writers clobber
one another's temp file. On any error the temp file is removed.
"""
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def atomic_write_text(path, text, *, encoding="utf-8"):
    """Atomically write *text* to *path* (unique temp → fsync → os.replace)."""
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_bytes(path, content):
    """Atomically write bytes with owner-only permissions."""
    with atomic_binary_writer(path) as output:
        output.write(content)


@contextmanager
def atomic_binary_writer(path):
    """Yield a private temporary file and publish it only on normal exit."""
    path = Path(path)
    parent_dir = path.parent
    parent_dir.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=parent_dir, prefix=path.name + "-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
