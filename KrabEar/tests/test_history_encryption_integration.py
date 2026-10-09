"""A5.4: actual AES-GCM journal через CPU-only synthetic IPC, без Keychain."""
import base64
import json
import os
from pathlib import Path
import tempfile

import pytest

from test_plaintext_export_integration import IsolatedBackend

pytestmark = pytest.mark.timeout(150)
MARKER = "A54_SYNTHETIC_CRYPTO_TEXT"


def test_actual_crypto_socket_write_read_restart():
    with tempfile.TemporaryDirectory(prefix="a54c-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        key = os.urandom(32)
        backend = IsolatedBackend(root, crypto_key=key)
        try:
            backend.start()
            response = backend.call("add_history_item", text=MARKER, paste_status="ok")
            assert response["ok"] is True
            item = response["result"]
            assert item["text"] == MARKER
            assert backend.call("get_history_page", limit=10)["result"]["items"] == [item]
            assert backend.call("set_paste_status", id=item["id"], paste_status="failed")["result"]["updated"]
            assert backend.call("set_annotation", id=item["id"], note=MARKER)["result"]["note"] == MARKER
            item["paste_status"] = "failed"
            assert_crypto_evidence(backend)
            raw = (root / "profile/history.ndjson").read_bytes()
            assert raw and MARKER.encode() not in raw
            assert all(line.startswith(b"ENC1:") for line in raw.splitlines() if line)
            for name in ("history_status.ndjson", "history_annotations.ndjson"):
                journal = (root / "profile" / name).read_bytes()
                assert journal and MARKER.encode() not in journal
                assert all(line.startswith(b"ENC1:") for line in journal.splitlines() if line)
        finally:
            backend.close()
        reopened = IsolatedBackend(root, crypto_key=key)
        try:
            reopened.start()
            result = reopened.call("get_history_page", limit=10)
            assert result["ok"] is True
            assert result["result"]["items"] == [item]
            assert reopened.call("get_annotation", id=item["id"])["result"]["note"] == MARKER
            assert_crypto_evidence(reopened)
        finally:
            reopened.close()


def assert_crypto_evidence(backend):
    metadata = backend.control("crypto_metadata")
    assert metadata["provider_reads"] > 0
    assert metadata["key_registered"] is True and metadata["positive_control"] is True
    inspection = backend.control("inspect")
    assert inspection["guard_violations"] == 0
    assert inspection["leaked_files"] == 0 and inspection["logs_clean"] is True


def persistent_profile(root):
    """История/settings неизменны; штатный metadata-only audit допускает append."""
    return {str(path.relative_to(root)): path.read_bytes()
            for path in (root / "profile").rglob("*")
            if path.is_file() and not path.name.startswith("audit_")}


@pytest.mark.parametrize("fault", ["wrong_key", "tamper"])
def test_actual_crypto_failure_is_nondestructive_and_recoverable(fault):
    with tempfile.TemporaryDirectory(prefix="a54f-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        key = os.urandom(32)
        writer = IsolatedBackend(root, crypto_key=key)
        try:
            writer.start()
            item = writer.call("add_history_item", text=MARKER, paste_status="ok")["result"]
            assert_crypto_evidence(writer)
        finally:
            writer.close()
        path = root / "profile/history.ndjson"
        original = path.read_bytes()
        if fault == "tamper":
            # Валидный base64 и прежняя длина: повреждаем именно GCM tag,
            # а не отсекаем ошибку до production authentication boundary.
            payload = bytearray(base64.b64decode(original.strip()[len(b"ENC1:"):], validate=True))
            payload[-1] ^= 1
            path.write_bytes(b"ENC1:" + base64.b64encode(payload) + b"\n")
        broken = IsolatedBackend(root, crypto_key=os.urandom(32) if fault == "wrong_key" else key)
        before_boot = persistent_profile(root)
        audit_paths = list((root / "profile").glob("audit_*.ndjson"))
        before_audit = {path: path.read_bytes() for path in audit_paths}
        try:
            broken.start()
            assert persistent_profile(root) == before_boot
            for method in ("get_history_page", "compact_history"):
                response = broken.call(method)
                assert response["ok"] is False
                assert response["error"]["code"] == "internal_error"
                assert "result" not in response
                assert persistent_profile(root) == before_boot
            events = []
            for audit in (root / "profile").glob("audit_*.ndjson"):
                data = audit.read_bytes()
                prefix = before_audit.get(audit, b"")
                assert data.startswith(prefix), "failed reads must append, never rewrite audit"
                appended = data[len(prefix):]
                assert MARKER.encode() not in appended
                events.extend(json.loads(line) for line in appended.splitlines())
            assert {(event["method"], event["success"]) for event in events} == {
                ("get_history_page", False), ("compact_history", False)}
            assert all(set(event) == {"ts", "method", "params_keys", "success", "duration_ms"}
                       for event in events)
            assert_crypto_evidence(broken)
        finally:
            broken.close()
        assert persistent_profile(root) == before_boot
        path.write_bytes(original)
        recovered = IsolatedBackend(root, crypto_key=key)
        try:
            recovered.start()
            assert recovered.call("get_history_page")["result"]["items"] == [item]
            assert recovered.call("compact_history")["result"]["compacted"] is True
            assert recovered.call("get_history_page")["result"]["items"] == [item]
            assert_crypto_evidence(recovered)
        finally:
            recovered.close()


def test_actual_encrypted_export_consent_and_restart_epoch():
    with tempfile.TemporaryDirectory(prefix="a54e-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        key = os.urandom(32)
        backend = IsolatedBackend(root, crypto_key=key)
        try:
            backend.start()
            backend.call("add_history_item", text=MARKER, paste_status="ok")
            denied = backend.call("export_history", save_to_file=True)["result"]
            assert denied == {"content": "", "total_items": 0, "path": None,
                              "reason": "plaintext_confirmation_required"}
            assert not (root / "profile/transcripts").exists()
            context = backend.grant()
            exported = backend.call("export_history", save_to_file=True, plaintext_export=context)["result"]
            assert exported["total_items"] == 1 and MARKER in exported["content"]
            output = Path(exported["path"])
            assert output.is_relative_to(root / "profile/transcripts")
            assert output.read_text() == exported["content"]
            assert_crypto_evidence(backend)
        finally:
            backend.close()
        reopened = IsolatedBackend(root, crypto_key=key)
        try:
            reopened.start()
            policy = reopened.call("get_plaintext_export_policy")["result"]
            assert policy["epoch"] != context["epoch"]
            before = persistent_profile(root)
            stale = reopened.call("export_history", save_to_file=True, plaintext_export=context)["result"]
            assert stale["content"] == "" and stale["path"] is None
            assert stale["reason"] == "plaintext_session_expired"
            assert persistent_profile(root) == before
            assert reopened.call("get_history_page")["result"]["items"][0]["text"] == MARKER
            assert_crypto_evidence(reopened)
        finally:
            reopened.close()


def test_actual_stream_scanner_rejects_json_escaped_memory_key():
    with tempfile.TemporaryDirectory(prefix="a54s-", dir="/tmp") as directory:
        key = os.urandom(31) + b"\x00"  # Гарантированно есть JSON-escaped slash.
        backend = IsolatedBackend(directory, crypto_key=key)
        payload = ""
        try:
            backend.start()
            payload = json.dumps({"extra": {"probe": repr(key)}})
            result = backend.scan_streams(payload)
            assert result["clean"] is False, "serialized key escaped scanner"
        finally:
            payload = ""
            backend.close()


@pytest.mark.parametrize("sink", ["file", "event", "audit", "logging"])
def test_actual_sink_scanner_rejects_escaped_key_and_restores_baseline(sink):
    with tempfile.TemporaryDirectory(prefix="a54p-", dir="/tmp") as directory:
        backend = IsolatedBackend(directory, crypto_key=os.urandom(31) + b"\x00")
        try:
            backend.start()
            assert backend.control("crypto_sink_probe", sink=sink) == {
                "sink_reached": True, "scanner_rejected": True,
                "clean_before": True, "clean_after": True}
        finally:
            backend.close()


def test_actual_stream_scanner_key_forms_and_json_bounds():
    with tempfile.TemporaryDirectory(prefix="a54b-", dir="/tmp") as directory:
        key = os.urandom(31) + b"\x00"
        backend = IsolatedBackend(directory, crypto_key=key)
        payload = ""
        try:
            backend.start()
            assert backend.scan_streams(key)["clean"] is False, "raw bytes key escaped scanner"
            forms = (key.hex(), base64.b64encode(key).decode("ascii"), repr(key))
            for form in forms:
                for payload in (form, json.dumps({"extra": {"key": form}}),
                                json.dumps(json.dumps({"key": form})),
                                json.dumps({"noise": 1}) + "\n" + json.dumps({"key": form})):
                    assert backend.scan_streams(payload)["clean"] is False, "key form escaped scanner"
            assert backend.scan_streams(json.dumps({"extra": {"noise": ["no key", 12, None]}}))["clean"] is True
            # Невозможно доказать clean вне документированного depth budget.
            payload = '"no key"'
            for _ in range(34):
                payload = "[" + payload + "]"
            assert backend.scan_streams(payload)["clean"] is False, "depth limit silently accepted"
        finally:
            payload = ""
            backend.close()
