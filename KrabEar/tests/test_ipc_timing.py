"""Opt-in IPC metadata: без моделей, секретов и обращения к production socket."""
from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import tempfile
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]
RID = "ear-diag-" + "a" * 32


def timing():
    assert importlib.util.find_spec("backend.ipc_timing") is not None, "bounded IPC timing recorder missing"
    return importlib.import_module("backend.ipc_timing")


def payload(rid=RID, method="get_recording_state", **extra):
    return {"id": rid, "method": method, "params": {}, **extra}


class TimingStoreTests(unittest.TestCase):
    def setUp(self):
        self.mod = timing()
        self.store = self.mod.TimingStore()

    def test_opt_in_only_and_no_payload_retention(self):
        for p in (payload("usual-id"), payload(RID + "\n"),
                  payload(method="transcribe"), payload(method="arbitrary-secret"),
                  payload(params={"api_key": RID})):
            self.assertIsNone(self.store.begin(p))
        self.assertIsNone(self.store.begin(payload(params={"transcript": "PRIVATE_TEXT"})))
        self.assertIsNone(self.store.begin(payload(signature=RID)))
        trace = self.store.begin(payload())
        self.store.record(trace, "handler_started")
        result = self.store.snapshot({"request_id": RID})
        self.assertTrue(result["known"])
        self.assertEqual(result["state"], "partial")
        self.assertNotIn("PRIVATE_TEXT", json.dumps(result))
        self.assertNotIn("params", json.dumps(result))

    def test_capacity_eviction_duplicate_and_event_limit_are_explicit(self):
        self.store.begin(payload())
        self.assertIsNone(self.store.begin(payload()))
        self.assertFalse(self.store.snapshot({"request_id": RID})["known"])
        for n in range(70):
            rid = "ear-diag-" + format(n, "032x")
            self.store.begin(payload(rid))
        self.assertEqual(self.store.snapshot({"request_id": RID})["state"], "not_found")
        rid = "ear-diag-" + format(69, "032x")
        last = self.store._traces[rid]
        for _ in range(30):
            self.store.record(last, "handler_started")
        self.assertLessEqual(len(self.store._traces), 64)
        snap = self.store.snapshot({"request_id": rid})
        self.assertEqual(len(snap["events"]), 24)
        self.assertFalse(snap["known"])

    def test_nonblocking_lock_never_claims_complete_on_lost_event(self):
        trace = self.store.begin(payload())
        self.store._lock.acquire()
        try:
            self.store.record(trace, "handler_started")
            result = self.store.snapshot({"request_id": RID})
            self.assertFalse(result["known"])
            self.assertEqual(result["state"], "contended")
        finally:
            self.store._lock.release()
        self.store.record(trace, "send_finished")
        result = self.store.snapshot({"request_id": RID})
        self.assertFalse(result["known"])
        self.assertEqual(result["state"], "incomplete")

    def test_contended_duplicate_begin_invalidates_previous_completion(self):
        trace = self.store.begin(payload())
        self.store.record(trace, "send_finished")
        self.assertTrue(self.store.snapshot({"request_id": RID})["known"])
        self.store._lock.acquire()
        try:
            self.assertIsNone(self.store.begin(payload()))
        finally:
            self.store._lock.release()
        snapshot = self.store.snapshot({"request_id": RID})
        self.assertFalse(snapshot["known"])
        self.assertEqual(snapshot["state"], "incomplete")

    def test_evicted_worker_token_cannot_retain_or_append_event_lists(self):
        trace = self.store.begin(payload())
        self.store.record(trace, "handler_started")
        for n in range(70):
            self.store.begin(payload("ear-diag-" + format(n, "032x")))
        self.assertEqual(trace.events, [])
        self.store.record(trace, "handler_finished")
        self.assertEqual(trace.events, [])

    def test_clocks_and_send_failure_are_distinct_from_completion(self):
        trace = self.store.begin(payload())
        self.store.record(trace, "send_started", stamp=(100, 200))
        self.store.record(trace, "send_failed", stamp=(130, 240))
        result = self.store.snapshot({"request_id": RID})
        self.assertEqual(result["state"], "send_failed")
        self.assertEqual(result["events"][-1],
                         {"stage": "send_failed", "monotonic_ns": 130, "utc_ns": 240})
        with self.assertRaises(ValueError):
            self.store.snapshot({"request_id": "secret\n"})

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_fork_does_not_inherit_ring_or_held_collector_lock(self):
        parent = self.mod.STORE
        parent.begin(payload())
        read_fd, write_fd = os.pipe()
        parent._lock.acquire()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(read_fd)
                out = self.mod.snapshot({"request_id": RID})
                os.write(write_fd, json.dumps(out).encode())
            finally:
                os._exit(0)
        os.close(write_fd)
        parent._lock.release()
        try:
            out = json.loads(os.read(read_fd, 8192))
        finally:
            os.close(read_fd)
            os.waitpid(pid, 0)
        self.assertEqual(out["state"], "not_found")
        self.assertNotEqual(out["owner_epoch"], parent.epoch)
        self.assertEqual(out["owner_pid"], pid)


