"""Штатный Swift-поллер сам перевооружает Python listener после смены ревизии."""

import json
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace

import pytest

from backend.openwakeword_adapter import OpenWakeWordAdapter
from backend.state_store import StateStore


@pytest.mark.timeout(240)
def test_poller_rearms_after_ordinary_settings_commit(tmp_path, monkeypatch):
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("Поллер и IPCClient используют macOS")
    import numpy as np

    root = Path(__file__).resolve().parents[2]
    swift = root / "native/KrabEarAgent/Sources/KrabEarAgent"
    binary = tmp_path / "poller"
    subprocess.run(
        ["swiftc", "-swift-version", "5", "-parse-as-library", swift / "IPCClient.swift",
         swift / "WakeWordPoller.swift", Path(__file__).parent / "fixtures/WakeWordRearmHarness.swift",
         "-o", binary], check=True, timeout=150,
    )
    store = StateStore(tmp_path / "profile")
    store.initialize_startup_plaintext_policy(new_profile=True)
    store.save_settings({**store.load_settings(), "wake_word_enabled": True, "audio_stream_starve_sec": 0})
    writer = StateStore(store.data_dir)
    adapter = OpenWakeWordAdapter(
        store.data_dir, read_privacy_revision=store.read_wake_privacy_revision,
        settings_get=lambda key, default: store.load_settings().get(key, default),
    )
    adapter._oww_available = True
    streams = []
    starts = []
    published = []

    class Stream:
        def __init__(self, blocksize, **kwargs):
            self.frames = blocksize
            self.reads = 0
            streams.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        @property
        def read_available(self):
            if len(streams) == 1:
                writer.save_settings({**writer.load_settings(), "live_subs_language": "es"})
            return 0 if self.reads else self.frames

        def read(self, frames):
            self.reads += 1
            return np.ones((frames, 1), dtype="int16"), False

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=Stream))
    monkeypatch.setattr(adapter, "_load_model", lambda *args: SimpleNamespace(predict=lambda pcm: {"model": 1.0}))

    class Request(socketserver.StreamRequestHandler):
        def handle(self):
            call = json.loads(self.rfile.readline())
            if call["method"] == "wake_word_start":
                result = adapter.handle_wake_word_start(call["params"])
                starts.append(result["ok"])
            else:
                assert call["method"] == "wake_word_status"
                result = adapter.handle_wake_word_status({})
                if result["last_detection"]:
                    published.append(result["last_detection"])
            self.wfile.write(json.dumps({"id": call["id"], "ok": True, "result": result}).encode() + b"\n")

    # sockaddr_un ограничен длиной пути; /tmp + mkdtemp даёт короткий приватный адрес.
    with tempfile.TemporaryDirectory(dir="/tmp") as sock_dir:
        path = str(Path(sock_dir) / "wake.sock")
        with socketserver.UnixStreamServer(path, Request) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                subprocess.run([binary, path], check=True, timeout=35)
            finally:
                server.shutdown()
                thread.join(timeout=2)
                adapter.stop()
    assert starts == [True, True]
    assert [stream.reads for stream in streams] == [0, 1]
    assert len(published) == 1
    assert set(published[0]) == {"model", "score", "ts"}
