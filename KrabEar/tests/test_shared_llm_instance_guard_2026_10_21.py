"""Общий LLM нельзя выселять через другое имя роли; без HTTP/ML и prod state."""
from __future__ import annotations

import json
import ast
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.memory_conductor import MemoryConductor
from backend.lm_studio_lifecycle import models_share_loaded_instance
from backend.lm_studio_lifecycle import unload_model_async

BASE = "http://localhost:1234/v1"
SHARED = {"models": [{"key": "shared-qwen", "loaded_instances": [
    {"id": "shared-runtime", "config": {"context_length": 8192}},
]}]}
DISTINCT = {"models": [
    {"key": "shared-qwen", "loaded_instances": [{"id": "brain-runtime"}]},
    {"key": "small-rewriter", "loaded_instances": [{"id": "rewrite-runtime"}]},
]}


def response(payload, status=200):
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__.return_value = resp
    return resp


class ConductorSharedInstanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = []
        self.settings = {
            "memory_conductor_enforce": True,
            "llm_base_url": BASE,
            "llm_model": "shared-runtime",
            "llm_brain_model": "shared-qwen",
            "llm_api_key": "test-only-token",
            "rewriter_idle_unload_sec": 1,
        }

    def conductor(self, *, recording=False):
        svc = MagicMock()
        svc.cached_settings.side_effect = lambda: dict(self.settings)
        return MemoryConductor(
            svc, MagicMock(), is_recording=lambda: recording,
            is_meeting_active=lambda: False,
            last_stt_activity_ts_fn=lambda: 0,
            unload_model_fn=lambda *a, **kw: self.events.append(("unload", a)),
            load_model_fn=lambda *a, **kw: self.events.append(("load", a)),
            model_loaded_fn=lambda *a, **kw: False,
            lease_holder_fn=lambda: None,
            shadow_marker_path=Path(self.tmp.name) / "shadow.json",
        )

    def test_shared_alias_rewriter_idle_does_not_unload_brain(self):
        c = self.conductor()
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=response(SHARED)):
            c._rewriter_step(False)
        self.assertEqual(self.events, [])
        self.assertEqual(c._counters["rewriter"]["skipped_gate"], 1)
        self.assertNotIn("rewriter", c._cooldown_until)

    def test_shared_alias_recording_does_not_unload_then_reload_same_instance(self):
        c = self.conductor(recording=True)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=response(SHARED)):
            c.on_recording_start()
            c.wait_workers(2)
        self.assertEqual(self.events, [])

    def test_unknown_catalog_blocks_both_lifecycle_paths(self):
        for payload in ({"data": [{"id": "shared-qwen", "state": "loaded"}]},
                        {"models": []},
                        {"models": [{"key": "shared-qwen", "loaded_instances": []}]}):
            with self.subTest(payload=payload):
                c = self.conductor()
                self.events.clear()
                with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=response(payload)):
                    c._rewriter_step(False)
                    c._recording_sequence_worker()
                self.assertEqual(self.events, [])

    def test_distinct_instances_keep_existing_unload_verify_load(self):
        self.settings["llm_model"] = "small-rewriter"
        c = self.conductor()
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=response(DISTINCT)):
            c._rewriter_step(False)
            self.assertEqual(self.events, [("unload", (BASE, "small-rewriter"))])
            self.assertIn("rewriter", c._cooldown_until)
            self.events.clear()
            c._recording_sequence_worker()
        self.assertEqual(self.events, [("unload", (BASE, "shared-qwen")),
                                       ("load", (BASE, "small-rewriter"))])

    def test_shadow_and_active_recording_do_not_probe_catalog(self):
        self.settings["memory_conductor_enforce"] = False
        c = self.conductor()
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open") as http:
            c._rewriter_step(False)
            c._recording_sequence_worker()
            self.settings["memory_conductor_enforce"] = True
            c._rewriter_step(True)
        http.assert_not_called()
        self.assertEqual(self.events, [])

    def test_injected_identity_error_and_non_bool_result_fail_closed(self):
        for result in (None, 0, "false", RuntimeError("metadata unavailable")):
            with self.subTest(result=result):
                c = self.conductor()
                c.models_share_loaded_instance_fn = MagicMock(
                    side_effect=result if isinstance(result, Exception) else None,
                    return_value=result,
                )
                c._rewriter_step(False)
                c._recording_sequence_worker()
                self.assertEqual(self.events, [])
                self.assertEqual(c._counters["rewriter"]["attempted"], 0)

    def test_recording_started_during_metadata_get_blocks_idle_eviction(self):
        self.settings["llm_model"] = "small-rewriter"
        c = self.conductor()

        def metadata(*args, **kwargs):
            c._is_recording = lambda: True
            return response(DISTINCT)

        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", side_effect=metadata):
            c._rewriter_step(False)
        self.assertEqual(self.events, [])

    def test_foreign_lease_or_unknown_activity_blocks_disjoint_instances(self):
        self.settings["llm_model"] = "small-rewriter"
        for gate in ("foreign_lease", "meeting", "lease_error", "recording_error"):
            with self.subTest(gate=gate):
                c = self.conductor()
                if gate == "foreign_lease":
                    c._lease_holder_fn = lambda: {"owner": "krab"}
                elif gate == "meeting":
                    c._is_meeting_active = lambda: True
                elif gate == "lease_error":
                    c._lease_holder_fn = MagicMock(side_effect=RuntimeError)
                else:
                    c._is_recording = MagicMock(side_effect=RuntimeError)
                with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=response(DISTINCT)):
                    c._rewriter_step(False)
                    if gate != "recording_error":
                        c._recording_sequence_worker()
                self.assertEqual(self.events, [])


