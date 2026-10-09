"""A5.3 B: настоящие timeline handlers и автоматические файловые writers."""

from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

from backend.export_scheduler import ExportScheduler
from backend.plaintext_export_authorization import PlaintextExportAuthorizer
from backend.state_store import StateStore
from _settings_test_helpers import safe_backend_for_settings


class TimelineSinkTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ear-timeline-sinks-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = StateStore(self.root / "profile")
        self.store.initialize_startup_plaintext_policy(new_profile=True)
        self.service = safe_backend_for_settings(self, self.store)

    def call(self, fmt, **params):
        result = self.service.handle_request({
            "id": 1, "method": "export_timeline_" + fmt,
            "params": {"output_dir": str(self.root / fmt), **params},
        })
        self.assertTrue(result["ok"], result)
        return result["result"]

    def enable(self):
        self.store.save_settings({"history_encryption_enabled": True, "privacy_mode_enabled": False})

    def context(self):
        auth = self.service._plaintext_export_authorizer
        session = str(uuid.uuid4())
        grant = auth.issue_grant(session)
        self.assertTrue(grant.ok)
        return {"app_session_id": session, "epoch": grant.epoch.hex(),
                "capability": grant.capability, "expected_policy_generation": grant.policy_generation}

    def test_every_timeline_route_denies_before_resolver_without_consent(self):
        self.enable()
        for fmt in ("svg", "json", "ical"):
            with self.subTest(fmt=fmt), patch.object(self.service, "_resolve_timeline_export_dir", wraps=self.service._resolve_timeline_export_dir) as resolver:
                result = self.call(fmt, force=True, confirm=True)
                self.assertEqual(result.get("reason"), "plaintext_confirmation_required")
                resolver.assert_not_called()
                self.assertFalse((self.root / fmt).exists())

    def test_off_and_bound_grant_allow_real_files(self):
        for fmt in ("svg", "json", "ical"):
            with self.subTest(fmt=fmt):
                result = self.call(fmt)
                self.assertTrue(Path(result["path"]).is_file())
        self.enable()
        context = self.context()
        for fmt in ("svg", "json", "ical"):
            with self.subTest(fmt=fmt):
                result = self.call(fmt, plaintext_export=context)
                self.assertTrue(Path(result["path"]).is_file())

    def test_unknown_missing_authorizer_and_malformed_context_deny(self):
        for raw in (None, {}, "wrong", {"capability": "synthetic-secret"}):
            with self.subTest(raw=type(raw).__name__):
                result = self.call("json", plaintext_export=raw)
                self.assertEqual(result.get("reason"), "plaintext_session_expired")
                self.assertFalse((self.root / "json").exists())
        with patch.object(self.service, "_plaintext_export_authorizer", None):
            result = self.call("svg")
            self.assertEqual(result.get("reason"), "plaintext_policy_unavailable")
        (self.store.data_dir / "settings.json").write_text("broken", encoding="utf-8")
        result = self.call("ical")
        self.assertEqual(result.get("reason"), "plaintext_policy_unavailable")
        self.assertFalse((self.root / "ical").exists())

    def test_service_injects_one_authorizer_into_every_manager(self):
        auth = self.service._plaintext_export_authorizer
        for manager in (self.service._history, self.service._export_scheduler,
                        self.service._sharing, self.service._obsidian_sync):
            self.assertIs(manager._plaintext_export_authorizer, auth)

    def test_actual_dispatch_aliases_use_history_authorizer(self):
        self.enable()
        for method in ("export_html_report", "generate_html_report"):
            with self.subTest(method=method):
                response = self.service.handle_request({
                    "id": 1, "method": method, "params": {"save_to_file": True},
                })
                self.assertTrue(response["ok"], response)
                self.assertEqual(response["result"].get("reason"), "plaintext_confirmation_required")

    def test_fresh_privacy_denies_even_valid_manual_grant(self):
        self.enable()
        context = self.context()
        self.store.save_settings({"privacy_mode_enabled": True})
        for fmt in ("svg", "json", "ical"):
            with self.subTest(fmt=fmt):
                result = self.call(fmt, plaintext_export=context)
                self.assertEqual(result.get("reason"), "privacy_mode_active")
                self.assertFalse((self.root / fmt).exists())

    def test_revoke_during_render_denies_before_mkdir(self):
        self.enable()
        context = self.context()
        auth = self.service._plaintext_export_authorizer
        for fmt in ("svg", "json", "ical"):
            context = self.context()
            original = getattr(self.service._timeline_exporter, "export_" + fmt)
            def render(*args, **kwargs):
                result = original(*args, **kwargs)
                auth.revoke(context["app_session_id"], bytes.fromhex(context["epoch"]), context["capability"])
                return result
            with self.subTest(fmt=fmt), patch.object(self.service._timeline_exporter, "export_" + fmt, side_effect=render):
                result = self.call(fmt, plaintext_export=context)
                self.assertEqual(result.get("reason"), "plaintext_session_expired")
                self.assertFalse((self.root / fmt).exists())


class SchedulerSinkTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ear-scheduler-sinks-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = StateStore(self.root / "profile")
        self.store.initialize_startup_plaintext_policy(new_profile=True)
        self.auth = PlaintextExportAuthorizer(read_snapshot=self.store.read_plaintext_policy_snapshot, profile_identity=str(self.store.data_dir))
        self.addCleanup(self.auth.close)

    def scheduler(self, auth=True):
        return ExportScheduler(
            self.store.data_dir, plaintext_export_authorizer=self.auth if auth else None,
        )

    def test_direct_scheduler_on_denied_despite_active_manual_grant(self):
        self.store.save_settings({"history_encryption_enabled": True, "privacy_mode_enabled": False})
        self.assertTrue(self.auth.issue_grant(str(uuid.uuid4())).ok)
        scheduler = self.scheduler()
        target = self.root / "exports"
        with self.assertRaisesRegex(RuntimeError, "plaintext_confirmation_required"):
            scheduler._do_export(self.store, "json", target)
        self.assertFalse(target.exists())

    def test_missing_authorizer_direct_path_denied(self):
        target = self.root / "exports"
        with self.assertRaisesRegex(RuntimeError, "plaintext_policy_unavailable"):
            self.scheduler(auth=False)._do_export(self.store, "json", target)
        self.assertFalse(target.exists())

    def test_timer_on_denied_before_export_prune_or_schedule_mutation(self):
        scheduler = self.scheduler()
        scheduler.configure("json")
        before = scheduler._schedule_path.read_bytes()
        self.store.save_settings({"history_encryption_enabled": True, "privacy_mode_enabled": False})
        with patch.object(scheduler, "_prune_old_exports", wraps=scheduler._prune_old_exports) as prune:
            result = scheduler.check_and_export(self.store)
            self.assertFalse(result.get("exported", True))
            self.assertEqual(result.get("reason"), "plaintext_confirmation_required")
            prune.assert_not_called()
        self.assertEqual(scheduler._schedule_path.read_bytes(), before)
        self.assertFalse(scheduler.exports_dir.exists())

    def test_off_exports_and_policy_change_after_write_is_explicit_partial(self):
        scheduler = self.scheduler()
        scheduler.configure("json")
        before = scheduler._schedule_path.read_bytes()
        original = scheduler._do_export
        def export_then_enable(*args):
            entry = original(*args)
            self.store.save_settings({"history_encryption_enabled": True, "privacy_mode_enabled": False})
            return entry
        with patch.object(scheduler, "_do_export", side_effect=export_then_enable), patch.object(scheduler, "_prune_old_exports", wraps=scheduler._prune_old_exports) as prune:
            result = scheduler.check_and_export(self.store)
            self.assertTrue(result.get("partial"), result)
            self.assertTrue(Path(result["path"]).is_file())
            prune.assert_not_called()
        self.assertEqual(scheduler._schedule_path.read_bytes(), before)

    def test_policy_change_while_rendering_blocks_first_mutation(self):
        scheduler = self.scheduler()
        target = self.root / "exports"
        def render(*args):
            self.store.save_settings({"history_encryption_enabled": True, "privacy_mode_enabled": False})
            return "synthetic plaintext"
        with patch.object(scheduler, "_generate_content", side_effect=render):
            with self.assertRaisesRegex(RuntimeError, "plaintext_session_expired|plaintext_confirmation_required"):
                scheduler._do_export(self.store, "json", target)
        self.assertFalse(target.exists())

    def test_direct_pruning_requires_current_policy(self):
        scheduler = self.scheduler()
        old_file = self.store.data_dir / "old-export.json"
        old_file.write_text("synthetic plaintext", encoding="utf-8")
        scheduler.max_exports = 0
        schedule = {"exports": [{"path": str(old_file)}]}
        self.store.save_settings({"history_encryption_enabled": True, "privacy_mode_enabled": False})
        with self.assertRaisesRegex(RuntimeError, "plaintext_confirmation_required"):
            scheduler._prune_old_exports(schedule)
        self.assertTrue(old_file.exists())
        self.assertEqual(len(schedule["exports"]), 1)

    def test_off_normal_timer_success(self):
        scheduler = self.scheduler()
        scheduler.configure("json")
        result = scheduler.check_and_export(self.store)
        self.assertTrue(Path(result["path"]).is_file())
        self.assertEqual(scheduler.get_schedule_status()["total_exports"], 1)

    def test_service_loop_never_logs_denial_as_success(self):
        from backend.service import BackendService
        service = object.__new__(BackendService)
        service.store = self.store
        service._export_scheduler_stop = Mock()
        service._export_scheduler_stop.is_set.side_effect = [False, True]
        service._export_scheduler = Mock()
        service._export_scheduler.check_and_export.return_value = {"exported": False, "reason": "plaintext_confirmation_required"}
        with patch("backend.service.logger") as logger:
            service._export_scheduler_loop()
        logger.info.assert_not_called()
