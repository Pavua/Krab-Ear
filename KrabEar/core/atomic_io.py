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
from pathlib import Path


def atomic_write_text(path, text, *, encoding="utf-8"):
    """Atomically write *text* to *path* (unique temp → fsync → os.replace)."""
    path = Path(path)
    parent_dir = path.parent
    parent_dir.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=parent_dir, prefix=path.name + "-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
