"""Один bounded disposable Flask/executor gate; максимум 35 секунд, без моделей."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

PROBE = Path(__file__).with_name("rest_stt_stageb_probe.py")
FLAGS = ("RATE_LIMIT_ENABLED", "LLM_ENABLED", "STT_GIGAAM_ENABLED", "REST_IN_PROCESS_ENABLED",
         "EVENT_BRIDGE_ENABLED", "AUTO_BACKUP_ENABLED", "RECAP_EMAIL_ENABLED",
         "DISK_MONITOR_ENABLED", "RECORDING_DURATION_WATCHDOG_ENABLED",
         "STT_AUTO_SEED_HOTWORDS", "SEMANTIC_SEARCH_ENABLED", "WAKE_WORD_ENABLED")



def valid_cases(cases):
    """Не принимать self-report count без полной матрицы, включая zero-worker cases."""
    expected = ([(number, call) for call in (False, True) for number in range(4)]
                + [(label, call) for call in (False, True) for label in
                   ('privacy_enabled', 'privacy_unreadable', 'empty_audio', 'null_error_success')]
                + [('expired_engine_error', True), ('expired_privacy_error', True)]
                + [('invalid_upload', False), ('invalid_upload', True)])
    if not isinstance(cases, list) or len(cases) != 20:
        return False
    for case, (label, call) in zip(cases, expected):
        if (not isinstance(case, dict) or set(case) != {'case', 'call', 'status'}
                or type(case['case']) is not type(label) or case['case'] != label
                or type(case['call']) is not bool or case['call'] is not call
                or type(case['status']) is not int):
            return False
        status = (503 if label < 3 else 200) if type(label) is int else {
            'privacy_enabled': 403, 'privacy_unreadable': 403,
            'empty_audio': 200, 'null_error_success': 200,
            'expired_engine_error': 504, 'expired_privacy_error': 403, 'invalid_upload': 400,
        }[label]
        if case['status'] != status:
            return False
    return True


def run(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    root = PROBE.resolve().parents[1]
    sources = ("backend/rest_server.py", "core/config.py", "core/engine.py",
               "backend/rest_auth.py", "backend/state_store.py", "backend/transcriber.py",
               "backend/translator.py", "backend/tts_service.py", "backend/event_bus.py",
               "backend/activity_diagnostics.py", "core/stt_router.py",
               "core/mlx_whisper_session.py")
    files = (PROBE, PROBE.with_name("rest_stageb_boundary.py"), Path(__file__),
             *(root / name for name in sources))

    def hashes():
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    before = hashes()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ear-rest-flask-") as directory:
        scratch = Path(directory).resolve()
        home, temporary = scratch / "home", scratch / "tmp"
        home.mkdir()
        temporary.mkdir()
        data = home / "Library/Application Support/KrabEar"
        env = {"PATH": os.defpath, "HOME": str(home), "TMPDIR": str(temporary),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": str(scratch / "pycache"),
               "PYTHONTZPATH": "", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
               "VECLIB_MAXIMUM_THREADS": "1", "MKL_NUM_THREADS": "1",
               "KRAB_EAR_DATA_DIR": str(data), "KRAB_EAR_MLX_WHISPER_WORKER": "0"}
        env.update({"KRAB_EAR_" + key: "false" for key in FLAGS})
        command = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)",
                   sys.executable, "-I", "-B", str(PROBE), str(scratch)]
        process = subprocess.Popen(command, cwd=scratch, env=env, start_new_session=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        timed_out = interrupted = False
        raw = b""
        cleanup_error = None
        communication_error = None
        empty = False
        try:
            try:
                raw, _ = process.communicate(timeout=35)
            except subprocess.TimeoutExpired:
                timed_out = True
            except KeyboardInterrupt:
                interrupted = True
            except BaseException as exc:
                communication_error = type(exc).__name__
        finally:
            # Only our child/group; interruption must not skip final reaping.
            previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    raw, _ = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    raw, _ = process.communicate(timeout=5)
                try:
                    os.killpg(process.pid, 0)
                    # No subprocesses are allowed by the child audit guard.
                    # A survivor is a fixture failure even if killed here.
                    cleanup_error = "unexpected group survivor"
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    empty = True
            except BaseException as exc:
                cleanup_error = type(exc).__name__
            finally:
                signal.signal(signal.SIGINT, previous_sigint)
        child_receipt = None
        child_receipt_valid = False
        try:
            lines = [line.removeprefix("REST_STAGEB_RECEIPT=")
                     for line in raw.decode("utf-8").splitlines()
                     if line.startswith("REST_STAGEB_RECEIPT=")]
            if len(lines) == 1:
                child_receipt = json.loads(lines[0])
                child_receipt_valid = (
                    isinstance(child_receipt, dict)
                    and child_receipt.get("full_rest_import") is True
                    and type(child_receipt.get("http_cases")) is int
                    and child_receipt["http_cases"] == 20
                    and valid_cases(child_receipt.get("cases"))
                    and type(child_receipt.get("worker_starts")) is int
                    and child_receipt["worker_starts"] == 18
                    and child_receipt.get("violations") == []
                    and child_receipt.get("failures") == []
                    and child_receipt.get("scope") ==
                    "real Flask upload/executor/error mapping; synthetic transcriber leaf; no ASR/runtime proof"
                )
        except (UnicodeError, ValueError, TypeError):
            pass
        receipt = {"pid": process.pid, "exit_code": process.returncode,
                   "reaped": process.poll() is not None, "pgid_empty": empty,
                   "timed_out": timed_out, "interrupted": interrupted,
                   "cleanup_error": cleanup_error, "communication_error": communication_error, "child_receipt": child_receipt,
                   "child_receipt_valid": child_receipt_valid,
                   "elapsed_sec": round(time.monotonic() - started, 3),
                   "before": before, "after": hashes(), "hash_stable": before == hashes(),
                   "command": command, "scope": "real Flask upload/executor; synthetic transcriber leaf, no ASR/runtime"}
        (output / "STAGEB_STDOUT.log").write_bytes(raw)
        (output / "STAGEB_RECEIPT.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt))
        return 0 if (process.returncode == 0 and empty and not timed_out and not interrupted
                     and cleanup_error is None and communication_error is None and child_receipt_valid
                     and receipt["hash_stable"]) else 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1]))
