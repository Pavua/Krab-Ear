"""G3-privacy-04 P04-06 (остаток): read-back живых журналов не следует за symlink.

Read-back — доказательство, что на диске лежит именно восстановленный набор.
``Path.is_file()``/``read_bytes()`` следуют за symlink: журнал-ссылка на файл с
тем же содержимым (или подмена между replace и read-back) проходил проверку, хотя
живым журналом не является. Теперь читаем через ``O_NOFOLLOW`` и требуем
обычный файл (``S_ISREG``); всё иное — расхождение без утечки путей.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import backend.encrypted_snapshot as es


def _meta(name: str, body: bytes) -> dict:
    return {"name": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}


def test_regular_file_with_matching_content_is_ok(tmp_path: Path) -> None:
    body = b"line\n"
    (tmp_path / "history.ndjson").write_bytes(body)
    out = es._readback_live_journals(
        data_dir=tmp_path, files_meta=[_meta("history.ndjson", body)]
    )
    assert out["ok"] is True and out["mismatches"] == []


def test_symlink_to_identical_content_is_mismatch(tmp_path: Path) -> None:
    body = b"line\n"
    real = tmp_path / "elsewhere.bin"
    real.write_bytes(body)
    (tmp_path / "history.ndjson").symlink_to(real)
    out = es._readback_live_journals(
        data_dir=tmp_path, files_meta=[_meta("history.ndjson", body)]
    )
    assert out["ok"] is False
    assert len(out["mismatches"]) == 1
    assert out["mismatches"][0].startswith("history.ndjson")


def test_dangling_symlink_is_mismatch(tmp_path: Path) -> None:
    (tmp_path / "history.ndjson").symlink_to(tmp_path / "nowhere")
    out = es._readback_live_journals(
        data_dir=tmp_path, files_meta=[_meta("history.ndjson", b"x")]
    )
    assert out["ok"] is False and len(out["mismatches"]) == 1


def test_fifo_is_mismatch_and_does_not_block(tmp_path: Path) -> None:
    fifo = tmp_path / "history.ndjson"
    os.mkfifo(fifo)
    out = es._readback_live_journals(
        data_dir=tmp_path, files_meta=[_meta("history.ndjson", b"x")]
    )
    assert out["ok"] is False and len(out["mismatches"]) == 1


def test_mismatch_text_has_no_absolute_path(tmp_path: Path) -> None:
    real = tmp_path / "secret_target.bin"
    real.write_bytes(b"x")
    (tmp_path / "history.ndjson").symlink_to(real)
    out = es._readback_live_journals(
        data_dir=tmp_path, files_meta=[_meta("history.ndjson", b"x")]
    )
    assert str(tmp_path) not in " ".join(out["mismatches"])
    assert "secret_target" not in " ".join(out["mismatches"])


@pytest.mark.parametrize("body", [b"", b"abc\n"])
def test_size_and_hash_mismatch_still_detected(tmp_path: Path, body: bytes) -> None:
    (tmp_path / "history.ndjson").write_bytes(b"different\n")
    out = es._readback_live_journals(
        data_dir=tmp_path, files_meta=[_meta("history.ndjson", body)]
    )
    assert out["ok"] is False
