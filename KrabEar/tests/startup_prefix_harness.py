"""Изолированное исполнение раннего init; backend/audio/model модули не импортируются.

Реальны AST prefix, runtime getters, SettingsService cache/validator и чтение
StateStore.load_settings. Границы: flock, backup, соседние сервисы и Thread —
инертны; fixture JSON не квалифицирует production save/locking/lifecycle.
"""

from __future__ import annotations

import ast
import copy
import json
import logging
import math
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def source_tree(relative):
    return ast.parse((ROOT / relative).read_text(encoding="utf-8"))


def definition(tree, name):
    return next(node for node in tree.body if getattr(node, "name", None) == name)


def literal(relative, name):
    node = next(
        node for node in source_tree(relative).body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        and node.target.id == name
    )
    return ast.literal_eval(node.value)


def execute(nodes, namespace):
    # Annotations не требуют импортировать реальные audio/provider типы.
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *copy.deepcopy(nodes)], type_ignores=[]))
    exec(compile(module, "<ear-startup-source-contract>", "exec"), namespace)


def early_init_nodes(tree):
    """Непрерывный prefix до следующего init collaborator, без копирования логики."""
    init = definition(definition(tree, "BackendService"), "__init__")
    boundary = next(i for i, node in enumerate(init.body) if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Attribute) and t.attr == "_action_items_extractor"
                            for t in node.targets))
    prefix = init.body[:boundary]
    last = prefix[-1]
    assert isinstance(last, ast.If), "warmup boundary changed: review harness scope"
    calls = [n for n in ast.walk(last) if isinstance(n, ast.Call)]
    assert any(isinstance(n.func, ast.Attribute) and n.func.attr == "start" for n in calls)
    assert any(isinstance(n.func, ast.Attribute) and n.func.attr == "Thread" for n in calls)
    return init, prefix


class DiskSettings:
    """Fixture disk writer; read/merge/privacy semantics берутся из StateStore AST."""

    def __init__(self, directory, load_settings):
        self.data_dir = directory
        directory.mkdir()
        self.settings_path = directory / "settings.json"
        self.load_settings = load_settings.__get__(self, type(self))

    def persist(self, settings):
        self.settings_path.write_text(json.dumps(settings), encoding="utf-8")

    def _lock(self, **kwargs):
        assert kwargs.get("shared") is True
        return nullcontext()

    def read_plaintext_policy_snapshot(self):
        raise AssertionError("unrelated policy reader must stay inert")


def build_harness(directory, settings, *, source=None):
    """Подставляет только явные inert boundaries; реальный Thread не стартует."""
    defaults = literal("core/config.py", "DEFAULT_SETTINGS")
    # Validator целиком, но ровно два core imports заменены exact literal data.
    validator_ns = {
        "__name__": __name__,
        "SUPPORTED_GIGAAM_ASR_MODES": literal("core/gigaam_compat.py", "SUPPORTED_GIGAAM_ASR_MODES"),
        "_STT_BUDGET_KNOB_BOUNDS": literal("core/stt_budget.py", "KNOB_BOUNDS"),
    }
    validator_nodes = []
    for node in source_tree("backend/settings_validator.py").body:
        if isinstance(node, ast.ImportFrom) and node.module in {"core.gigaam_compat", "core.stt_budget", "__future__"}:
            continue
        validator_nodes.append(node)
    execute(validator_nodes, validator_ns)
    namespace = {
        "__name__": __name__, "DEFAULT_SETTINGS": defaults,
        "SettingsValidator": validator_ns["SettingsValidator"],
        "SettingsBackup": lambda: object(), "threading": threading,
        "time": time, "math": math, "json": json,
        "logger": logging.getLogger(__name__), "_log": logging.getLogger(__name__),
        "StateStoreLockTimeout": TimeoutError,
    }
    store_tree = source_tree("backend/state_store.py")
    execute([definition(store_tree, "_DuplicateKeyError"),
             definition(store_tree, "_reject_duplicate_keys"),
             definition(definition(store_tree, "StateStore"), "load_settings")], namespace)
    store = DiskSettings(directory, namespace["load_settings"])
    store.persist(settings)
    settings_class = copy.deepcopy(definition(source_tree("backend/settings_service.py"), "SettingsService"))
    settings_class.body = [definition(settings_class, name) for name in (
        "__init__", "cached_settings", "_read_lock_timeout_budget", "_on_read_lock_timeout", "invalidate_cache")]
    execute([settings_class], namespace)
    tree = source if source is not None else source_tree("backend/service.py")
    init, prefix = early_init_nodes(tree)
    init = copy.deepcopy(init)
    init.body = copy.deepcopy(prefix)
    service_class = copy.deepcopy(definition(tree, "BackendService"))
    service_class.bases = []
    service_class.decorator_list = []
    service_class.body = [init, *[copy.deepcopy(definition(service_class, name)) for name in (
        "_cached_settings", "_get_runtime_setting", "_invalidate_settings_cache")]]
    events = []
    rewriter = SimpleNamespace()
    holder = {}

    def ready(where):
        service = holder["service"]
        getter = getattr(rewriter, "_settings_getter", None)
        assert getattr(getter, "__self__", None) is service, f"{where}: bound getter not ready"
        assert getter.__func__ is type(service)._get_runtime_setting, f"{where}: wrong getter function"
        assert getattr(rewriter, "_spend_dir", None) is store.data_dir, f"{where}: spend dir not ready"
        events.append(where)

    def warmup_sync(**kwargs):
        ready("target")
        assert kwargs["should_continue"]() is True
        holder["warmup"] = kwargs

    rewriter.warmup_sync = warmup_sync

    class ImmediateThread:
        def __init__(self, *, target, kwargs, daemon):
            assert daemon is True
            self.target, self.kwargs = target, kwargs

        def start(self):
            ready("start")
            self.target(**self.kwargs)

    def init_rewriter(service):
        holder["service"] = service
        # Это самое раннее чтение settings при конструировании rewriter.
        holder["early_settings"] = service._cached_settings()
        return rewriter

    def inert(*args, **kwargs):
        return object()

    namespace.update({name: inert for name in (
        "PlaintextExportAuthorizer", "PlaintextExportIPC", "VocabularyStore", "TextSnippetService", "PhoneticVocabService")})
    namespace["threading"] = SimpleNamespace(Thread=ImmediateThread, RLock=threading.RLock)
    execute([definition(tree, "llm_warmup_needed"), service_class], namespace)
    cls = namespace["BackendService"]
    cls._init_llm_rewriter = init_rewriter
    service = cls(store=store, recorder=object(), transcriber=object(), translator=object())
    return SimpleNamespace(service=service, store=store, rewriter=rewriter, events=events,
                           warmup=holder.get("warmup"), early_settings=holder["early_settings"])
