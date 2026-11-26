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
import threading
from pathlib import Path

import pytest

import backend.encrypted_snapshot as es


def _readback_with_timeout(data_dir: Path, files_meta: list[dict], timeout: float = 5.0) -> dict:
    """read-back в потоке с таймаутом: при регрессии O_NONBLOCK FIFO повесил бы pytest.

    Висящий поток-читатель в open(FIFO) будят открытием FIFO на запись (O_RDWR не
    блокируется), чтобы он не пережил тест.
    """
    box: dict = {}

    def run() -> None:
        box["out"] = es._readback_live_journals(data_dir=data_dir, files_meta=files_meta)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        for entry in files_meta:
            fifo = data_dir / str(entry["name"])
            try:
                os.close(os.open(str(fifo), os.O_RDWR | os.O_NONBLOCK))
            except OSError:
                pass
        th.join(2.0)
        pytest.fail(f"read-back завис на FIFO дольше {timeout}с (регрессия O_NONBLOCK)")
    return box["out"]


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
    out = _readback_with_timeout(tmp_path, [_meta("history.ndjson", b"x")])
    assert out["ok"] is False and len(out["mismatches"]) == 1
    # Именно по типу, а не по size: FIFO с писателем мог бы отдать ровно ожидаемые байты.
    assert "не обычный файл" in out["mismatches"][0]


def test_directory_is_mismatch_by_type(tmp_path: Path) -> None:
    (tmp_path / "history.ndjson").mkdir()
    out = es._readback_live_journals(
        data_dir=tmp_path, files_meta=[_meta("history.ndjson", b"")]
    )
    assert out["ok"] is False
    assert "не обычный файл" in out["mismatches"][0]


def test_fifo_with_writer_cannot_pass_with_expected_bytes(tmp_path: Path) -> None:
    body = b"line\n"
    fifo = tmp_path / "history.ndjson"
    os.mkfifo(fifo)
    wfd = os.open(str(fifo), os.O_RDWR | os.O_NONBLOCK)  # «писатель» с нужными байтами
    try:
        os.write(wfd, body)
        out = _readback_with_timeout(tmp_path, [_meta("history.ndjson", body)])
    finally:
        os.close(wfd)
    assert out["ok"] is False
    assert "не обычный файл" in out["mismatches"][0]


def test_mismatch_text_has_no_absolute_path(tmp_path: Path) -> None:
    real = tmp_path / "secret_target.bin"
    real.write_bytes(b"x")
    (tmp_path / "history.ndjson").symlink_to(real)
    out = es._readback_live_journals(
        data_dir=tmp_path, files_meta=[_meta("history.ndjson", b"x")]
    )
    assert str(tmp_path) not in " ".join(out["mismatches"])
    assert "secret_target" not in " ".join(out["mismatches"])


def test_same_size_different_content_is_caught_by_sha(tmp_path: Path) -> None:
    """Та же длина, другое содержимое: сработать должен именно sha256, а не size."""
    (tmp_path / "history.ndjson").write_bytes(b"BBBB\n")
    out = es._readback_live_journals(
        data_dir=tmp_path, files_meta=[_meta("history.ndjson", b"AAAA\n")]
    )
    assert out["ok"] is False
    assert "sha256" in out["mismatches"][0] and "size" not in out["mismatches"][0]


@pytest.mark.parametrize("body", [b"", b"abc\n"])
def test_size_and_hash_mismatch_still_detected(tmp_path: Path, body: bytes) -> None:
    (tmp_path / "history.ndjson").write_bytes(b"different\n")
    out = es._readback_live_journals(
        data_dir=tmp_path, files_meta=[_meta("history.ndjson", body)]
    )
    assert out["ok"] is False