def service_harness(audit=None, privacy=False):
    """Настоящий handle_request AST без импорта ML-графа BackendService."""
    mod = timing()
    tree = ast.parse((ROOT / "backend/service.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BackendService")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "handle_request")
    ns = {"time": time, "logger": logging.getLogger("ipc-timing-test"),
          "ipc_timing": mod, "_BREADCRUMB_EXCLUDED_METHODS": set(),
          "settings": types.SimpleNamespace(IPC_SIGNING_SECRET="not-a-real-secret"),
          "add_breadcrumb": lambda **kw: None,
          "IpcOperationalError": type("IpcOperationalError", (Exception,), {})}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0), fn],
        type_ignores=[])), str(ROOT / "backend/service.py"), "exec"), ns)
    fake = types.SimpleNamespace(
        _request_signer=None, _ipc_throttle=None,
        _dispatch_table={"get_recording_state": lambda p: {"is_recording": False}},
        _get_runtime_setting=lambda key, default: privacy,
        _audit_logger=types.SimpleNamespace(log_request=audit or (lambda **kw: None)),
        _error=lambda rid, code, msg: {"id": rid, "ok": False, "error": {"code": code}},
    )
    fake.handle_request = types.MethodType(ns["handle_request"], fake)
    return fake


class TimingWiringTests(unittest.TestCase):
    def setUp(self):
        self.mod = timing()
        self.store_patch = patch.object(self.mod, "STORE", self.mod.TimingStore())
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)

    def test_actual_handler_and_audit_have_separate_spans(self):
        mod = timing()
        store = mod.TimingStore()
        trace = store.begin(payload())
        audit_started = threading.Event()
        release = threading.Event()

        def audit(**kw):
            audit_started.set()
            release.wait(2)

        service = service_harness(audit=audit)
        result = []

        def run():
            with mod.bind(trace):
                result.append(service.handle_request(payload()))

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(audit_started.wait(1))
            stages = [e["stage"] for e in store.snapshot({"request_id": RID})["events"]]
            self.assertIn("handler_finished", stages)
            self.assertIn("audit_started", stages)
            self.assertNotIn("audit_finished", stages)
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [{"id": RID, "ok": True, "result": {"is_recording": False}}])
        self.assertIn("audit_finished", [e["stage"] for e in store.snapshot({"request_id": RID})["events"]])

    def test_socketpair_preserves_reply_and_records_full_send(self):
        mod = timing()
        from backend.ipc_server import IPCServer
        service = service_harness()
        ipc = IPCServer(Path("unused.sock"), service)
        server, client = socket.socketpair()
        client.settimeout(2)
        ipc._conn_semaphore.acquire()
        client.sendall((json.dumps(payload()) + "\n").encode())
        thread = threading.Thread(target=ipc._handle_connection, args=(server,))
        thread.start()
        try:
            data = b""
            while b"\n" not in data:
                data += client.recv(4096)
        finally:
            client.close()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(json.loads(data), {"id": RID, "ok": True, "result": {"is_recording": False}})
        snap = mod.snapshot({"request_id": RID})
        stages = [e["stage"] for e in snap["events"]]
        for stage in ("connection_started", "request_decoded", "worker_submitted",
                      "worker_started", "handler_started", "handler_finished",
                      "privacy_check_started", "privacy_check_finished", "audit_started",
                      "audit_finished", "dispatch_finished", "response_serialized",
                      "send_started", "send_finished"):
            self.assertIn(stage, stages)
        self.assertEqual(snap["state"], "complete")

    def test_query_uses_normal_dispatch_auth_but_has_no_audit_io(self):
        mod = timing()
        audits = []
        service = service_harness(audit=lambda **kw: audits.append(kw))
        service._dispatch_table["get_ipc_timing"] = mod.snapshot
        query = {"id": "query", "method": "get_ipc_timing", "params": {"request_id": RID}}
        service._request_signer = object()
        rejected = service.handle_request(query)
        self.assertEqual(rejected["error"]["code"], "unauthorized")
        service._request_signer = None
        response = service.handle_request(query)
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["state"], "not_found")
        self.assertEqual(audits, [])

    def test_privacy_skips_audit_without_losing_stage_boundaries(self):
        mod = timing()
        audits = []
        service = service_harness(audit=lambda **kw: audits.append(kw), privacy=True)
        trace = mod.STORE.begin(payload())
        with mod.bind(trace):
            result = service.handle_request(payload())
        self.assertTrue(result["ok"])
        self.assertEqual(audits, [])
        stages = [e["stage"] for e in mod.snapshot({"request_id": RID})["events"]]
        self.assertIn("privacy_check_finished", stages)
        self.assertNotIn("audit_started", stages)

    def test_backstop_reply_does_not_claim_worker_completion(self):
        mod = timing()
        from backend.ipc_server import IPCServer
        entered, release = threading.Event(), threading.Event()
        service = service_harness()

        def block(_):
            entered.set()
            release.wait(2)
            return {"is_recording": False}

        service._dispatch_table["get_recording_state"] = block
        ipc = IPCServer(Path("unused.sock"), service)
        ipc._request_timeout_sec = 0.03
        server, client = socket.socketpair()
        client.settimeout(2)
        ipc._conn_semaphore.acquire()
        client.sendall((json.dumps(payload()) + "\n").encode())
        thread = threading.Thread(target=ipc._handle_connection, args=(server,))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            data = b""
            while b"\n" not in data:
                data += client.recv(4096)
            thread.join(1)
            result = json.loads(data)
            self.assertFalse(result["ok"])
            snap = mod.snapshot({"request_id": RID})
            self.assertEqual(snap["state"], "backstop_timeout")
            self.assertNotIn("dispatch_finished", [e["stage"] for e in snap["events"]])
        finally:
            release.set()
            client.close()
            thread.join(2)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            snap = mod.snapshot({"request_id": RID})
            if "dispatch_finished" in [e["stage"] for e in snap["events"]]:
                break
            time.sleep(0.001)
        self.assertEqual(snap["state"], "backstop_timeout")
        self.assertIn("dispatch_finished", [e["stage"] for e in snap["events"]])

    def test_client_disconnect_never_claims_send_success(self):
        mod = timing()
        from backend.ipc_server import IPCServer
        ipc = IPCServer(Path("unused.sock"), service_harness())
        server, client = socket.socketpair()
        ipc._conn_semaphore.acquire()
        client.sendall((json.dumps(payload()) + "\n").encode())
        client.close()
        ipc._handle_connection(server)
        snap = mod.snapshot({"request_id": RID})
        self.assertEqual(snap["state"], "send_failed")
        self.assertNotIn("send_finished", [e["stage"] for e in snap["events"]])

    def test_executor_submit_failure_still_sends_internal_error(self):
        from backend.ipc_server import IPCServer
        for rid in (RID, "ordinary-id"):
            with self.subTest(request_id=rid):
                ipc = IPCServer(Path("unused.sock"), service_harness())
                server, client = socket.socketpair()
                client.settimeout(1)
                ipc._conn_semaphore.acquire()
                client.sendall((json.dumps(payload(rid)) + "\n").encode())
                try:
                    with patch("concurrent.futures.ThreadPoolExecutor.submit",
                               side_effect=RuntimeError("executor unavailable")):
                        ipc._handle_connection(server)
                    response = json.loads(client.recv(4096))
                    self.assertEqual(response["id"], rid)
                    self.assertEqual(response["error"]["code"], "internal_error")
                finally:
                    client.close()

    def test_real_unix_listener_and_separate_query(self):
        mod = timing()
        from backend.ipc_server import IPCServer
        service = service_harness()
        service._dispatch_table["get_ipc_timing"] = mod.snapshot
        with tempfile.TemporaryDirectory(prefix="kit-", dir="/tmp") as tmp:
            path = Path(tmp) / "ipc.sock"
            ipc = IPCServer(path, service)
            listener = threading.Thread(target=ipc.serve_forever)
            listener.start()

            def call(body):
                with socket.socket(socket.AF_UNIX) as conn:
                    conn.settimeout(2)
                    conn.connect(str(path))
                    conn.sendall((json.dumps(body) + "\n").encode())
                    data = b""
                    while b"\n" not in data:
                        data += conn.recv(4096)
                    return json.loads(data)

            try:
                deadline = time.monotonic() + 2
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(0.001)
                result = call(payload())
                self.assertTrue(result["ok"])
                for _ in range(10):
                    answer = call({"id": "query", "method": "get_ipc_timing",
                                   "params": {"request_id": RID}})
                    if answer["result"]["state"] == "complete":
                        break
                self.assertTrue(answer["ok"])
                self.assertEqual(answer["result"]["state"], "complete")
                self.assertIn("accepted", [e["stage"] for e in answer["result"]["events"]])
            finally:
                ipc.stop(timeout_sec=1)
                listener.join(2)
            self.assertFalse(listener.is_alive())


if __name__ == "__main__":
    unittest.main()
