#!/usr/bin/env python3
"""Bounded per-file fallback: parallel first, serial retry of exit 124 only."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time


def pytest_command(file, seconds):
    return [
        sys.executable, str(Path(__file__).with_name("run_isolated_pytest.py")),
        "timeout", str(seconds), sys.executable, "-m", "pytest", file,
        "--tb=short", "-q", "-p", "no:xdist", "-p", "no:cacheprovider", "--timeout=75",
    ]


def _annotation(text):
    return str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _atomic_json(path, data):
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix="pending-", suffix=".json.tmp", delete=False,
    ) as stream:
        pending = Path(stream.name)
        json.dump(data, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(stream, data):
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if not written:
            raise OSError("short log write")
        view = view[written:]


def _stop_owned(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()  # isolated runner handles TERM and reaps its owned group
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    else:
        process.wait()


def _run_one(index, file, attempt, seconds, root, build_command, stop, deadline):
    log_name = f"{index:04d}-{attempt}.log"
    row = dict(index=index, file=file, attempt=attempt, limit_seconds=seconds,
               log=log_name, started=time.monotonic(), state="error", rc=None)
    process = None
    selector = selectors.DefaultSelector()
    digest = hashlib.sha256()
    size = 0
    try:
        with (root / log_name).open("xb", buffering=0) as log:
            if stop.is_set() or time.monotonic() >= deadline:
                raise RuntimeError("cancelled or fallback budget exhausted before launch")
            process = subprocess.Popen(build_command(file, seconds), stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            row["pid"] = process.pid
            selector.register(process.stdout, selectors.EVENT_READ)
            eof = False
            exited_at = None
            while not eof or process.poll() is None:
                if stop.is_set() or time.monotonic() >= deadline:
                    raise RuntimeError("cancelled or fallback budget exhausted")
                if process.poll() is not None:
                    exited_at = exited_at or time.monotonic()
                    if not eof and time.monotonic() - exited_at > 5:
                        raise RuntimeError("output pipe did not close after runner exit")
                if eof:
                    time.sleep(0.05)
                    continue
                for key, _ in selector.select(timeout=0.1):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        eof = True
                    else:
                        _write_all(log, data)
                        digest.update(data)
                        size += len(data)
            rc = process.wait()
            row["rc"] = rc if rc >= 0 else 128 - rc
            os.fsync(log.fileno())
            row.update(state="complete", log_bytes=size, log_sha256=digest.hexdigest())
    except Exception as error:
        row.update(state="error", error=f"{type(error).__name__}: {error}")
    finally:
        try:
            _stop_owned(process)
        except Exception as error:
            row.update(state="error", error=f"cleanup failed: {type(error).__name__}: {error}")
        if process is not None and process.stdout is not None:
            process.stdout.close()
        selector.close()
        row["finished"] = time.monotonic()
    return row


def _load_phase(root, files, attempt, indices):
    expected = {f"{i:04d}-{attempt}.json" for i in indices}
    actual = {p.name for p in root.glob(f"*-{attempt}.json")}
    if actual != expected:
        raise RuntimeError(f"incomplete {attempt} statuses: expected {sorted(expected)}, got {sorted(actual)}")
    rows = []
    for index in indices:
        row = json.loads((root / f"{index:04d}-{attempt}.json").read_text())
        if (row["index"], row["file"], row["attempt"], row["log"]) != (
            index, files[index], attempt, f"{index:04d}-{attempt}.log"
        ):
            raise RuntimeError(f"status identity mismatch: {index}/{attempt}")
        if row["state"] != "complete" or type(row["rc"]) is not int or not 0 <= row["rc"] <= 255:
            raise RuntimeError(f"unsuccessful orchestration for {index}/{attempt}: {row}")
        digest = hashlib.sha256()
        size = 0
        with (root / row["log"]).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        if (size, digest.hexdigest()) != (row["log_bytes"], row["log_sha256"]):
            raise RuntimeError(f"log integrity mismatch: {index}/{attempt}")
        rows.append(row)
    return rows


def _tail(root, name):
    try:
        with (root / name).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 16384))
            text = stream.read().decode("utf-8", errors="replace")
        print("\n".join(text.splitlines()[-80:]), flush=True)
    except OSError as error:
        print(f"log unavailable: {_annotation(error)}", flush=True)


def run_fallback(files, log_dir, *, build_command=pytest_command, workers=4, budget=900, stop=None):
    stop = stop if stop is not None else threading.Event()
    deadline = time.monotonic() + budget
    root = None
    parallel, retry, errors = [], [], []
    try:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="fallback-", dir=log_dir))
        files = list(files)
        if not files or not 1 <= workers <= 4 or not math.isfinite(budget) or budget <= 0:
            raise ValueError("nonempty queue, 1..4 workers and finite positive budget required")
        if len({str(Path(file).resolve()) for file in files}) != len(files):
            raise ValueError("duplicate input file")
        _atomic_json(root / "manifest.json", dict(
            schema=1, files=files, workers=workers, parallel_limit=90, retry_limit=180,
            pytest_limit=75, budget=budget,
        ))
        futures = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            try:
                for index, file in enumerate(files):
                    future = pool.submit(_run_one, index, file, "parallel", 90, root,
                                         build_command, stop, deadline)
                    futures[future] = index
            except Exception as error:
                errors.append(f"queue submission failed: {type(error).__name__}: {error}")
                stop.set()
            for future in as_completed(futures):
                index = futures[future]
                try:
                    row = future.result()
                    parallel.append(row)  # retain known test failures even if status persistence fails
                    _atomic_json(root / f"{index:04d}-parallel.json", row)
                    if row["state"] != "complete":
                        errors.append(f"parallel {index}: {row.get('error', 'not complete')}")
                        stop.set()
                    print(f"parallel {index + 1}/{len(files)} rc={row['rc']}: {_annotation(files[index])}", flush=True)
                except Exception as error:
                    errors.append(f"parallel status {index}: {type(error).__name__}: {error}")
                    stop.set()
        if not errors:
            parallel = _load_phase(root, files, "parallel", list(range(len(files))))
            # Barrier: all parallel runners are reaped and all persisted records verified.
            indices = [row["index"] for row in parallel if row["rc"] == 124]
            for index in indices:
                print(f"serial retry after parallel timeout: {_annotation(files[index])}", flush=True)
                row = _run_one(index, files[index], "retry", 180, root, build_command, stop, deadline)
                retry.append(row)
                _atomic_json(root / f"{index:04d}-retry.json", row)
                if row["state"] != "complete":
                    raise RuntimeError(f"retry {index}: {row.get('error', 'not complete')}")
            retry = _load_phase(root, files, "retry", indices)
        if stop.is_set():
            errors.append("fallback interrupted or cancelled")
    except Exception as error:
        stop.set()
        errors.append(f"{type(error).__name__}: {error}")

    final = {row["index"]: row for row in parallel}
    final.update({row["index"]: row for row in retry})
    failed = [row for row in final.values() if row["state"] == "complete" and row["rc"] != 0]
    code = 2 if errors else (1 if failed else 0)
    summary = dict(schema=1, rc=code, parallel=sorted(parallel, key=lambda r: r["index"]),
                   retry=retry, errors=errors)
    if root is not None:
        try:
            _atomic_json(root / "summary.json", summary)
        except Exception as error:
            errors.append(f"summary persistence failed: {type(error).__name__}: {error}")
            code = 2
    # Never emit a transient timeout as ::error:: before retry decisions are final.
    for row in sorted(failed, key=lambda r: r["index"]):
        if errors and row["attempt"] == "parallel" and row["rc"] == 124:
            continue
        print(f"::error::isolated FAIL({row['rc']}): {_annotation(row['file'])}", flush=True)
        for attempt in ("parallel", "retry"):
            name = f"{row['index']:04d}-{attempt}.log"
            if (root / name).exists():
                print(f"--- {name} ---", flush=True)
                _tail(root, name)
    for error in errors:
        print(f"::error::fallback orchestration: {_annotation(error)}", flush=True)
    if not code and retry:
        print(f"::warning::Recovered {len(retry)} parallel timeout(s) by serial retry", flush=True)
    print(f"Fallback rc={code}; diagnostics={root}", flush=True)
    return code, root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--budget", type=float, default=900)
    parser.add_argument("files", nargs="+")
    args = parser.parse_args(argv)
    stop = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        return run_fallback(args.files, args.log_dir, budget=args.budget, stop=stop)[0]
    finally:
        stop.set()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
