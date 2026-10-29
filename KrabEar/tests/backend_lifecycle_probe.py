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


def run(scratch, scenario):
    boundary = Boundary(scratch)
    boundary.install()
    sys.path.insert(0, str(ROOT))
    result = {"tests": 1, "skipped": 0, "constructor_completed": False,
              "full_module_imported": False, "started_workers": 0,
              "python": sys.version, "executable": sys.executable,
              "python_version": list(sys.version_info[:3]),
              "pycache_prefix": sys.pycache_prefix, "scenario": scenario}
    owner = None
    bus = None
    original_listeners = []
    original_replay = None
    original_export = None
    original_transcriber_close = None
    errors = []
    try:
        assert sys.version_info[:2] == (3, 12), "gate requires Python3.12"
        assert os.environ.get("PYTHONTZPATH") == "", "explicit timezone search-path fixture required"
        from core import config
        import zoneinfo
        result["zoneinfo_tzpath"] = list(zoneinfo.TZPATH)
        assert result["zoneinfo_tzpath"] == []
        assert scenario in {"idle", "export_alive"}
        from backend.state_store import StateStore
        data = scratch / "home/Library/Application Support/KrabEar"
        store = StateStore(data)
        assert config._SETTINGS_JSON_FILE == store.settings_path
        store.save_settings({
            "rewriter_warmup_on_startup": False, "stt_warmup_on_startup": False,
            "binary_drift_check_on_startup": False, "rest_in_process_enabled": False,
            "wake_word_enabled": False, "wake_word_watchdog_enabled": False,
            "auto_purge_enabled": False, "semantic_search_enabled": False,
            "auto_backup_enabled": False, "recap_email_enabled": False,
            "history_encryption_enabled": False,
        })
        from backend import service as module
        result["full_module_imported"] = True
        from backend.recorder import AudioRecorder
        from backend.transcriber import Transcriber
        from core.engine import AudioEngine
        from core.stt_router import STTRouter
        bus = module.event_bus
        original_listeners = list(bus._listeners)
        original_replay = bus._event_replay
        owner = module.BackendService.__new__(module.BackendService)
        module.BackendService.__init__(owner, store)
        result["constructor_completed"] = True
        assert type(owner) is module.BackendService
        assert owner in module._LIVE_INSTANCES
        assert type(owner.recorder) is AudioRecorder
        assert type(owner.transcriber) is Transcriber
        assert type(owner.transcriber.engine) is AudioEngine
        assert type(owner.transcriber.engine._router) is STTRouter
        assert {"get_settings", "get_history_page"} <= owner._dispatch_table.keys()
        assert callable(owner._dispatch_table["get_settings"])
        result["dispatch_built"] = True
        report = owner._startup_diagnostics._cached_report
        assert report is not None
        checks = {check.name: check for check in report.checks}
        assert checks["required_packages"].status == "error"
        assert checks["audio_devices"].status == "error"
        assert checks["lm_studio"].details.get("pending") is True
        assert report.status != "ready"
        result["diagnostics"] = {"status": report.status, "lm_pending": True,
                                 "packages_unavailable": True, "audio_unavailable": True}
        close_calls = []
        held_export = None
        if scenario == "export_alive":
            # Только external thread-handle boundary; actual Thread не subclass.
            class HeldExport:
                alive = True

                def __init__(self):
                    self.joins = []

                def is_alive(self):
                    return self.alive

                def join(self, timeout=None):
                    self.joins.append(timeout)

            held_export = HeldExport()
            original_export = owner._export_scheduler_thread
            owner._export_scheduler_thread = held_export
            original_transcriber_close = owner.transcriber.close

            def observed_close():
                close_calls.append("transcriber.close")
                return original_transcriber_close()

            owner.transcriber.close = observed_close
        result["close_result"] = owner.close()
        result["product_snapshot"] = {
            "retained_listeners": len(bus._listeners) - len(original_listeners),
            "retained_replay": bus._event_replay is owner._event_replay,
            "python_threads": threading.active_count(),
        }
        if held_export is not None:
            result["product_snapshot"].update(
                export_alive=held_export.alive, export_joins=list(held_export.joins),
                transcriber_close_calls=len(close_calls),
                export_owner_retained=owner._export_scheduler_thread is held_export,
                export_stop_set=owner._export_scheduler_stop.is_set(),
            )
            assert result["close_result"] is False, "close accepted exporter still alive after join"
            assert close_calls == [], "transcriber closed before exporter stopped"
            assert owner._export_scheduler_thread is held_export
            assert owner._export_scheduler_stop.is_set()
            assert held_export.joins == [2.0]
            held_export.alive = False
            result["retry_close_result"] = owner.close()
            assert result["retry_close_result"] is True
            assert close_calls == ["transcriber.close"]
        else:
            assert result["close_result"] is True
        assert len(boundary.scheduled) == 5
        assert set(boundary.scheduled) == {cls + "." + method for _, cls, method in SCHEDULERS}
    except BaseException:
        errors.append(traceback.format_exc())
    finally:
        # Эти действия НЕ подменяют product verdict/retention snapshot выше.
        if owner is not None:
            if original_export is not None:
                owner._export_scheduler_thread = original_export
            if original_transcriber_close is not None:
                owner.transcriber.close = original_transcriber_close
            try:
                owner.close()
            except BaseException:
                errors.append("fixture owner.close: " + traceback.format_exc())
            for name, method, kwargs in (
                ("_webhook_manager", "shutdown", {"wait": True}),
                ("_audit_logger", "close", {}), ("_event_replay", "close", {}),
                ("_translation_cache", "close", {}),
            ):
                resource = getattr(owner, name, None)
                if resource is not None:
                    try:
                        getattr(resource, method)(**kwargs)
                    except BaseException:
                        errors.append("fixture " + name + ": " + traceback.format_exc())
        if bus is not None:
            for listener in list(bus._listeners):
                if listener not in original_listeners:
                    bus.remove_listener(listener)
            if owner is not None and bus._event_replay is getattr(owner, "_event_replay", None):
                bus._event_replay = original_replay
    result.update(violations=boundary.violations, denials=boundary.denials, unavailable=boundary.unavailable,
                  scheduled_targets=boundary.scheduled, suppressed=len(boundary.handles),
                  intercepted_joins=boundary.joined, threads_after=threading.active_count(), errors=errors)
    provider_roots = EXPECTED_UNAVAILABLE | FORBIDDEN
    result["loaded_providers"] = sorted(
        name for name in sys.modules if name.partition(".")[0] in provider_roots
    )
    result["ok"] = (not errors and not boundary.violations and not result["loaded_providers"]
                    and result["threads_after"] == 1)
    print("LIFECYCLE_RECEIPT=" + json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(run(Path(sys.argv[1]).resolve(), sys.argv[2]))