class LoadedInstanceMetadataTest(unittest.TestCase):
    def test_key_and_instance_alias_resolve_from_one_authenticated_get(self):
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=response(SHARED)) as http:
            result = models_share_loaded_instance(BASE, "shared-runtime", "shared-qwen", api_key="test-only-token")
        self.assertIs(result, True)
        http.assert_called_once()
        req = http.call_args.args[0]
        self.assertEqual(req.full_url, "http://localhost:1234/api/v1/models")
        self.assertEqual(req.method, "GET")
        self.assertEqual(req.get_header("Authorization"), "Bearer test-only-token")
        self.assertEqual(http.call_args.kwargs["timeout"], 1.5)

    def test_identity_table_including_multiple_instances_and_no_fuzzy_alias(self):
        multi = {"models": [{"key": "qwen", "loaded_instances": [
            {"id": "qwen-a"}, {"id": "qwen-b"},
        ]}]}
        cases = [
            (SHARED, "shared-qwen", "shared-qwen", True),
            (SHARED, "shared-runtime", "shared-runtime", True),
            (DISTINCT, "rewrite-runtime", "brain-runtime", False),
            (DISTINCT, "small-rewriter", "shared-qwen", False),
            (multi, "qwen-a", "qwen-b", False),
            (multi, "qwen", "qwen-b", True),
            (SHARED, "shared-qwen@4bit", "shared-qwen", None),
            (SHARED, "SHARED-QWEN", "shared-qwen", None),
            (SHARED, "unknown", "shared-qwen", None),
        ]
        for payload, first, second, want in cases:
            with self.subTest(first=first, second=second):
                with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=response(payload)):
                    self.assertIs(models_share_loaded_instance(BASE, first, second), want)

    def test_malformed_unloaded_and_ambiguous_catalogs_are_unknown(self):
        cases = [None, [], {}, {"models": None}, {"models": []},
                 {"data": [{"id": "shared-qwen", "state": "loaded"}]},
                 {"models": [None]},
                 {"models": [{"key": "shared-qwen", "loaded_instances": None}]},
                 {"models": [{"key": "shared-qwen", "loaded_instances": []}]},
                 {"models": [{"key": "shared-qwen", "loaded_instances": [{}]}]},
                 {"models": [{"key": "shared-qwen", "loaded_instances": [{"id": 7}]}]},
                 {"models": SHARED["models"] * 2},
                 {"models": SHARED["models"] + [{"key": "other", "loaded_instances": [{"id": "shared-runtime"}]}]},
                 {"models": SHARED["models"] + [{"key": "shared-runtime", "loaded_instances": [{"id": "other"}]}]}]
        for payload in cases:
            with self.subTest(payload=payload):
                with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=response(payload)):
                    self.assertIsNone(models_share_loaded_instance(BASE, "shared-runtime", "shared-qwen"))

    def test_transport_http_json_and_oversized_errors_are_unknown(self):
        responses = [response(SHARED, status=401), response(SHARED, status=503)]
        for raw in (b"{broken", b"x" * (1024 * 1024 + 1)):
            resp = response(SHARED)
            resp.read.return_value = raw
            responses.append(resp)
        for resp in responses:
            with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp) as http:
                self.assertIsNone(models_share_loaded_instance(BASE, "shared-runtime", "shared-qwen"))
                http.assert_called_once()
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", side_effect=TimeoutError):
            self.assertIsNone(models_share_loaded_instance(BASE, "shared-runtime", "shared-qwen"))

    def test_missing_role_or_disallowed_scheme_does_not_request(self):
        for base, first, second in ((BASE, "", "brain"), (BASE, "rewriter", ""),
                                    ("file:///private/tmp/model", "rewriter", "brain")):
            with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open") as http:
                self.assertIsNone(models_share_loaded_instance(base, first, second))
                http.assert_not_called()


