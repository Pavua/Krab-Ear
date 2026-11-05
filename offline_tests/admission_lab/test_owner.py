"""Межпроцессные тесты синтетического owner; без моделей и production."""
import json
import os
from pathlib import Path
import select
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time

import pytest

import owner as lab_owner


def send_message(connection, message):
    payload = json.dumps(message).encode("utf-8")
    connection.sendall(struct.pack("!I", len(payload)) + payload)
    connection.shutdown(socket.SHUT_WR)


def read_message(connection):
    deadline = time.monotonic() + 3

    def read_exact(count):
        data = bytearray()
        while len(data) < count:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "response deadline expired"
            connection.settimeout(remaining)
            chunk = connection.recv(count - len(data))
            assert chunk, "truncated response"
            data.extend(chunk)
        return bytes(data)

    length = struct.unpack("!I", read_exact(4))[0]
    assert 1 <= length <= 65536, "invalid response size"
    payload = read_exact(length)
    remaining = deadline - time.monotonic()
    assert remaining > 0, "response EOF deadline expired"
    connection.settimeout(remaining)
    assert connection.recv(1) == b"", "trailing response bytes"
    return json.loads(payload)


def read_event(fd):
    data = bytearray()
    deadline = time.monotonic() + 5
    while not data.endswith(b"\n"):
        assert len(data) < 512, "owner event too long"
        remaining = deadline - time.monotonic()
        assert remaining > 0 and select.select([fd], [], [], remaining)[0], "owner event timed out"
        part = os.read(fd, 1)
        assert part, "owner control channel closed"
        data.extend(part)
    return data.decode("ascii").strip()


@pytest.mark.parametrize("packet", [struct.pack("!I", 65537),
                                     struct.pack("!I", 2) + b"{}x"])
def test_response_reader_rejects_oversize_and_trailing_bytes(packet):
    reader, writer = socket.socketpair()
    try:
        writer.sendall(packet)
        writer.shutdown(socket.SHUT_WR)
        with pytest.raises(AssertionError):
            read_message(reader)
    finally:
        reader.close()
        writer.close()


def test_full_private_pipe_does_not_block_claim_and_preserves_debt(tmp_path):
    os.chmod(tmp_path, 0o700)
    store = lab_owner.create_claim_journal(tmp_path / "journal.sqlite3", "lab-installation", "controller")
    read_fd = write_fd = None
    try:
        read_fd, write_fd = os.pipe()
        store._connection.execute("UPDATE control SET admission = 'TEST_OPEN'")
        store._connection.commit()
        reserved = store.reserve_leaf("lab-client", "c", "reserve", "controller", "e", "stt")
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
        with pytest.raises(BlockingIOError):
            while True:
                os.write(write_fd, b"x" * 512)
        with pytest.raises(BlockingIOError):
            while True:
                os.write(write_fd, b"x")
        claim = {"op": "claim", "request_id": "claim", "client_incarnation": "c",
                 "executor": "e", "grant_id": reserved["grant_id"]}
        with pytest.raises(BlockingIOError):
            lab_owner._execute(store, claim, write_fd)
        status = store.status()
        assert status["outstanding_count"] == 1 and status["unknown"] is True
        with pytest.raises(BlockingIOError):
            while True:
                os.read(read_fd, 65536)
        replay, fresh = lab_owner._execute(store, claim, write_fd)
        assert replay["status"] == "already_claimed" and fresh is False
        assert os.read(read_fd, 512) == b"permit claim false\n"
    finally:
        try:
            if read_fd is not None:
                os.close(read_fd)
        finally:
            try:
                if write_fd is not None:
                    os.close(write_fd)
            finally:
                store.close()


def test_snapshot_sqlite_error_propagates_to_fatal_actor():
    class BrokenConnection:
        def execute(self, _sql):
            raise sqlite3.OperationalError("synthetic snapshot failure")

    class BrokenStore:
        _connection = BrokenConnection()

    with pytest.raises(sqlite3.OperationalError):
        lab_owner._snapshot(BrokenStore())


