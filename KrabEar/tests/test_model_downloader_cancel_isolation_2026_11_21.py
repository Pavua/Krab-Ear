"""KE05D-09 / KE05D-10: отмена загрузки изолирована по модели, сбой _is_cached не зависает.

KE05D-09: один ``threading.Event`` на все модели — ``cancel(B)`` прерывал загрузку A,
а ``start_download(C)`` сбрасывал отмену B.
KE05D-10: исключение из ``_is_cached()`` в воркере до внутреннего try оставляло
статус ``downloading`` без конечного события.
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for _p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.model_downloader import ModelDownloader, _DownloadCancelled  # noqa: E402

_A = "mlx-community/model-a"
_B = "mlx-community/model-b"
_C = "mlx-community/model-c"


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def emit(self, event_type: str, payload: dict) -> None:
        with self._lock:
            self.events.append((event_type, payload))

    def statuses(self, model_id: str) -> list[str]:
        with self._lock:
            return [p.get("status") for _t, p in self.events if p.get("model_id") == model_id]


def _downloading(dl: ModelDownloader, model_id: str) -> None:
    dl._get_or_create_state(model_id).update(status="downloading")


def _raises_cancelled(dl: ModelDownloader, model_id: str) -> bool:
    try:
        dl._on_progress(model_id, 10, 100, 10.0)
    except _DownloadCancelled:
        return True
    return False


class CancelIsPerModel(unittest.TestCase):
    def test_cancel_b_does_not_abort_a(self):
        dl = ModelDownloader(event_bus=_Bus())
        _downloading(dl, _A)
        _downloading(dl, _B)
        self.assertTrue(dl.cancel(_B))
        self.assertFalse(_raises_cancelled(dl, _A), "отмена B прервала загрузку A")
        self.assertTrue(_raises_cancelled(dl, _B))

    def test_start_download_c_does_not_clear_cancel_of_b(self):
        dl = ModelDownloader(event_bus=_Bus())
        _downloading(dl, _B)
        self.assertTrue(dl.cancel(_B))
        with patch.object(dl, "_is_cached", return_value=False), patch.object(
            dl, "_download_worker", lambda model_id: None
        ):
            self.assertEqual(dl.start_download(_C), "started")
        self.assertTrue(_raises_cancelled(dl, _B), "старт C сбросил отмену B")

    def test_restart_of_same_model_clears_its_own_cancel(self):
        dl = ModelDownloader(event_bus=_Bus())
        _downloading(dl, _B)
        dl.cancel(_B)
        dl._get_or_create_state(_B).update(status="cancelled")
        with patch.object(dl, "_is_cached", return_value=False), patch.object(
            dl, "_download_worker", lambda model_id: None
        ):
            self.assertEqual(dl.start_download(_B), "started")
        self.assertFalse(_raises_cancelled(dl, _B), "повторный старт B наследует прежнюю отмену")

    def test_cancel_while_queued_never_starts_download(self):
        # B отменили, пока он ждал _dl_lock: после захвата лока snapshot_download вызываться не должен.
        bus = _Bus()
        dl = ModelDownloader(event_bus=bus)
        _downloading(dl, _B)
        dl.cancel(_B)
        with patch.object(dl, "_is_cached", return_value=False), patch(
            "huggingface_hub.snapshot_download", side_effect=AssertionError("download started after cancel")
        ):
            dl._download_worker(_B)
        self.assertEqual(dl._get_or_create_state(_B).status, "cancelled")
        self.assertIn("cancelled", bus.statuses(_B))
        self.assertTrue(dl._dl_lock.acquire(blocking=False), "_dl_lock не освобождён")
        dl._dl_lock.release()


class WorkerSurvivesIsCachedFailure(unittest.TestCase):
    def test_is_cached_error_in_worker_ends_in_error_state(self):
        bus = _Bus()
        dl = ModelDownloader(event_bus=bus)
        _downloading(dl, _A)
        with patch.object(dl, "_is_cached", side_effect=OSError("cache dir unreadable")):
            dl._download_worker(_A)  # не должен бросать
        state = dl._get_or_create_state(_A)
        self.assertEqual(state.status, "error")
        self.assertIn("error", bus.statuses(_A))
        self.assertTrue(dl._dl_lock.acquire(blocking=False), "_dl_lock не освобождён")
        dl._dl_lock.release()


if __name__ == "__main__":
    unittest.main()
