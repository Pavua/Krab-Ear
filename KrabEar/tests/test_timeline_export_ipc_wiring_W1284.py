"""Wave 1284: IPC wiring tests for TimelineExporter (W1279 F3 LOW).

Verifies that BackendService correctly wires
  export_timeline_svg / export_timeline_json / export_timeline_ical
to self._timeline_exporter and self._timeline_view.

Strategy: build a minimal _FakeService that mirrors the 3 handler methods
and the helper _resolve_timeline_export_dir; inject mock collaborators.
Also tests dispatch key presence via AST inspection of service.py.
"""
from __future__ import annotations

import ast
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.timeline_export import TimelineExporter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SERVICE_PY = Path(PROJECT_ROOT) / "backend" / "service.py"


def _load_dispatch_keys() -> set[str]:
    """Parse service.py AST and collect all string-literal keys in the handlers dict."""
    source = SERVICE_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    keys: set[str] = set()

    class _Visitor(ast.NodeVisitor):
        def visit_Dict(self, node: ast.Dict) -> None:
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
            self.generic_visit(node)

    _Visitor().visit(tree)
    return keys


def _make_block(
    start_time: str = "2026-04-10T14:00:00+00:00",
    end_time: str = "2026-04-10T15:00:00+00:00",
    items_count: int = 3,
    total_duration_sec: float = 90.0,
    languages: list[str] | None = None,
    summary_text: str = "test recording",
) -> dict:
    return {
        "start_time": start_time,
        "end_time": end_time,
        "items_count": items_count,
        "total_duration_sec": total_duration_sec,
        "total_words": 50,
        "languages": languages or ["ru"],
        "summary_text": summary_text,
    }


def _make_block_obj(block_dict: dict) -> object:
    """Wrap dict as an object with .to_dict() method — mimics TimelineBlock."""
    ns = SimpleNamespace(**block_dict)
    ns.to_dict = lambda: block_dict
    return ns


