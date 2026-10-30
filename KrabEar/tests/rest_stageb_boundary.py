"""Полный actual-class idle lifecycle; запускается только disposable parent."""

import sys

# -B запрещает запись, но не чтение существующих shared pyc.
sys.pycache_prefix = sys.argv[1] + "/pycache"

import importlib.util
import json
import os
from pathlib import Path
import threading
import traceback


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_UNAVAILABLE = frozenset({
    "mlx_whisper", "soundfile", "sounddevice", "torch", "pyannote", "funasr",
    "nemo", "whisperx", "mistral_inference", "mistral_common", "sentry_sdk",
})
FORBIDDEN = frozenset({
    "mlx", "tensorflow", "transformers", "huggingface_hub", "argostranslate",
    "pyaudio", "keyring", "httpx", "openwakeword",
})
SCHEDULERS = {
    ("backend.memory_conductor", "MemoryConductor", "_loop"),
    ("backend.service", "BackendService", "_export_scheduler_loop"),
    ("backend.purge_scheduler", "PurgeScheduler", "_run"),
    ("backend.wake_word_watchdog", "WakeWordWatchdog", "_run"),
    ("backend.startup_diagnostics", "StartupDiagnostics", "_run_lm_studio_check_in_background"),
}


class Boundary:
    def __init__(self, scratch):
        self.scratch = scratch
        self.violations = []
        self.denials = []
        self.unavailable = []
        self.scheduled = []
        self.joined = []
        self.handles = {}
        self.read_roots = [ROOT, scratch]
        for entry in sys.path:
            if entry:
                self.read_roots.append(Path(entry).resolve())

    def deny(self, label, *, path=None):
        self.violations.append(label)
        # Без linecache/source reads внутри audit hook и без содержимого файлов.
        frames = []
        frame = sys._getframe(1)
        while frame is not None and len(frames) < 24:
            frames.append({"file": frame.f_code.co_filename.rsplit("/", 1)[-1],
                           "function": frame.f_code.co_name, "line": frame.f_lineno})
            frame = frame.f_back
        del frame
        detail = {"label": label, "frames": frames}
        if path is not None:
            detail["path_name"] = Path(os.fsdecode(path)).name
        self.denials.append(detail)
        raise PermissionError(label)

    def inside(self, path, roots):
        resolved = Path(os.fsdecode(path)).resolve()
        return any(resolved.is_relative_to(root) for root in roots)

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.partition(".")[0]
        if root in EXPECTED_UNAVAILABLE:
            self.unavailable.append(fullname)
            raise ModuleNotFoundError(fullname, name=fullname)
        if root in FORBIDDEN or fullname == "backend.rest_server":
            self.deny("unreviewed provider/module: " + fullname)
        return None

    def audit(self, event, args):
        if event.startswith("socket.") or event in {
            "subprocess.Popen", "os.system", "os.posix_spawn", "os.fork",
            "os.forkpty", "pty.spawn", "os.exec", "os.kill", "os.killpg",
        }:
            self.deny(event)
        if event == "sqlite3.connect" and not self.inside(args[0], [self.scratch]):
            self.deny("non-scratch sqlite")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path, mode, flags = args
            write = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            )
            if write and not self.inside(path, [self.scratch]):
                self.deny("non-scratch write")
            if not write and not self.inside(path, self.read_roots):
                if os.fsdecode(path) not in {"/dev/null", "/dev/urandom", "/etc/localtime"}:
                    self.deny("non-source non-scratch read", path=path)
            if Path(os.fsdecode(path)).name in {".env", ".secrets", "settings.json"}:
                if not self.inside(path, [self.scratch]):
                    self.deny("non-scratch configuration read")
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.truncate"}:
            if isinstance(args[0], (str, bytes, os.PathLike)) and not self.inside(args[0], [self.scratch]):
                self.deny("non-scratch mutation")
        if event in {"os.rename", "os.link", "os.symlink"}:
            if not all(self.inside(p, [self.scratch]) for p in args[:2]):
                self.deny("non-scratch path mutation")
        if event in {"os.listdir", "os.scandir"}:
            if isinstance(args[0], (str, bytes, os.PathLike)) and not self.inside(args[0], self.read_roots):
                # service добавляет PROJECT_ROOT для KrabEar.__version__.
                # Только package discovery: файлы/подкаталоги parent не разрешены.
                if Path(os.fsdecode(args[0])).resolve() != ROOT.parent:
                    self.deny("non-source non-scratch directory read")

    def start(self, thread):
        target = thread._target
        owner = getattr(target, "__self__", None)
        function = getattr(target, "__func__", None)
        for module_name, class_name, method_name in SCHEDULERS:
            module = sys.modules.get(module_name)
            cls = getattr(module, class_name, None)
            if cls is not None and type(owner) is cls and function is getattr(cls, method_name):
                if id(thread) in self.handles:
                    self.deny("duplicate suppressed start")
                label = class_name + "." + method_name
                self.handles[id(thread)] = (thread, label)
                self.scheduled.append(label)
                return
        self.deny("unreviewed thread start")

    def join(self, thread, timeout=None):
        record = self.handles.get(id(thread))
        if record is None or record[0] is not thread:
            self.deny("unreviewed thread join")
        self.joined.append({"target": record[1], "timeout": timeout})

    def install(self):
        sys.meta_path.insert(0, self)
        sys.addaudithook(self.audit)
        # urllib3 проверяет bind(::1) при импорте, если capability true.
        # Только disposable transport profile; socket audit deny сохраняется.
        import socket
        socket.has_ipv6 = False
        original_find_spec = importlib.util.find_spec

        def find_spec(name, package=None):
            if name == "openwakeword":
                self.unavailable.append("find_spec:openwakeword")
                return None
            return original_find_spec(name, package)

        importlib.util.find_spec = find_spec
        boundary = self
        threading.Thread.start = lambda thread: boundary.start(thread)
        threading.Thread.join = lambda thread, timeout=None: boundary.join(thread, timeout)
        import _thread
        _thread.start_new_thread = lambda *a, **k: boundary.deny("direct thread start")
        _thread.start_new = _thread.start_new_thread
        threading._start_new_thread = _thread.start_new_thread
        os._exit = lambda *a: boundary.deny("os._exit")
