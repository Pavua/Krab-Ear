"""A5.4: production Timeline + Swift coordinator + AES-GCM на owned профиле."""
import base64
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import xml.etree.ElementTree as ET

import pytest

import test_plaintext_export_integration as plaintext_integration
from test_history_encryption_integration import assert_crypto_evidence
from test_plaintext_export_integration import (
    IsolatedBackend, assert_secret_free_streams, synthetic_subprocess_env,
)

swift_harness = plaintext_integration.swift_harness

pytestmark = pytest.mark.timeout(150)
MARKER = "syntheticcryptotimeline"
METHODS = ("export_timeline_svg", "export_timeline_json", "export_timeline_ical")


@pytest.fixture
def encrypted_backend():
    with tempfile.TemporaryDirectory(prefix="a54ts-", dir="/tmp") as directory:
        backend = IsolatedBackend(Path(directory).resolve(), crypto_key=os.urandom(32))
        try:
            seed(backend.start())
            yield backend
        finally:
            backend.close()


def protected_state(root):
    """Metadata-only audit может append; protected history/settings — byte-exact."""
    return {path.name: path.read_bytes() for path in (root / "profile").iterdir()
            if path.is_file() and (path.name.startswith("history") or path.name == "settings.json")}


def outputs(root):
    directory = root / "profile/exports/timeline"
    return {path.name: path.read_bytes() for path in directory.glob("*") if path.is_file()}


def assert_owned_tree_clean(backend):
    """Metadata-only scan применим и ДО первого чтения crypto key после restart."""
    metadata = backend.control("crypto_metadata")
    assert metadata["key_registered"] is True and metadata["positive_control"] is True
    inspection = backend.control("inspect")
    assert inspection["guard_violations"] == 0 and inspection["leaked_files"] == 0
    assert inspection["logs_clean"] is True


def seed(backend):
    response = backend.call("add_history_item", text=MARKER, paste_status="ok")
    assert_crypto_evidence(backend)
    assert response["ok"] is True and response["result"]["text"] == MARKER
    raw = (backend.root / "profile/history.ndjson").read_bytes()
    assert raw and MARKER.encode() not in raw
    assert all(line.startswith(b"ENC1:") for line in raw.splitlines() if line)


def assert_format(method, data, *, count=1):
    text = data.decode("utf-8")
    assert (MARKER in text) is bool(count)
    if method == "export_timeline_json":
        exported = json.loads(text)
        assert exported["total_blocks"] == count and exported["total_recordings"] == count
        assert len(exported["blocks"]) == count
        if count:
            assert exported["blocks"][0]["summary_text"] == MARKER
    elif method == "export_timeline_svg":
        svg = ET.fromstring(text)
        assert svg.tag == "{http://www.w3.org/2000/svg}svg"
        bars = [node for node in svg.iter() if node.tag.endswith("}rect") and node.get("id")]
        assert len(bars) == count
    else:
        assert text.startswith("BEGIN:VCALENDAR") and "END:VCALENDAR" in text
        assert text.count("BEGIN:VEVENT") == count and text.count("END:VEVENT") == count


def assert_single_output(backend, method):
    # Actual whole-tree key/auth scanner ДО raw content/assert_format funcargs.
    assert_crypto_evidence(backend)
    exported = outputs(backend.root)
    assert len(exported) == 1
    name, data = next(iter(exported.items()))
    extension = {"export_timeline_svg": ".svg", "export_timeline_json": ".json",
                 "export_timeline_ical": ".ics"}[method]
    assert name.endswith(extension)
    assert_format(method, data)
    # Whole owned tree: synthetic transcript может появиться только в allowed export.
    marker_paths = {path.relative_to(backend.root).as_posix() for path in backend.root.rglob("*")
                    if path.is_file() and MARKER.encode() in path.read_bytes()}
    assert marker_paths == {"profile/exports/timeline/" + name}


def swift_command(binary, backend, method, consent="grant", mode="run"):
    return [str(binary), str(backend.root / "ipc.sock"), "backend", method, consent, mode]