class LegacyRecordingUnloadTest(unittest.TestCase):
    def test_legacy_recording_hook_forwards_protected_role_without_conductor(self):
        # Исполняем точный try-block hook из source без импорта audio/ML всего сервиса.
        source = Path(__file__).parents[1] / "backend" / "recording_core_service.py"
        tree = ast.parse(source.read_text())
        blocks = [node for node in ast.walk(tree) if isinstance(node, ast.Try)
                  and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                          and call.func.id == "unload_model_async" for call in ast.walk(node))]
        hook = min(blocks, key=lambda node: node.end_lineno - node.lineno)
        namespace = {"self": types.SimpleNamespace(_memory_conductor=None),
                     "settings": {"llm_model": "shared-runtime", "llm_brain_model": "shared-qwen"},
                     "bump_stt_activity": lambda: None, "logger": MagicMock()}
        threads = []
        real_thread = threading.Thread

        def thread_factory(**kwargs):
            worker = real_thread(**kwargs)
            threads.append(worker)
            return worker

        with patch("backend.lm_studio_lifecycle.unload_model_async", wraps=unload_model_async) as unload, \
             patch("backend.lm_studio_lifecycle.threading.Thread", side_effect=thread_factory), \
             patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=response(SHARED)), \
             patch("backend.lm_studio_lifecycle._try_rest_unload") as rest, \
             patch("backend.lm_studio_lifecycle._try_cli") as cli:
            exec(compile(ast.Module(body=[hook], type_ignores=[]), str(source), "exec"), namespace)
            for worker in threads:
                worker.join(2)
                self.assertFalse(worker.is_alive())
            rest.assert_not_called()
            cli.assert_not_called()
        self.assertEqual(unload.call_count, 1)
        self.assertEqual(unload.call_args.kwargs.get("protected_model_id"), "shared-runtime")

    def test_legacy_worker_blocks_shared_unknown_and_missing_protected_role(self):
        for payload, protected in ((SHARED, "shared-runtime"), ({}, "shared-runtime"), (SHARED, "")):
            with self.subTest(payload=payload, protected=protected):
                threads = []
                real_thread = threading.Thread

                def thread_factory(**kwargs):
                    worker = real_thread(**kwargs)
                    threads.append(worker)
                    return worker

                with patch("backend.lm_studio_lifecycle.threading.Thread", side_effect=thread_factory), \
                     patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=response(payload)), \
                     patch("backend.lm_studio_lifecycle._try_rest_unload") as rest, \
                     patch("backend.lm_studio_lifecycle._try_cli") as cli:
                    unload_model_async(BASE, "shared-qwen", protected_model_id=protected)
                    for worker in threads:
                        worker.join(2)
                        self.assertFalse(worker.is_alive())
                    rest.assert_not_called()
                    cli.assert_not_called()

    def test_distinct_metadata_runs_in_worker_and_preserves_auth(self):
        threads = []
        metadata_threads = []
        real_thread = threading.Thread

        def thread_factory(**kwargs):
            worker = real_thread(**kwargs)
            threads.append(worker)
            return worker

        def metadata(*args, **kwargs):
            metadata_threads.append(threading.get_ident())
            return response(DISTINCT)

        with patch("backend.lm_studio_lifecycle.threading.Thread", side_effect=thread_factory), \
             patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", side_effect=metadata), \
             patch("backend.lm_studio_lifecycle._try_rest_unload", return_value=True) as rest:
            unload_model_async(BASE, "shared-qwen", api_key="test-only-token", protected_model_id="small-rewriter")
            for worker in threads:
                worker.join(2)
                self.assertFalse(worker.is_alive())
            rest.assert_called_once_with(BASE, "shared-qwen", api_key="test-only-token")
        self.assertEqual(len(metadata_threads), 1)
        self.assertNotEqual(metadata_threads[0], threading.get_ident())


if __name__ == "__main__":
    unittest.main()
