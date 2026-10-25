"""Characterization раннего init; не заменяет integration/lifecycle acceptance."""

import ast
import importlib.util
import sys
from pathlib import Path

import pytest


# По пути: без импорта backend, conftest и тестовых fake audio implementations.
_path = Path(__file__).with_name("startup_prefix_harness.py")
_spec = importlib.util.spec_from_file_location("ear_startup_prefix_harness", _path)
harness = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = harness
_spec.loader.exec_module(harness)

SAVED = {
    "privacy_mode_enabled": False,
    "rewriter_warmup_on_startup": True,
    "llm_rewrite_enabled": True,
    "stt_punctuation_llm_pass_enabled": False,
    "rewriter_warmup_timeout_sec": 17,
    "llm_idle_keepalive_enabled": True,
}


def test_saved_settings_and_dependencies_ready_before_worker(tmp_path):
    probe = harness.build_harness(tmp_path / "data", SAVED)
    assert probe.early_settings["llm_idle_keepalive_enabled"] is True
    assert probe.early_settings["privacy_mode_enabled"] is False
    assert probe.service._get_runtime_setting("rewriter_warmup_timeout_sec", 60) == 17
    assert probe.events == ["start", "target"]
    assert type(probe.warmup["timeout_sec"]) is float
    assert probe.warmup["timeout_sec"] == 17.0


@pytest.mark.parametrize("key,value", [
    ("privacy_mode_enabled", True),
    ("rewriter_warmup_on_startup", False),
    ("llm_rewrite_enabled", False),
])
def test_continuation_rereads_persisted_settings_after_invalidation(tmp_path, key, value):
    probe = harness.build_harness(tmp_path / "data", SAVED)
    continuation = probe.warmup["should_continue"]
    assert continuation() is True
    probe.store.persist({**SAVED, key: value})
    # Часы harness заморожены: wallclock/scheduling не могут состарить кэш.
    assert continuation() is True
    probe.service._invalidate_settings_cache()
    assert continuation() is False
    probe.store.persist(SAVED)
    probe.service._invalidate_settings_cache()
    assert continuation() is True


def test_continuation_rereads_disk_when_real_cache_ttl_expires(tmp_path):
    probe = harness.build_harness(tmp_path / "data", SAVED)
    continuation = probe.warmup["should_continue"]
    ttl = probe.service._settings_svc._cache_ttl
    assert ttl > 0
    probe.store.persist({**SAVED, "privacy_mode_enabled": True})
    probe.clock.advance(ttl / 2)
    assert continuation() is True
    # В настоящем алгоритме на точной границе TTL кэш уже протух.
    probe.clock.advance(ttl / 2)
    assert continuation() is False


@pytest.mark.parametrize("overrides", [
    {"privacy_mode_enabled": True},
    {"rewriter_warmup_on_startup": False},
    {"llm_rewrite_enabled": False, "stt_punctuation_llm_pass_enabled": False},
])
def test_saved_gates_prevent_start_even_with_rewriter(tmp_path, overrides):
    probe = harness.build_harness(tmp_path / "data", {**SAVED, **overrides})
    assert probe.events == []
    assert probe.warmup is None


def test_punctuation_is_also_a_warmup_consumer(tmp_path):
    probe = harness.build_harness(tmp_path / "data", {
        **SAVED, "llm_rewrite_enabled": False, "stt_punctuation_llm_pass_enabled": True,
    })
    assert probe.events == ["start", "target"]


def test_prefix_contains_every_statement_through_warmup_boundary():
    tree = harness.source_tree("backend/service.py")
    init, prefix = harness.early_init_nodes(tree)
    assert init.body[:len(prefix)] == prefix
    assert isinstance(init.body[len(prefix)], ast.Assign)
    assert init.body[len(prefix)].targets[0].attr == "_action_items_extractor"
