"""BackendService._get_runtime_setting: privacy_mode_enabled читается fail-closed.

Волна #2005–#2015 сделала fail-closed собственные гейты сервисов, но ~20
коллабораторов (AutoDeduplicator, RecordingMerger, SharingManager,
TranscriptionQueue, PlaybackTracker, LiveSubs, Meeting, Analytics, …) и ~15
inline-гейтов самого ``service.py`` читают приватность через общий
``_get_runtime_setting(key, default)``. Он глотал сбой чтения настроек
(OSError из ``StateStore._lock()``) и отдавал ``default=False`` — гейт
открывался именно тогда, когда состояние приватности неизвестно. Собственный
fail-closed ``AutoDeduplicator._privacy_mode_enabled`` (#2012) при этом был
декоративным: провайдер исключения наружу не пропускал.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.auto_deduplication import AutoDeduplicator  # noqa: E402
from backend.service import BackendService  # noqa: E402


class _BrokenSettingsSvc:
    def cached_settings(self):
        raise OSError(28, "No space left on device")


class _OkSettingsSvc:
    def __init__(self, payload):
        self._payload = payload

    def cached_settings(self):
        return self._payload


def _service_with(settings_svc) -> BackendService:
    svc = BackendService.__new__(BackendService)
    svc._settings_svc = settings_svc
    return svc


class RuntimeSettingPrivacyFailClosedTest(unittest.TestCase):
    def test_privacy_read_failure_means_privacy_on(self) -> None:
        svc = _service_with(_BrokenSettingsSvc())
        with self.assertLogs("KrabEar.Backend", level="WARNING"):
            self.assertIs(svc._get_runtime_setting("privacy_mode_enabled", False), True)

    def test_other_keys_keep_default_on_failure(self) -> None:
        svc = _service_with(_BrokenSettingsSvc())
        self.assertEqual(svc._get_runtime_setting("llm_rewrite_enabled", "dflt"), "dflt")

    def test_successful_read_is_untouched(self) -> None:
        svc = _service_with(_OkSettingsSvc({"privacy_mode_enabled": False}))
        self.assertIs(svc._get_runtime_setting("privacy_mode_enabled", False), False)
        svc = _service_with(_OkSettingsSvc({"privacy_mode_enabled": True}))
        self.assertIs(svc._get_runtime_setting("privacy_mode_enabled", False), True)

    def test_missing_key_is_not_a_failure(self) -> None:
        svc = _service_with(_OkSettingsSvc({}))
        self.assertIs(svc._get_runtime_setting("privacy_mode_enabled", False), False)

    def test_wired_deduplicator_is_fail_closed(self) -> None:
        """Провод как в проде: AutoDeduplicator(settings_provider=_get_runtime_setting)."""
        svc = _service_with(_BrokenSettingsSvc())
        dedup = AutoDeduplicator(settings_provider=svc._get_runtime_setting)
        self.assertTrue(dedup._privacy_mode_enabled())


if __name__ == "__main__":
    unittest.main()
