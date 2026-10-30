"""Bounded E2/E5 actual-handler profile; only five owned stdlib workers.
Supervisor mechanics reused from run_stt_ownership_consumer.py:30s, TERM3/KILL3,
reap/process-group check, sandbox network deny, isolated interpreter and scratch.
"""

from pathlib import Path
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
ART = Path("/Users/pablito/.codex/visualizations/2026/10/21/01a15c4e-ea0a-7d80-b807-f6c6224a9030/ear-e2-e5-causal-139d-1030")
SOURCES = ("KrabEar/backend/rest_server.py", "KrabEar/backend/activity_diagnostics.py", "KrabEar/core/operation_ownership.py", "KrabEar/core/stt_budget.py", "offline/ear_e2_e5_139d/test_causal.py", "offline/ear_e2_e5_139d/run_profile.py", "offline/ear_e2_e5_139d/README_RU.md")


def snapshot():
    return {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES
    }


def child(scratch):
    import threading

    if sys.version_info < (3, 11):
        raise RuntimeError(
            "E2/E5 profile requires Python 3.11+ (asyncio.timeout)"
        )
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeError("Child requires -I -S -B")
    scratch = Path(scratch).resolve()
    sys.pycache_prefix = str(scratch / "pycache")
    attempts = []

    def audit(event, args):
        blocked = event in (
            "subprocess.Popen",
            "os.system",
            "os.posix_spawn",
            "os.fork",
            "sqlite3.connect",
            "socket.connect",
            "socket.bind",
            "socket.getaddrinfo",
            "socket.sendto",
        ) or event.startswith("ctypes.")
        if event == "import" and args:
            blocked |= (args[0].split(".")[0] in ("app", "backend", "core")) or args[
                0
            ].startswith(
                (
                    "mlx",
                    "torch",
                    "transformers",
                    "openai",
                    "anthropic",
                    "groq",
                    "google",
                )
            )
        if event == "open" and isinstance(args[0], (str, bytes)):
            path = Path(os.fsdecode(args[0]))
            blocked |= path.name.startswith(".env") or ".remember" in path.parts
            blocked |= path.suffix in (".db", ".sqlite", ".sqlite3")
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(c in mode for c in "wxa+")) or (
                isinstance(flags, int)
                and bool(
                    flags
                    & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
                )
            )
            if writing:
                blocked |= not path.resolve().is_relative_to(scratch)
        if blocked:
            attempts.append(event)
            raise RuntimeError("Offline ready-yield gate denied: " + event)

    sys.addaudithook(audit)

    real_thread_start = threading.Thread.start
    thread_starts = []

    def owned_thread_start(thread, *args, **kwargs):
        if not thread.name.startswith("ear-causal_") or len(thread_starts) >= 5:
            attempts.append("unowned_thread.start")
            raise RuntimeError("Only five owned executor workers allowed")
        thread_starts.append(thread.name)
        return real_thread_start(thread, *args, **kwargs)

    threading.Thread.start = owned_thread_start
    import unittest

    suite = unittest.defaultTestLoader.discover(
        str(Path(__file__).resolve().parent), pattern="test_causal.py"
    )
    if suite.countTestCases() != 5:
        raise RuntimeError("Exact E2/E5 causal profile must contain 5 tests")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    app_modules = sorted(name for name in sys.modules if name.split(".")[0] in ("app", "backend", "core"))
    remaining = [
        t.name for t in threading.enumerate() if t is not threading.main_thread()
    ]
    receipt = {
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "tests_run": result.testsRun,
        "failures": [name.id() for name, _ in result.failures],
        "errors": [name.id() for name, _ in result.errors],
        "skips": [name.id() for name, _ in result.skipped],
        "forbidden_attempts": attempts,
        "remaining_threads": remaining,
        "owned_thread_starts": thread_starts,
        "app_modules_after": app_modules,
        "no_app_bootstrap": not app_modules,
    }
    (scratch / "receipt.json").write_text(json.dumps(receipt, indent=2))
    return (
        0
        if result.wasSuccessful()
        and not result.skipped
        and not attempts
        and not remaining
        and receipt["no_app_bootstrap"]
        else 1
    )


def parent(output, expected):
    if sys.version_info < (3, 11):
        raise RuntimeError(
            "E2/E5 profile requires Python 3.11+ (asyncio.timeout)"
        )
    head = subprocess.check_output(
        ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    if head != expected or subprocess.check_output(
        ["git", "-c", "core.fsmonitor=false", "status", "--porcelain"],
        cwd=ROOT,
        text=True,
    ):
        raise RuntimeError("Frozen clean HEAD required")
    before = snapshot()
    frozen = json.loads((ART / "FREEZE.json").read_text())
    if frozen["head"] != expected or frozen["source_hashes"] != before:
        raise RuntimeError("Reviewed frozen code-input mismatch")
    with tempfile.TemporaryDirectory(prefix="ear-e2-e5-offline-") as temp:
        scratch = Path(temp)
        env = {
            "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
            "HOME": temp,
            "TMPDIR": temp,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
        }
        command = [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(Path(__file__).resolve()),
            "--child",
            temp,
        ]
        if sys.platform == "darwin":
            command[:0] = [
                "/usr/bin/sandbox-exec",
                "-p",
                "(version 1)(allow default)(deny network*)",
            ]
        else:
            raise RuntimeError("Reviewed offline profile is macOS-only")
        interruptions = []
        previous = {
            sig: signal.signal(sig, lambda signum, frame: interruptions.append(signum))
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGABRT)
        }
        start = time.monotonic()
        parent_error = None
        timed_out = False
        with output.with_suffix(".log").open("w") as log:
            proc = subprocess.Popen(
                command,
                cwd=scratch,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

            def kill(sig):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    pass

            try:
                while True:
                    if interruptions:
                        raise InterruptedError("Ready-yield gate interrupted")
                    if time.monotonic() - start >= 30:
                        raise subprocess.TimeoutExpired(command, 30)
                    try:
                        proc.wait(timeout=0.25)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException as error:
                parent_error = type(error).__name__
                timed_out = isinstance(error, subprocess.TimeoutExpired)
                kill(signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    kill(signal.SIGKILL)
                    proc.wait(timeout=3)
            finally:
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    group_empty = True
                else:
                    group_empty = False
                    kill(signal.SIGKILL)
                if proc.poll() is None:
                    kill(signal.SIGKILL)
                    proc.wait(timeout=3)
        path = scratch / "receipt.json"
        receipt = json.loads(path.read_text()) if path.exists() else {}
        receipt.update(
            head=head,
            child_exit=proc.returncode,
            timed_out=timed_out,
            parent_error=parent_error,
            interruptions=interruptions,
            group_empty=group_empty,
            reaped=proc.returncode is not None,
            wall_seconds=round(time.monotonic() - start, 3),
            source_hashes=before,
            source_stable=snapshot() == before,
        )
        output.write_text(json.dumps(receipt, indent=2) + "\n")
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        print(json.dumps(receipt, indent=2))
        ok = (
            proc.returncode == 0
            and not parent_error
            and not timed_out
            and not interruptions
            and group_empty
            and receipt["source_stable"]
            and receipt.get("tests_run") == 5
            and receipt.get("forbidden_attempts") == []
            and receipt.get("remaining_threads") == []
            and receipt.get("skips") == []
            and receipt.get("no_app_bootstrap") is True
        )
        return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: run_profile.py OUTPUT.json EXPECTED_SHA")
    if sys.argv[1] == "--child":
        raise SystemExit(child(sys.argv[2]))
    raise SystemExit(parent(Path(sys.argv[1]).resolve(), sys.argv[2]))
