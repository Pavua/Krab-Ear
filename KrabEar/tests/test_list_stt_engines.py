"""Tests for STTManagementService.handle_list_stt_engines IPC method.

Contract: method list_stt_engines returns all known STT engines (including
disabled-but-installed ones) so the GUI can build a model-picker.

🔴 mlx-masking trap: do NOT assert whisper is_available=True — ubuntu CI runs
Python 3.12 with no mlx wheels, so such assertions are false-green locally.
"""

from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRAB_EAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, KRAB_EAR_ROOT)

from backend.stt_management_service import STTManagementService


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeSettingsService:
    """Minimal SettingsService stub used across all test cases."""

    def __init__(self, initial: dict | None = None):
        self._data: dict = dict(initial or {})

    def cached_settings(self) -> dict:
        return dict(self._data)

    def handle_set_settings(self, params: dict) -> dict:
        self._data.update(params)
        return {"ok": True}


def _make_svc(settings: dict | None = None) -> STTManagementService:
    return STTManagementService(
        settings_svc=_FakeSettingsService(settings or {}),
        transcriber=None,
    )


# ---------------------------------------------------------------------------
# 1. list_stt_engines is in the dispatch table
# ---------------------------------------------------------------------------

class TestListSttEnginesDispatch(unittest.TestCase):
    """Verify list_stt_engines is wired into BackendService._dispatch_table."""

    def test_dispatch_table_contains_list_stt_engines(self):
        """list_stt_engines must be present in the dispatch table."""
        import tempfile
        from pathlib import Path
        from backend.state_store import StateStore
        from backend.service import BackendService

        class _FakeRecorder:
            def start(self, *a, **kw): return {}
            def stop(self, *a, **kw): return {}
            def is_recording(self): return False

        class _FakeEngine:
            def transcribe(self, *a, **kw): return ("", 0.0, [])
            def warmup(self): return {"loaded": True, "latency_ms": 0, "model_name": "", "error": None}

        class _FakeTranscriber:
            engine = _FakeEngine()
            vocabulary: list = []
            def set_vocabulary(self, v): pass
            def transcribe(self, *a, **kw): return ("", 0.0, [])

        class _FakeTranslator:
            def translate(self, *a, **kw): return ("", "")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = StateStore(Path(tmp) / "data")
            svc = BackendService(
                store=store,
                recorder=_FakeRecorder(),
                transcriber=_FakeTranscriber(),
                translator=_FakeTranslator(),
            )
            resp = svc.handle_request(
                {"id": "t1", "method": "list_stt_engines", "params": {}}
            )
            # Must NOT be unknown_method
            if not resp.get("ok"):
                error_code = resp.get("error", {}).get("code", "")
                self.assertNotEqual(
                    error_code,
                    "unknown_method",
                    "list_stt_engines отсутствует в таблице диспетчеризации",
                )
            # Either ok=True or some other handled error (not unknown_method)
            self.assertIn("ok", resp)


# ---------------------------------------------------------------------------
# 2. Response shape
# ---------------------------------------------------------------------------

class TestListSttEnginesShape(unittest.TestCase):
    """Response must have ok, engines (non-empty), and default='whisper_mlx'."""

    def test_response_has_ok_engines_default(self):
        svc = _make_svc()
        res = svc.handle_list_stt_engines({})
        self.assertTrue(res.get("ok"), f"expected ok=True, got {res!r}")
        self.assertIn("engines", res)
        self.assertIsInstance(res["engines"], list)
        self.assertGreater(len(res["engines"]), 0, "engines list must not be empty")
        self.assertEqual(res.get("default"), "whisper_mlx")

    def test_extra_params_ignored(self):
        """Handler must accept (and ignore) unexpected params."""
        svc = _make_svc()
        res = svc.handle_list_stt_engines({"unknown_param": 42, "foo": "bar"})
        self.assertTrue(res.get("ok"))


# ---------------------------------------------------------------------------
# 3. Every engine dict has all 7 required keys
# ---------------------------------------------------------------------------

_REQUIRED_KEYS = {"name", "display_name", "available", "enabled", "toggle_key", "note", "type"}


class TestListSttEnginesEngineKeys(unittest.TestCase):
    """Every engine entry must have all 7 canonical keys."""

    def test_all_keys_present(self):
        svc = _make_svc()
        engines = svc.handle_list_stt_engines({})["engines"]
        for engine in engines:
            missing = _REQUIRED_KEYS - set(engine.keys())
            self.assertEqual(
                missing,
                set(),
                f"Engine '{engine.get('name', '?')}' is missing keys: {missing}",
            )

    def test_type_is_local(self):
        svc = _make_svc()
        engines = svc.handle_list_stt_engines({})["engines"]
        for engine in engines:
            self.assertEqual(
                engine["type"],
                "local",
                f"Engine '{engine['name']}' should have type='local'",
            )


# ---------------------------------------------------------------------------
# 4. whisper_mlx is always present and enabled with toggle_key=null
# ---------------------------------------------------------------------------

