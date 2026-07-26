"""TTL-replay терминальных ответов и privacy-purge проводка (R2 Task 5).

Тесты фиксируют протокольный смысл generation token после физического stop:
повтор получает снимок первого терминального ответа без второй остановки или
персиста, кэш ограничен тремя поколениями и пятью минутами, а privacy/purge
не позволяют повторно раскрыть сохранённый транскрипт.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
for import_root in (PROJECT_ROOT, TESTS_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from backend.history_service import HistoryService  # noqa: E402
from backend.recording_core_service import RecordingCoreService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from test_recording_stop_gate import (  # noqa: E402
    _CountingRecorder,
    _make_service,
)


def _generation(token: str) -> dict:
    """Собрать минимальное finalizing-поколение для cache-unit теста."""
    return {
        "token": token,
        "owner": "dictation",
        "state": "finalizing",
        "started_at": 1.0,
        "promoted_from": None,
        "revision": 1,
        "terminal_cache_epoch": 0,
    }


class _Uncopyable:
    """Объект-заглушка, на котором deepcopy детерминированно падает."""

    def __deepcopy__(self, memo):
        raise RuntimeError("снимок запрещён тестом")


class _ExplodingCache(OrderedDict):
    """Кэш-заглушка, имитирующая ошибку публикации после успешного CAS."""

    def __setitem__(self, key, value):
        raise RuntimeError("cache write запрещён тестом")


class _ExplodingReadCache(OrderedDict):
    """Кэш-заглушка, имитирующая повреждение read/prune операции."""

    def items(self):
        raise RuntimeError("cache read запрещён тестом")


class RecordingTerminalCacheTest(unittest.TestCase):
    """Проверить replay, TTL, bounded eviction и изоляцию снимков."""

    def setUp(self) -> None:
        self._tmp_ctx = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmp_ctx.cleanup)
        self._tmp = Path(self._tmp_ctx.name)
        self._service_index = 0

    def _service(
        self,
        *,
        recorder: _CountingRecorder | None = None,
        settings_overrides: dict | None = None,
    ) -> RecordingCoreService:
        self._service_index += 1
        service = _make_service(
            self._tmp / f"service-{self._service_index}",
            recorder=recorder,
            settings_overrides=settings_overrides,
        )
        self.addCleanup(service.close_background_workers)
        return service

    @staticmethod
    def _terminalize(
        service: RecordingCoreService,
        token: str,
        response: dict,
        *,
        stored_at: float,
    ) -> None:
        """Опубликовать response через реальный identity-CAS terminalizer."""
        generation = _generation(token)
        service._finalizing_generations[token] = generation
        with patch(
            "backend.recording_core_service.time.monotonic",
            return_value=stored_at,
        ):
            service._terminalize_generation(generation, response)

    def test_repeat_stop_replays_without_second_stop_or_persist(self) -> None:
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        started = service.handle_start_recording(
            {"source": "dictation"}
        )

        with patch.object(
            service.store,
            "add_history_item",
            wraps=service.store.add_history_item,
        ) as persist:
            first = service.handle_stop_recording(
                {"generation_token": started["generation_token"]}
            )
            repeated = service.handle_stop_recording(
                {"generation_token": started["generation_token"]}
            )

        self.assertEqual(first["status"], "ok")
        self.assertEqual(repeated, first)
        self.assertEqual(recorder.stop_calls, 1)
        persist.assert_called_once()

    def test_old_replay_does_not_touch_new_active_capture(self) -> None:
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        first_start = service.handle_start_recording(
            {"source": "dictation"}
        )
        first_stop = service.handle_stop_recording(
            {"generation_token": first_start["generation_token"]}
        )
        second_start = service.handle_start_recording(
            {"source": "meeting"}
        )
        second_generation = service._active_generation

        replayed = service.handle_stop_recording(
            {"generation_token": first_start["generation_token"]}
        )

        self.assertEqual(replayed, first_stop)
        self.assertEqual(recorder.stop_calls, 1)
        self.assertTrue(recorder.is_recording)
        self.assertIs(service._active_generation, second_generation)
        self.assertEqual(
            second_generation["token"],
            second_start["generation_token"],
        )

    def test_cache_keeps_only_three_newest_terminal_generations(self) -> None:
        service = self._service()
        for index in range(4):
            self._terminalize(
                service,
                f"G{index}",
                {"status": "ok", "history_id": f"H{index}"},
                stored_at=10.0 + index,
            )

        with patch(
            "backend.recording_core_service.time.monotonic",
            return_value=20.0,
        ):
            self.assertIsNone(service._replay_terminal_response("G0"))
            for index in range(1, 4):
                self.assertEqual(
                    service._replay_terminal_response(f"G{index}")[
                        "history_id"
                    ],
                    f"H{index}",
                )

        self.assertEqual(
            list(service._terminal_cache),
            ["G1", "G2", "G3"],
        )

    def test_ttl_uses_monotonic_clock_and_removes_expired_entry(self) -> None:
        service = self._service()
        self._terminalize(
            service,
            "G-TTL",
            {"status": "ok", "text": "временный текст"},
            stored_at=100.0,
        )

        with patch(
            "backend.recording_core_service.time.monotonic",
            return_value=399.999,
        ):
            self.assertIsNotNone(
                service._replay_terminal_response("G-TTL")
            )
        with patch(
            "backend.recording_core_service.time.monotonic",
            return_value=400.0,
        ):
            self.assertIsNone(
                service._replay_terminal_response("G-TTL")
            )

        self.assertNotIn("G-TTL", service._terminal_cache)

    def test_put_prunes_all_expired_entries_not_only_oldest_overflow(
        self,
    ) -> None:
        service = self._service()
        for index in range(3):
            self._terminalize(
                service,
                f"G-OLD-{index}",
                {"status": "ok", "text": f"старый {index}"},
                stored_at=float(index),
            )

        self._terminalize(
            service,
            "G-NEW",
            {"status": "ok", "text": "новый"},
            stored_at=302.0,
        )

        self.assertEqual(
            list(service._terminal_cache),
            ["G-NEW"],
        )

    def test_replay_prunes_other_expired_entries(self) -> None:
        service = self._service()
        self._terminalize(
            service,
            "G-EXPIRED",
            {"status": "ok", "text": "протухший"},
            stored_at=0.0,
        )
        self._terminalize(
            service,
            "G-LIVE",
            {"status": "ok", "text": "живой"},
            stored_at=200.0,
        )

        with patch(
            "backend.recording_core_service.time.monotonic",
            return_value=301.0,
        ):
            replayed = service._replay_terminal_response("G-LIVE")

        self.assertEqual(replayed["text"], "живой")
        self.assertEqual(list(service._terminal_cache), ["G-LIVE"])

    def test_privacy_mode_blocks_replay_without_touching_recorder(self) -> None:
        recorder = _CountingRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"privacy_mode_enabled": False},
        )
        self._terminalize(
            service,
            "G-PII",
            {"status": "ok", "text": "секретный транскрипт"},
            stored_at=100.0,
        )
        service._settings_svc.overrides[
            "privacy_mode_enabled"
        ] = True

        with patch(
            "backend.recording_core_service.time.monotonic",
            return_value=101.0,
        ):
            response = service.handle_stop_recording(
                {"generation_token": "G-PII"}
            )

        self.assertEqual(response["status"], "unknown_generation")
        self.assertNotIn("text", response)
        self.assertEqual(recorder.stop_calls, 0)

    def test_clear_terminal_cache_is_idempotent_and_forgets_pii(self) -> None:
        service = self._service()
        self._terminalize(
            service,
            "G-CLEAR",
            {"status": "ok", "text": "PII для удаления"},
            stored_at=100.0,
        )

        service.clear_terminal_cache()
        service.clear_terminal_cache()

        with patch(
            "backend.recording_core_service.time.monotonic",
            return_value=101.0,
        ):
            self.assertIsNone(
                service._replay_terminal_response("G-CLEAR")
            )
        self.assertEqual(len(service._terminal_cache), 0)

    def test_store_and_replay_use_independent_deep_snapshots(self) -> None:
        service = self._service()
        original = {
            "status": "ok",
            "items": [{"text": "исходный текст"}],
        }
        self._terminalize(
            service,
            "G-COPY",
            original,
            stored_at=100.0,
        )
        original["items"][0]["text"] = "изменено после terminalize"

        with patch(
            "backend.recording_core_service.time.monotonic",
            return_value=101.0,
        ):
            first_replay = service._replay_terminal_response("G-COPY")
            first_replay["items"][0]["text"] = "изменил первый клиент"
            second_replay = service._replay_terminal_response("G-COPY")

        self.assertEqual(
            second_replay["items"][0]["text"],
            "исходный текст",
        )

    def test_stale_terminalizer_cannot_publish_cache_entry(self) -> None:
        service = self._service()
        stale_generation = _generation("G-STALE")

        service._terminalize_generation(
            stale_generation,
            {"status": "ok", "text": "ложный ответ"},
        )

        self.assertIsNone(
            service._replay_terminal_response("G-STALE")
        )

    def test_uncopyable_response_does_not_block_terminalization(
        self,
    ) -> None:
        service = self._service()
        generation = _generation("G-UNCOPYABLE")
        service._finalizing_generations["G-UNCOPYABLE"] = generation

        service._terminalize_generation(
            generation,
            {"status": "ok", "payload": _Uncopyable()},
        )

        self.assertNotIn(
            "G-UNCOPYABLE",
            service._finalizing_generations,
        )
        self.assertIsNone(
            service._replay_terminal_response("G-UNCOPYABLE")
        )

    def test_uncopyable_cached_entry_is_evicted_on_replay(self) -> None:
        service = self._service()
        service._terminal_cache["G-CORRUPT"] = (
            100.0,
            {"status": "ok", "payload": _Uncopyable()},
        )

        with patch(
            "backend.recording_core_service.time.monotonic",
            return_value=101.0,
        ):
            replayed = service._replay_terminal_response("G-CORRUPT")

        self.assertIsNone(replayed)
        self.assertNotIn("G-CORRUPT", service._terminal_cache)

    def test_cache_write_failure_does_not_break_terminal_stop(self) -> None:
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        started = service.handle_start_recording(
            {"source": "dictation"}
        )
        service._terminal_cache = _ExplodingCache()

        response = service.handle_stop_recording(
            {"generation_token": started["generation_token"]}
        )

        self.assertEqual(response["status"], "ok")
        self.assertIsNone(service._active_generation)
        self.assertNotIn(
            started["generation_token"],
            service._finalizing_generations,
        )
        self.assertEqual(recorder.stop_calls, 1)

    def test_cache_read_failure_becomes_safe_unknown_generation(
        self,
    ) -> None:
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        service._terminal_cache = _ExplodingReadCache()

        response = service.handle_stop_recording(
            {"generation_token": "G-CACHE-READ-FAIL"}
        )

        self.assertEqual(response["status"], "unknown_generation")
        self.assertEqual(
            response["generation_token"],
            "G-CACHE-READ-FAIL",
        )
        self.assertEqual(recorder.stop_calls, 0)
        self.assertFalse(recorder.is_recording)

    def test_purge_epoch_blocks_old_tail_but_allows_post_purge_g2(
        self,
    ) -> None:
        """Blocked G1 не репопулирует PII после clear; новая G2 replayable."""
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        first_start = service.handle_start_recording(
            {"source": "dictation"}
        )
        phase_b_entered = threading.Event()
        release_phase_b = threading.Event()
        first_result: dict = {}
        errors: list[BaseException] = []
        original_phase_b = service._stop_recording_phase_b

        def _blocked_phase_b(*args, **kwargs):
            phase_b_entered.set()
            if not release_phase_b.wait(timeout=3.0):
                raise TimeoutError("тест не отпустил phase B")
            return original_phase_b(*args, **kwargs)

        def _stop_first() -> None:
            try:
                first_result.update(service.handle_stop_recording(
                    {
                        "generation_token": (
                            first_start["generation_token"]
                        )
                    }
                ))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(
            service,
            "_stop_recording_phase_b",
            side_effect=_blocked_phase_b,
        ):
            stop_thread = threading.Thread(
                target=_stop_first,
                daemon=True,
            )
            stop_thread.start()
            self.assertTrue(phase_b_entered.wait(timeout=2.0))

            service.clear_terminal_cache()
            second_start = service.handle_start_recording(
                {"source": "meeting"}
            )
            release_phase_b.set()
            stop_thread.join(timeout=4.0)

        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(first_result["status"], "ok")
        stop_calls_before_old_retry = recorder.stop_calls

        old_retry = service.handle_stop_recording(
            {"generation_token": first_start["generation_token"]}
        )

        self.assertEqual(old_retry["status"], "unknown_generation")
        self.assertEqual(recorder.stop_calls, stop_calls_before_old_retry)
        self.assertTrue(recorder.is_recording)

        second_stop = service.handle_stop_recording(
            {"generation_token": second_start["generation_token"]}
        )
        second_replay = service.handle_stop_recording(
            {"generation_token": second_start["generation_token"]}
        )

        self.assertEqual(second_stop["status"], "ok")
        self.assertEqual(second_replay, second_stop)
        self.assertEqual(recorder.stop_calls, stop_calls_before_old_retry + 1)


class RecordingTerminalCachePurgeTest(unittest.TestCase):
    """Проверить constructor/purge/production wiring HistoryService."""

    def setUp(self) -> None:
        self._tmp_ctx = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmp_ctx.cleanup)
        self.store = StateStore(data_dir=Path(self._tmp_ctx.name))

    def test_confirmed_purge_clears_terminal_cache(self) -> None:
        recording_core = MagicMock()
        service = HistoryService(
            store=self.store,
            recording_core=recording_core,
        )

        result = service.handle_purge_all_data({"confirm": True})

        self.assertTrue(result["ok"])
        recording_core.clear_terminal_cache.assert_called_once_with()
        self.assertNotIn("terminal_cache", result["errors"])

    def test_unconfirmed_purge_does_not_clear_terminal_cache(self) -> None:
        recording_core = MagicMock()
        service = HistoryService(
            store=self.store,
            recording_core=recording_core,
        )

        result = service.handle_purge_all_data({})

        self.assertFalse(result["ok"])
        recording_core.clear_terminal_cache.assert_not_called()

    def test_terminal_cache_failure_is_reported_but_purge_continues(
        self,
    ) -> None:
        recording_core = MagicMock()
        recording_core.clear_terminal_cache.side_effect = RuntimeError(
            "cache lock недоступен"
        )
        service = HistoryService(
            store=self.store,
            recording_core=recording_core,
        )

        result = service.handle_purge_all_data({"confirm": True})

        self.assertTrue(result["ok"])
        self.assertFalse(result["complete"])
        self.assertIn("terminal_cache", result["errors"])

    def test_history_service_without_recording_core_remains_compatible(
        self,
    ) -> None:
        service = HistoryService(store=self.store)

        result = service.handle_purge_all_data({"confirm": True})

        self.assertTrue(result["ok"])
        self.assertNotIn("terminal_cache", result["errors"])

    def test_backend_wires_core_after_both_services_exist(self) -> None:
        service_source = (
            PROJECT_ROOT / "backend" / "service.py"
        ).read_text(encoding="utf-8")
        history_pos = service_source.index(
            "self._history = HistoryService("
        )
        core_pos = service_source.index(
            "self._recording_core_svc = RecordingCoreService("
        )
        wire_pos = service_source.index(
            "self._history._recording_core = "
            "self._recording_core_svc"
        )

        self.assertLess(history_pos, core_pos)
        self.assertGreater(wire_pos, core_pos)


if __name__ == "__main__":
    unittest.main()