def assert_read_survives_without_export(backend):
    # Refusal deliberately precedes decrypt. После него доказываем actual AES-GCM
    # read, не выдавая grant и не создавая файлов ради криптографического proof.
    assert_owned_tree_clean(backend)
    before, old_outputs = protected_state(backend.root), outputs(backend.root)
    issued = backend.control("grant_count")
    response = backend.call("get_history_page", limit=10)
    assert backend.scan_streams(json.dumps(response))["clean"] is True
    assert_crypto_evidence(backend)
    assert response["ok"] is True
    assert [item["text"] for item in response["result"]["items"]] == [MARKER]
    assert backend.control("grant_count") == issued
    assert protected_state(backend.root) == before and outputs(backend.root) == old_outputs


def summary_streams(backend, stdout, stderr, *, granted):
    # Перед JSON parsing/assertions проверяются actual auth/key representations.
    failed = False
    result = None
    try:
        if granted:
            assert_secret_free_streams(backend, stdout, stderr, receipt=False)
        else:
            verdict = backend.scan_streams(stdout + stderr)
            assert verdict == {"clean": True, "positive_control": False,
                               "auth_kinds": {"capability": False, "receipt": False, "app_session_id": False}}
        assert_crypto_evidence(backend)
        result = json.loads(stdout)
    except Exception:
        failed = True
    finally:
        stdout = stderr = ""
    if failed:
        raise AssertionError("Timeline Swift summary/secret inspection failed") from None
    return result


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("mode", ["grant", "decline", "privacy"])
def test_swift_timeline_actual_crypto_consent(encrypted_backend, swift_harness, method, mode):
    backend = encrypted_backend
    if mode == "privacy":
        changed = backend.call("set_settings", privacy_mode_enabled=True)
        assert changed["ok"] is True
        assert backend.call("get_plaintext_export_policy")["result"]["privacy_mode_enabled"] is True
    assert_owned_tree_clean(backend)
    before = protected_state(backend.root)
    process = subprocess.run(swift_command(swift_harness, backend, method,
                             "decline" if mode == "decline" else "grant"),
                             capture_output=True, text=True, timeout=30,
                             env=synthetic_subprocess_env(backend.root))
    result = summary_streams(backend, process.stdout, process.stderr, granted=mode == "grant")
    assert process.returncode == 0
    assert result == {"consent_calls": 0 if mode == "privacy" else 1,
                      "performed": 1 if mode == "grant" else 0,
                      "blocks": 1 if mode == "grant" else 0,
                      "denied": mode != "grant", "repeated_callback_denied": mode == "grant",
                      "denial_reason": {"grant": "", "decline": "cancelled", "privacy": "privacy"}[mode]}
    assert protected_state(backend.root) == before
    if mode == "grant":
        assert_single_output(backend, method)
    else:
        assert not (backend.root / "profile/exports").exists()


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("change", ["revoke", "privacy", "restart"])
def test_live_swift_timeline_ticket_denied_after_backend_change(encrypted_backend, swift_harness, method, change):
    backend = encrypted_backend
    process = subprocess.Popen(swift_command(swift_harness, backend, method, mode="pause"),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=synthetic_subprocess_env(backend.root))
    try:
        assert select.select([process.stdout], [], [], 30)[0], "Swift prepare deadline"
        prepared = process.stdout.readline()
        assert_secret_free_streams(backend, prepared, "", receipt=False)
        assert json.loads(prepared) == {"prepared": True}
        if change == "revoke":
            assert backend.control("revoke_current") is True
        elif change == "privacy":
            assert backend.call("set_settings", privacy_mode_enabled=True)["ok"] is True
        else:
            assert_owned_tree_clean(backend)
            before_restart = protected_state(backend.root)
            backend.close()
            backend.start()
            assert_owned_tree_clean(backend)
            assert protected_state(backend.root) == before_restart
        assert_owned_tree_clean(backend)
        before = protected_state(backend.root)
        stdout, stderr = process.communicate(input="continue\n", timeout=30)
        if change == "restart":
            assert_read_survives_without_export(backend)
        # Две отдельные JSON-lines: readiness уже проверен до control mutations.
        result = summary_streams(backend, stdout, stderr, granted=True)
        assert process.returncode == 0
        assert result == {"consent_calls": 1, "performed": 0, "blocks": 0,
                          "denied": True, "repeated_callback_denied": True,
                          "denial_reason": "privacy" if change == "privacy" else "expired"}
        assert not (backend.root / "profile/exports").exists()
        assert protected_state(backend.root) == before
    finally:
        if process.poll() is None:
            process.terminate()  # Только owned test executable.
            process.communicate(timeout=5)