class TestWhisperMlxEntry(unittest.TestCase):
    """whisper_mlx must be present with enabled=True and toggle_key=None."""

    def _get_whisper(self) -> dict:
        svc = _make_svc()
        engines = svc.handle_list_stt_engines({})["engines"]
        matches = [e for e in engines if e["name"] == "whisper_mlx"]
        self.assertEqual(len(matches), 1, "whisper_mlx must appear exactly once")
        return matches[0]

    def test_whisper_mlx_present(self):
        engine = self._get_whisper()
        self.assertEqual(engine["name"], "whisper_mlx")

    def test_whisper_mlx_always_enabled(self):
        engine = self._get_whisper()
        self.assertTrue(engine["enabled"], "whisper_mlx must always be enabled=True")

    def test_whisper_mlx_toggle_key_is_null(self):
        engine = self._get_whisper()
        self.assertIsNone(engine["toggle_key"], "whisper_mlx has no toggle key (always on)")

    # 🔴 mlx-masking trap: deliberately NOT asserting available=True here.
    # ubuntu CI has no mlx wheels → is_available() returns False → that is correct.
    def test_whisper_mlx_available_field_is_bool(self):
        """available field must be bool (True or False, not undefined)."""
        engine = self._get_whisper()
        self.assertIsInstance(engine["available"], bool)


# ---------------------------------------------------------------------------
# 5. Disabled engine shows enabled=false with non-null toggle_key
# ---------------------------------------------------------------------------

class TestDisabledEngine(unittest.TestCase):
    """A disabled engine (e.g. gigaam) shows enabled=False + non-null toggle_key."""

    def test_gigaam_disabled_by_default(self):
        """With no settings, gigaam must be enabled=False."""
        svc = _make_svc({})  # stt_gigaam_enabled not set → defaults to False
        engines = svc.handle_list_stt_engines({})["engines"]
        gigaam = next((e for e in engines if e["name"] == "gigaam"), None)
        self.assertIsNotNone(gigaam, "gigaam must be in the engines list")
        self.assertFalse(gigaam["enabled"], "gigaam must be enabled=False when flag not set")
        self.assertIsNotNone(gigaam["toggle_key"], "gigaam must have a non-null toggle_key")
        self.assertEqual(gigaam["toggle_key"], "stt_gigaam_enabled")

    def test_gigaam_enabled_when_flag_set(self):
        """With stt_gigaam_enabled=True in settings, gigaam shows enabled=True."""
        svc = _make_svc({"stt_gigaam_enabled": True})
        engines = svc.handle_list_stt_engines({})["engines"]
        gigaam = next((e for e in engines if e["name"] == "gigaam"), None)
        self.assertIsNotNone(gigaam)
        self.assertTrue(gigaam["enabled"])

    def test_gigaam_display_name_is_v3_not_v2(self):
        """Прод грузит v3_e2e_rnnt; UI-лейбл v2 — миф (живой IPC 2026-08-17)."""
        svc = _make_svc({"stt_gigaam_enabled": True})
        engines = svc.handle_list_stt_engines({})["engines"]
        gigaam = next((e for e in engines if e["name"] == "gigaam"), None)
        self.assertIsNotNone(gigaam)
        self.assertEqual(gigaam["display_name"], "GigaAM v3 (RU)")
        self.assertNotIn("v2", gigaam["display_name"])

    def test_all_optional_engines_have_toggle_keys(self):
        """All non-whisper engines must have a non-null toggle_key."""
        svc = _make_svc()
        engines = svc.handle_list_stt_engines({})["engines"]
        for engine in engines:
            if engine["name"] != "whisper_mlx":
                self.assertIsNotNone(
                    engine["toggle_key"],
                    f"Engine '{engine['name']}' must have a toggle_key",
                )


# ---------------------------------------------------------------------------
# 6. Handler never raises even if adapter imports fail
# ---------------------------------------------------------------------------

class TestListSttEnginesRobustness(unittest.TestCase):
    """Handler must not raise even when adapter's is_available() raises."""

    def test_is_available_exception_yields_available_false(self):
        """If is_available() raises, available must be False (not an exception)."""
        svc = _make_svc()

        # Patch importlib.import_module to raise for one adapter
        import importlib as _il
        _real_import = _il.import_module

        def _flaky_import(name, *args, **kwargs):
            if "stt_whisper_mlx" in name:
                raise ImportError("Simulated mlx import failure")
            return _real_import(name, *args, **kwargs)

        with patch.object(_il, "import_module", side_effect=_flaky_import):
            res = svc.handle_list_stt_engines({})

        # Must not have raised; response must be well-formed
        self.assertTrue(res.get("ok"))
        engines = res["engines"]
        whisper = next((e for e in engines if e["name"] == "whisper_mlx"), None)
        self.assertIsNotNone(whisper)
        # With import failure, available must be False (not an exception)
        self.assertFalse(whisper["available"])

    def test_adapter_init_exception_yields_available_false(self):
        """If adapter __init__ raises, available must be False."""
        svc = _make_svc()

        import importlib as _il
        _real_import = _il.import_module

        def _bad_init_import(name, *args, **kwargs):
            mod = _real_import(name, *args, **kwargs)
            if "stt_sherpa" in name:
                # Wrap the class so __init__ raises
                original_cls = getattr(mod, "SherpaOnnxSTTAdapter", None)
                if original_cls is not None:
                    class _BrokenAdapter(original_cls):
                        def __init__(self, *a, **kw):
                            raise RuntimeError("Simulated init failure")
                    mod = type(mod)("_patched")
                    mod.SherpaOnnxSTTAdapter = _BrokenAdapter
            return mod

        with patch.object(_il, "import_module", side_effect=_bad_init_import):
            res = svc.handle_list_stt_engines({})

        self.assertTrue(res.get("ok"))
        sherpa = next((e for e in res["engines"] if e["name"] == "sherpa"), None)
        self.assertIsNotNone(sherpa)
        self.assertFalse(sherpa["available"])

    def test_never_raises_on_any_import_failure(self):
        """Complete import blackout must still return ok response."""
        svc = _make_svc()

        import importlib as _il

        def _always_fail(name, *args, **kwargs):
            raise ImportError(f"Simulated total import failure for {name}")

        with patch.object(_il, "import_module", side_effect=_always_fail):
            # Must not raise
            res = svc.handle_list_stt_engines({})

        self.assertIn("ok", res)
        self.assertIn("engines", res)
        # All engines should be available=False when imports fail completely
        for engine in res["engines"]:
            self.assertFalse(engine["available"])