class _FakeStore:
    """Minimal StateStore stand-in."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def _lock(self):
        return contextlib.nullcontext()

    def _load_active_items_unlocked(self):
        return []


class _FakeService:
    """Mirrors the 3 handler methods + helper from BackendService.

    Collaborators (_timeline_exporter, _timeline_view, _settings_svc,
    store) are injected for full isolation.
    """

    def __init__(
        self,
        store: _FakeStore,
        timeline_exporter: TimelineExporter,
        timeline_view,
        settings: dict | None = None,
    ) -> None:
        self.store = store
        self._timeline_exporter = timeline_exporter
        self._timeline_view = timeline_view
        self._settings = settings or {}

    def _cached_settings(self) -> dict:
        return self._settings

    # Copy the 3 handler methods verbatim from service.py logic:

    def _resolve_timeline_export_dir(self, output_dir):
        if output_dir is None:
            out = Path(self.store.data_dir) / "exports" / "timeline"
            out.mkdir(parents=True, exist_ok=True)
            return out
        resolved = Path(output_dir).expanduser().resolve()
        allowed_roots = [
            Path(self.store.data_dir).resolve(),
            Path.home().resolve(),
            Path("/tmp").resolve(),
            Path(tempfile.gettempdir()).resolve(),
        ]
        if not any(str(resolved).startswith(str(root)) for root in allowed_roots):
            raise ValueError(
                f"output_dir вне разрешённых директорий: {resolved}"
            )
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    def _handle_export_timeline_svg(self, params):
        from datetime import datetime, timezone
        settings = self._cached_settings()
        if settings.get("privacy_mode_enabled"):
            return {"error": {"code": "privacy_mode", "message": "Экспорт отключён в режиме приватности"}}
        output_dir_param = params.get("output_dir")
        try:
            out_dir = self._resolve_timeline_export_dir(output_dir_param)
        except ValueError as exc:
            return {"error": {"code": "invalid_path", "message": str(exc)}}
        group_by = str(params.get("group_by", "day")).strip()
        limit = max(1, min(int(params.get("limit", 500)), 5000))
        width = max(200, int(params.get("width", 1200)))
        height = max(100, int(params.get("height", 400)))
        try:
            with self.store._lock():
                raw_items = self.store._load_active_items_unlocked()[:limit]
        except Exception:
            raw_items = []
        blocks = self._timeline_view.generate_timeline(raw_items, group_by=group_by)
        block_dicts = [b.to_dict() for b in blocks]
        svg_content = self._timeline_exporter.export_svg(block_dicts, width=width, height=height)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"timeline_{ts}.svg"
        file_path = Path(out_dir) / filename
        file_path.write_text(svg_content, encoding="utf-8")
        return {"path": str(file_path), "blocks": len(blocks)}

    def _handle_export_timeline_json(self, params):
        from datetime import datetime, timezone
        settings = self._cached_settings()
        if settings.get("privacy_mode_enabled"):
            return {"error": {"code": "privacy_mode", "message": "Экспорт отключён в режиме приватности"}}
        output_dir_param = params.get("output_dir")
        try:
            out_dir = self._resolve_timeline_export_dir(output_dir_param)
        except ValueError as exc:
            return {"error": {"code": "invalid_path", "message": str(exc)}}
        group_by = str(params.get("group_by", "day")).strip()
        limit = max(1, min(int(params.get("limit", 500)), 5000))
        try:
            with self.store._lock():
                raw_items = self.store._load_active_items_unlocked()[:limit]
        except Exception:
            raw_items = []
        blocks = self._timeline_view.generate_timeline(raw_items, group_by=group_by)
        block_dicts = [b.to_dict() for b in blocks]
        json_content = self._timeline_exporter.export_json(block_dicts)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"timeline_{ts}.json"
        file_path = Path(out_dir) / filename
        file_path.write_text(json_content, encoding="utf-8")
        return {"path": str(file_path), "blocks": len(blocks)}

    def _handle_export_timeline_ical(self, params):
        from datetime import datetime, timezone
        settings = self._cached_settings()
        if settings.get("privacy_mode_enabled"):
            return {"error": {"code": "privacy_mode", "message": "Экспорт отключён в режиме приватности"}}
        output_dir_param = params.get("output_dir")
        try:
            out_dir = self._resolve_timeline_export_dir(output_dir_param)
        except ValueError as exc:
            return {"error": {"code": "invalid_path", "message": str(exc)}}
        group_by = str(params.get("group_by", "day")).strip()
        limit = max(1, min(int(params.get("limit", 500)), 5000))
        try:
            with self.store._lock():
                raw_items = self.store._load_active_items_unlocked()[:limit]
        except Exception:
            raw_items = []
        blocks = self._timeline_view.generate_timeline(raw_items, group_by=group_by)
        block_dicts = [b.to_dict() for b in blocks]
        ical_content = self._timeline_exporter.export_ical(block_dicts)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"timeline_{ts}.ics"
        file_path = Path(out_dir) / filename
        file_path.write_text(ical_content, encoding="utf-8")
        return {"path": str(file_path), "blocks": len(blocks)}


def _make_svc(
    tmp_dir: Path,
    settings: dict | None = None,
    blocks: list | None = None,
) -> _FakeService:
    """Build a _FakeService with real TimelineExporter and mock TimelineView."""
    store = _FakeStore(data_dir=tmp_dir)
    exporter = TimelineExporter()

    block_objs = [_make_block_obj(b) for b in (blocks or [_make_block()])]
    timeline_view = MagicMock()
    timeline_view.generate_timeline.return_value = block_objs

    return _FakeService(
        store=store,
        timeline_exporter=exporter,
        timeline_view=timeline_view,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestExportTimelineSvgIpc(unittest.TestCase):
    """IPC handler: export_timeline_svg returns a file path."""

    def test_export_timeline_svg_ipc_returns_path(self):
        """export_timeline_svg writes an SVG file and returns its path."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_svc(Path(tmp))
            resp = svc._handle_export_timeline_svg({"output_dir": tmp})

            self.assertNotIn("error", resp, f"unexpected error: {resp}")
            self.assertIn("path", resp)
            self.assertIn("blocks", resp)

            path = Path(resp["path"])
            self.assertTrue(path.exists(), f"file not created: {path}")
            self.assertTrue(path.suffix == ".svg")
            content = path.read_text(encoding="utf-8")
            self.assertIn("<svg", content)
            self.assertGreater(resp["blocks"], 0)

    def test_export_timeline_svg_default_dir_created(self):
        """When output_dir is None, file lands in <data_dir>/exports/timeline/."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_svc(Path(tmp))
            resp = svc._handle_export_timeline_svg({})

            self.assertNotIn("error", resp)
            path = Path(resp["path"])
            self.assertTrue(path.exists())
            expected_parent = Path(tmp) / "exports" / "timeline"
            self.assertEqual(path.parent, expected_parent)

    def test_export_timeline_svg_custom_dimensions(self):
        """width/height params flow through to the SVG."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_svc(Path(tmp))
            resp = svc._handle_export_timeline_svg(
                {"output_dir": tmp, "width": 800, "height": 300}
            )
            content = Path(resp["path"]).read_text(encoding="utf-8")
            self.assertIn('width="800"', content)
            self.assertIn('height="300"', content)