@pytest.fixture
def owner():
    scratch = tempfile.TemporaryDirectory(prefix="earlab-", dir="/tmp")
    notify_read = notify_write = control_read = control_write = None
    control_stream = None
    process = None
    try:
        tmp_path = Path(scratch.name)
        os.chmod(tmp_path, 0o700)
        notify_read, notify_write = os.pipe()
        control_read, control_write = os.pipe()
        script = Path(__file__).with_name("owner.py")
        process = subprocess.Popen(
            [sys.executable, str(script), "--directory", str(tmp_path),
             "--notify-fd", str(notify_write), "--control-fd", str(control_read),
             "--barrier-request", "claim-1"],
            pass_fds=(notify_write, control_read), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(notify_write)
        notify_write = None
        os.close(control_read)
        control_read = None
        control_stream = os.fdopen(control_write, "wb", buffering=0)
        control_write = None
        assert read_event(notify_read) == "ready"
        yield tmp_path / "echo.sock", notify_read, control_stream, process
    finally:
        if control_stream is not None:
            control_stream.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for fd in (notify_read, notify_write, control_read, control_write):
            if fd is not None:
                os.close(fd)
        scratch.cleanup()


def connect(path):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(3)
        connection.connect(str(path))
    except BaseException:
        connection.close()
        raise
    return connection


def exchange(path, message):
    with connect(path) as connection:
        send_message(connection, message)
        return read_message(connection)


def test_lost_ack_replay_is_receipt_without_second_permit(owner):
    path, events, control, _ = owner
    common = {"v": 0, "client_incarnation": "client-1"}
    reserve = exchange(path, {**common, "op": "reserve", "request_id": "reserve-1",
                              "kind": "stt", "executor": "worker-1"})
    assert reserve["status"] == "reserved"
    claim = {**common, "op": "claim", "request_id": "claim-1",
             "grant_id": reserve["grant_id"], "executor": "worker-1"}
    with connect(path) as connection:
        send_message(connection, claim)
        assert read_event(events) == "permit claim-1 true"
        committed = read_event(events).split()
        assert len(committed) == 3 and committed[:2] == ["committed", "claim-1"]
        connection.settimeout(0.1)
        with pytest.raises(TimeoutError):
            connection.recv(1)
        control.write(b"drop\n")
        connection.settimeout(3)
        assert connection.recv(1) == b""
    replay = exchange(path, claim)
    assert read_event(events) == "permit claim-1 false"
    assert replay["status"] == "already_claimed" and replay["claim_id"] == committed[2]
    status = exchange(path, {**common, "op": "status", "request_id": "status-1"})
    assert status["outstanding_count"] == status["claimed_count"] == 1
    assert status["unknown"] is True and status["drain_authority"] is False


def test_trailing_request_bytes_are_rejected_before_journal_write(owner):
    path, _, _, _ = owner
    message = {"v": 0, "op": "reserve", "request_id": "r", "client_incarnation": "c",
               "kind": "stt", "executor": "e"}
    payload = json.dumps(message).encode()
    with connect(path) as connection:
        connection.sendall(struct.pack("!I", len(payload)) + payload + b"x")
        connection.shutdown(socket.SHUT_WR)
        assert connection.recv(1) == b""
    status = exchange(path, {"v": 0, "op": "status", "request_id": "s",
                             "client_incarnation": "c"})
    assert status["outstanding_count"] == status["claimed_count"] == 0


@pytest.mark.parametrize("packet", [
    struct.pack("!I", 65537),
    struct.pack("!I", 3) + b"{}",
    struct.pack("!I", 3) + b"{}x",
])
def test_invalid_frame_does_not_create_debt(owner, packet):
    path, _, _, _ = owner
    with connect(path) as connection:
        connection.sendall(packet)
        connection.shutdown(socket.SHUT_WR)
        assert connection.recv(1) == b""
    status = exchange(path, {"v": 0, "op": "status", "request_id": "s",
                             "client_incarnation": "c"})
    assert status["outstanding_count"] == status["claimed_count"] == 0


def test_owner_exits_when_harness_control_closes(owner):
    _, _, control, process = owner
    control.close()
    assert process.wait(timeout=5) == 0