# ---------------------------------------------------------------------------
# 7. mlx_available для GigaAM (2026-08-23)
# ---------------------------------------------------------------------------

class MlxAvailableFieldTestCase(unittest.TestCase):
    """mlx_available в list_stt_engines (2026-08-23).

    GigaAM v3 умеет транспорт "mlx" (core/pipeline/stt_gigaam_mlx.py), но UI не
    может сам узнать, установлена ли библиотека gigaam_mlx. handle_list_stt_engines
    отдаёт это одним полем, специфичным ТОЛЬКО для записи gigaam.

    Критично: проверка ОБЯЗАНА идти через importlib.util.find_spec, а не импортом
    core.pipeline.stt_gigaam_mlx — тот импорт успешен и без библиотеки (gigaam_mlx
    импортируется лениво внутри методов адаптера), проверка через импорт была бы
    ложноположительной.
    """

    def test_mlx_available_present_only_on_gigaam(self):
        svc = _make_svc()
        with patch("importlib.util.find_spec", return_value=None):
            result = svc.handle_list_stt_engines({})

        engines = {e["name"]: e for e in result["engines"]}
        self.assertIn("mlx_available", engines["gigaam"])
        for name, engine in engines.items():
            if name != "gigaam":
                self.assertNotIn("mlx_available", engine)

    def test_mlx_available_false_when_spec_missing(self):
        svc = _make_svc()
        with patch("importlib.util.find_spec", return_value=None) as mock_find:
            result = svc.handle_list_stt_engines({})

        engines = {e["name"]: e for e in result["engines"]}
        self.assertFalse(engines["gigaam"]["mlx_available"])
        # find_spec обязан быть вызван именно с "gigaam_mlx"
        mock_find.assert_any_call("gigaam_mlx")

    def test_mlx_available_true_when_spec_present(self):
        svc = _make_svc()
        fake_spec = object()
        with patch("importlib.util.find_spec", return_value=fake_spec):
            result = svc.handle_list_stt_engines({})

        engines = {e["name"]: e for e in result["engines"]}
        self.assertTrue(engines["gigaam"]["mlx_available"])

    def test_mlx_available_false_when_find_spec_raises(self):
        """find_spec может бросить исключение (сломанный .pth/egg-link,
        кастомный sys.meta_path finder) — метод обязан деградировать
        мягко, а не уронить ВЕСЬ список движков."""
        svc = _make_svc()
        with patch("importlib.util.find_spec", side_effect=RuntimeError("boom")):
            result = svc.handle_list_stt_engines({})

        # Метод обязан вернуть ok=True несмотря на исключение
        self.assertTrue(result.get("ok"))
        engines = {e["name"]: e for e in result["engines"]}
        # mlx_available обязана быть False при исключении
        self.assertFalse(engines["gigaam"]["mlx_available"])


class MlxAvailableUsesFindSpecNotImportTestCase(unittest.TestCase):
    """Source-контракт: проверка идёт через importlib.util.find_spec("gigaam_mlx"),
    а НЕ через import core.pipeline.stt_gigaam_mlx (тот импорт успешен без библиотеки).
    Матчим AST, не подстроку — правило CLAUDE.md для source-inspection тестов."""

    def test_ast_calls_find_spec_with_gigaam_mlx_literal(self):
        source = Path(KRAB_EAR_ROOT, "backend", "stt_management_service.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)

        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_find_spec = (
                isinstance(func, ast.Attribute) and func.attr == "find_spec"
            )
            if not is_find_spec:
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and arg.value == "gigaam_mlx":
                    found = True
        self.assertTrue(
            found,
            "handle_list_stt_engines обязан вызывать "
            "importlib.util.find_spec('gigaam_mlx'), а не импортировать адаптер",
        )


if __name__ == "__main__":
    unittest.main()