@pytest.mark.parametrize("method", METHODS)
def test_timeline_direct_ipc_revoke_restart_preserves_old_output(encrypted_backend, method):
    backend = encrypted_backend
    context = backend.grant()
    response = backend.call(method, plaintext_export=context)
    assert_owned_tree_clean(backend)
    assert response["ok"] is True and response["result"]["blocks"] == 1
    output = Path(response["result"]["path"])
    assert output.is_relative_to(backend.root / "profile/exports/timeline")
    assert_single_output(backend, method)
    before, old_outputs = protected_state(backend.root), outputs(backend.root)
    assert backend.call("revoke_plaintext_export_session", **{key: context[key]
                        for key in ("app_session_id", "epoch", "capability")})["result"]["ok"] is True
    revoked = backend.call(method, plaintext_export=context)["result"]
    assert_owned_tree_clean(backend)
    assert revoked["ok"] is False and revoked["reason"] == "plaintext_session_expired"
    assert revoked["path"] is None and "blocks" not in revoked
    assert protected_state(backend.root) == before and outputs(backend.root) == old_outputs
    context = backend.grant()  # Активный, не отозванный grant должен устареть при restart.
    backend.close()
    backend.start()
    assert_owned_tree_clean(backend)
    assert protected_state(backend.root) == before and outputs(backend.root) == old_outputs
    assert backend.call("get_plaintext_export_policy")["result"]["epoch"] != context["epoch"]
    stale = backend.call(method, plaintext_export=context)["result"]
    assert_owned_tree_clean(backend)
    assert stale["ok"] is False and stale["reason"] == "plaintext_session_expired"
    assert stale["path"] is None and "blocks" not in stale
    assert protected_state(backend.root) == before and outputs(backend.root) == old_outputs
    assert_read_survives_without_export(backend)


@pytest.mark.parametrize("method", METHODS)
def test_empty_encrypted_timeline_is_a_valid_export(method):
    with tempfile.TemporaryDirectory(prefix="a54te-", dir="/tmp") as directory:
        backend = IsolatedBackend(Path(directory).resolve(), crypto_key=os.urandom(32))
        try:
            backend.start()
            assert_owned_tree_clean(backend)
            before = protected_state(backend.root)
            response = backend.call(method, plaintext_export=backend.grant())
            # Truly empty profile не обязан вызывать key provider/decryption.
            assert_owned_tree_clean(backend)
            assert response["ok"] is True and response["result"]["blocks"] == 0
            exported = outputs(backend.root)
            assert len(exported) == 1
            assert_format(method, next(iter(exported.values())), count=0)
            assert protected_state(backend.root) == before
        finally:
            backend.close()


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("fault", ["wrong_key", "tamper"])
def test_timeline_crypto_read_failure_does_not_export(method, fault):
    with tempfile.TemporaryDirectory(prefix="a54t-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        key = os.urandom(32)
        writer = IsolatedBackend(root, crypto_key=key)
        try:
            seed(writer.start())
        finally:
            writer.close()
        history = root / "profile/history.ndjson"
        if fault == "tamper":
            # Base64 остаётся валидным, меняется последний byte actual GCM tag.
            payload = bytearray(base64.b64decode(history.read_bytes().strip()[5:], validate=True))
            payload[-1] ^= 1
            history.write_bytes(b"ENC1:" + base64.b64encode(payload) + b"\n")
        broken = IsolatedBackend(root, crypto_key=os.urandom(32) if fault == "wrong_key" else key)
        before = protected_state(root)
        try:
            broken.start()
            context = broken.grant()
            response = broken.call(method, plaintext_export=context)
            assert_crypto_evidence(broken)
            evidence = {"accepted": response.get("ok") is True,
                        "blocks": response.get("result", {}).get("blocks"),
                        "files": len(outputs(root))}
            assert protected_state(root) == before
            assert evidence == {"accepted": False, "blocks": None, "files": 0}, \
                "crypto read failure must deny before Timeline file write"
            assert response["error"]["code"] == "internal_error" and "result" not in response
        finally:
            broken.close()
