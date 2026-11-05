"""Только изолированный lab owner; не запускать внутри production."""
import argparse
import json
import os
from pathlib import Path
import select
import socket
import stat
import struct
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "KrabEar"))

from core.maintenance_claim_journal import (  # noqa: E402
    JournalConflict, JournalError, create_claim_journal,
)
from codec import decode_request  # noqa: E402


def _read_exact(connection, count, deadline):
    data = bytearray()
    while len(data) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("lab request deadline")
        connection.settimeout(remaining)
        chunk = connection.recv(count - len(data))
        if not chunk:
            raise ValueError("truncated lab request")
        data.extend(chunk)
    return bytes(data)


def _request(connection):
    deadline = time.monotonic() + 3
    header = _read_exact(connection, 4, deadline)
    length = struct.unpack("!I", header)[0]
    if not 1 <= length <= 65536:
        raise ValueError("invalid frame length")
    payload = _read_exact(connection, length, deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("lab EOF deadline")
    connection.settimeout(remaining)
    if connection.recv(1):
        raise ValueError("trailing request bytes")
    return decode_request(header + payload)


def _send(connection, response):
    payload = json.dumps(response, separators=(",", ":"), allow_nan=False).encode("utf-8")
    connection.sendall(struct.pack("!I", len(payload)) + payload)


def _notify(fd, message):
    payload = (message + "\n").encode("ascii")
    if len(payload) > 512:
        raise ValueError("lab event too long")
    os.set_blocking(fd, False)
    if os.write(fd, payload) != len(payload):
        raise OSError("partial lab event")


def _snapshot(store):
    connection = store._connection
    connection.execute("BEGIN")
    try:
        status = store.status()
        claimed = connection.execute("SELECT COUNT(claim_id) FROM scoped_grants").fetchone()[0]
        connection.rollback()
    except BaseException:
        connection.rollback()
        raise
    return {"status": "ok", "outstanding_count": status["outstanding_count"],
            "claimed_count": claimed, "unknown": status["unknown"], "drain_authority": False}


def _execute(store, request, notify_fd):
    op, key, incarnation = request["op"], request["request_id"], request["client_incarnation"]
    if op == "status":
        return _snapshot(store), False
    try:
        if op == "reserve":
            result = store.reserve_leaf("lab-client", incarnation, key, "controller",
                                        request["executor"], request["kind"])
            status = "reserved" if result["outcome"] == "RESERVED" else "denied"
            return {"status": status, "grant_id": result["grant_id"]}, False
        result = store.claim_leaf("lab-client", incarnation, key, "controller",
                                  request["executor"], request["grant_id"])
        fresh = result["permit"] is not None
        _notify(notify_fd, f"permit {key} {str(fresh).lower()}")
        return {"status": "claimed" if fresh else "already_claimed",
                "claim_id": result["claim_id"]}, fresh
    except JournalConflict:
        return {"status": "conflict", "grant_id" if op == "reserve" else "claim_id": None}, False
    except JournalError:
        return {"status": "unavailable", "grant_id" if op == "reserve" else "claim_id": None}, False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--notify-fd", type=int, required=True)
    parser.add_argument("--control-fd", type=int, required=True)
    parser.add_argument("--barrier-request", required=True)
    args = parser.parse_args()
    directory = Path(args.directory)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("lab directory must be private")
    store = create_claim_journal(directory / "journal.sqlite3", "lab-installation", "controller")
    try:
        # Только новый изолированный fixture; wire OPEN отсутствует.
        store._connection.execute("UPDATE control SET admission = 'TEST_OPEN'")
        store._connection.commit()
        barrier_used = False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(directory / "echo.sock"))
            listener.listen()
            _notify(args.notify_fd, "ready")
            while True:
                readable, _, _ = select.select([listener, args.control_fd], [], [])
                # Контрольный канал принадлежит harness; EOF либо неожиданная
                # команда вне барьера завершают только этот lab owner.
                if args.control_fd in readable:
                    break
                connection, _ = listener.accept()
                with connection:
                    try:
                        request = _request(connection)
                        response, fresh = _execute(store, request, args.notify_fd)
                        if fresh and request["request_id"] == args.barrier_request and not barrier_used:
                            barrier_used = True
                            _notify(args.notify_fd, f"committed {args.barrier_request} {response['claim_id']}")
                            if not select.select([args.control_fd], [], [], 5)[0]:
                                continue
                            if os.read(args.control_fd, 32) != b"release\n":
                                continue
                        _send(connection, {"v": 0, "request_id": request["request_id"], **response})
                    except (OSError, TimeoutError, ValueError, JournalError):
                        continue
    finally:
        store.close()


if __name__ == "__main__":
    main()