class TestExportTimelineJsonIpc(unittest.TestCase):
    """IPC handler: export_timeline_json returns a file path."""

    def test_export_timeline_json_ipc_returns_path(self):
        """export_timeline_json writes a JSON file and returns its path."""
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_svc(Path(tmp))
            resp = svc._handle_export_timeline_json({"output_dir": tmp})

            self.assertNotIn("error", resp)
            self.assertIn("path", resp)
            path = Path(resp["path"])
            self.assertTrue(path.exists())
            self.assertTrue(path.suffix == ".json")

            data = _json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("blocks", data)
            self.assertIn("exported_at", data)
            self.assertIn("total_blocks", data)
            self.assertEqual(resp["blocks"], data["total_blocks"])

    def test_export_timeline_json_default_dir_created(self):
        """When output_dir is None, file lands in <data_dir>/exports/timeline/."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_svc(Path(tmp))
            resp = svc._handle_export_timeline_json({})

            self.assertNotIn("error", resp)
            path = Path(resp["path"])
            self.assertTrue(path.exists())
            expected_parent = Path(tmp) / "exports" / "timeline"
            self.assertEqual(path.parent, expected_parent)


class TestExportTimelineIcalIpc(unittest.TestCase):
    """IPC handler: export_timeline_ical returns a file path."""

    def test_export_timeline_ical_ipc_returns_path(self):
        """export_timeline_ical writes an .ics file and returns its path."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_svc(Path(tmp))
            resp = svc._handle_export_timeline_ical({"output_dir": tmp})

            self.assertNotIn("error", resp)
            self.assertIn("path", resp)
            path = Path(resp["path"])
            self.assertTrue(path.exists())
            self.assertTrue(path.suffix == ".ics")

            content = path.read_text(encoding="utf-8")
            self.assertIn("BEGIN:VCALENDAR", content)
            self.assertIn("END:VCALENDAR", content)

    def test_export_timeline_ical_default_dir_created(self):
        """When output_dir is None, file lands in <data_dir>/exports/timeline/."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_svc(Path(tmp))
            resp = svc._handle_export_timeline_ical({})

            self.assertNotIn("error", resp)
            path = Path(resp["path"])
            self.assertTrue(path.exists())
            expected_parent = Path(tmp) / "exports" / "timeline"
            self.assertEqual(path.parent, expected_parent)


class TestExportTimelineAllowlistRejected(unittest.TestCase):
    """Allowlist gate: output_dir outside allowed roots is rejected."""

    def _assert_rejected(self, handler_name: str, outside_dir: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_svc(Path(tmp))
            handler = getattr(svc, handler_name)
            resp = handler({"output_dir": outside_dir})
            self.assertIn("error", resp, f"{handler_name} should reject {outside_dir!r}")
            self.assertEqual(resp["error"]["code"], "invalid_path")

    def test_export_timeline_outside_allowlist_rejected_svg(self):
        """SVG handler rejects output_dir outside allowed roots."""
        # Use a clearly outside path that doesn't start with home/tmp/data_dir
        self._assert_rejected("_handle_export_timeline_svg", "/var/outside_dir_krab_W1284_test")

    def test_export_timeline_outside_allowlist_rejected_json(self):
        """JSON handler rejects output_dir outside allowed roots."""
        self._assert_rejected("_handle_export_timeline_json", "/var/outside_dir_krab_W1284_test")

    def test_export_timeline_outside_allowlist_rejected_ical(self):
        """iCal handler rejects output_dir outside allowed roots."""
        self._assert_rejected("_handle_export_timeline_ical", "/var/outside_dir_krab_W1284_test")


class TestExportTimelinePrivacyMode(unittest.TestCase):
    """Privacy mode gate: all 3 handlers return error when privacy_mode_enabled=True."""

    def _assert_privacy_blocked(self, handler_name: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_svc(Path(tmp), settings={"privacy_mode_enabled": True})
            handler = getattr(svc, handler_name)
            resp = handler({"output_dir": tmp})
            self.assertIn("error", resp, f"{handler_name} should be blocked in privacy mode")
            self.assertEqual(resp["error"]["code"], "privacy_mode")

    def test_export_timeline_skipped_in_privacy_mode_svg(self):
        """SVG export is blocked when privacy_mode_enabled=True."""
        self._assert_privacy_blocked("_handle_export_timeline_svg")

    def test_export_timeline_skipped_in_privacy_mode_json(self):
        """JSON export is blocked when privacy_mode_enabled=True."""
        self._assert_privacy_blocked("_handle_export_timeline_json")

    def test_export_timeline_skipped_in_privacy_mode_ical(self):
        """iCal export is blocked when privacy_mode_enabled=True."""
        self._assert_privacy_blocked("_handle_export_timeline_ical")

    def test_export_timeline_not_blocked_privacy_mode_false(self):
        """Handlers proceed normally when privacy_mode_enabled=False."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_svc(Path(tmp), settings={"privacy_mode_enabled": False})
            resp = svc._handle_export_timeline_svg({"output_dir": tmp})
            self.assertNotIn("error", resp)


class TestDispatchTableKeys(unittest.TestCase):
    """AST check: dispatch table in service.py contains the 3 new keys."""

    def test_export_timeline_svg_in_dispatch(self):
        keys = _load_dispatch_keys()
        self.assertIn("export_timeline_svg", keys)

    def test_export_timeline_json_in_dispatch(self):
        keys = _load_dispatch_keys()
        self.assertIn("export_timeline_json", keys)

    def test_export_timeline_ical_in_dispatch(self):
        keys = _load_dispatch_keys()
        self.assertIn("export_timeline_ical", keys)


class TestTimelineExporterMethodsExist(unittest.TestCase):
    """Sanity: TimelineExporter has the 3 methods being called."""

    def test_export_svg_method_exists(self):
        self.assertTrue(callable(getattr(TimelineExporter, "export_svg", None)))

    def test_export_json_method_exists(self):
        self.assertTrue(callable(getattr(TimelineExporter, "export_json", None)))

    def test_export_ical_method_exists(self):
        self.assertTrue(callable(getattr(TimelineExporter, "export_ical", None)))


if __name__ == "__main__":
    unittest.main()
