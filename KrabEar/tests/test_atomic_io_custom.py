import pytest
from core.atomic_io import atomic_write_text
from unittest import mock


def test_atomic_write_text_success(tmp_path):
    target = tmp_path / "test.txt"
    atomic_write_text(target, "hello world")
    assert target.read_text() == "hello world"


def test_atomic_write_text_failure(tmp_path):
    target = tmp_path / "test2.txt"
    with mock.patch("os.replace", side_effect=OSError("Disk full")):
        with pytest.raises(OSError):
            atomic_write_text(target, "fail")

    assert not target.exists()

    # Check no temp files left
    temps = list(tmp_path.glob("*.tmp"))
    assert len(temps) == 0


def test_atomic_write_text_replace_existing(tmp_path):
    target = tmp_path / "test3.txt"
    target.write_text("old")
    atomic_write_text(target, "new")
    assert target.read_text() == "new"
