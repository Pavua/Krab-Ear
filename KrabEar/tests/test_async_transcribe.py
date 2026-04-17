"""Интеграционные тесты асинхронного транскрибирования (PR #14).

Контракт: /tmp/krab-ear-async/API_CONTRACT.md
Тесты используют фейковые AudioEngine/Transcriber и не дёргают сеть/модели.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.service import BackendService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from backend.translator import TranslationResult  # noqa: E402


class _FakeRecorder:
    """Минимальный фейковый рекордер — для конструктора BackendService."""

    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000


class _FakeTranslator:
    """Фейковый переводчик: mode=off всегда возвращает not_requested."""

    def translate(
        self,
        text: str,
        mode: str,
        network_mode: str,
        translation_style: str = "neutral",
        glossary: dict[str, str] | None = None,
    ) -> TranslationResult:
        return TranslationResult(
            text="",
            status="not_requested",
            source_lang="",
            target_lang="",
            mode="off",
            engine="fake",
        )


class _FakeEngine:
    """Фейковый AudioEngine — impl в _transcribe_paths_core при progress_callback
    идёт напрямую через transcriber.engine.transcribe(...), минуя transcriber.transcribe.
    """

    def __init__(self, stage_sleep: float = 0.05) -> None:
        self._stage_sleep = stage_sleep
        self._counter = 0

    def set_quality_profile(self, profile: str) -> None:
        pass

    def transcribe(self, audio_data: Any, **kwargs: Any) -> dict[str, Any]:
        cb: Callable[[str], None] | None = kwargs.get("progress_callback")
        if cb is not None:
            cb("audio_load")
        time.sleep(self._stage_sleep)
        if cb is not None:
            cb("stt")
        time.sleep(self._stage_sleep)
        self._counter += 1
        return {
            "text": f"fake text #{self._counter}",
            "language": "ru",
            "diarization": None,
        }


class _FakeTranscriber:
    """Фейковый transcriber, эмулирующий стадии STT через progress_callback.

    Делегирует в собственный fake engine — такой же путь, как в бою, когда
    _transcribe_paths_core идёт через transcriber.engine.transcribe(...).
    """

    def __init__(self, stage_sleep: float = 0.05) -> None:
        self.engine = _FakeEngine(stage_sleep=stage_sleep)

    def transcribe(self, audio_data: Any, **kwargs: Any) -> dict[str, Any]:
        return self.engine.transcribe(audio_data, **kwargs)

    def transcribe_preview(self, audio_data: Any, quality_profile: str = "balanced") -> dict[str, Any]:
        return {"text": "", "language": "ru"}


def _make_backend(tmp: str, stage_sleep: float = 0.05) -> BackendService:
    """Строит минимальный BackendService с фейковыми зависимостями."""
    store = StateStore(Path(tmp) / "data")
    return BackendService(
        store=store,
        recorder=_FakeRecorder(),
        transcriber=_FakeTranscriber(stage_sleep=stage_sleep),
        translator=_FakeTranslator(),
    )


def _create_fake_audio_files(tmp: str, count: int) -> list[str]:
    """Создаёт пустые .m4a файлы (достаточно для прохождения allowlist и extension-check).

    Содержимое файла нам не важно — фейковый transcriber возвращает результат
    без чтения файла.
    """
    paths: list[str] = []
    root = Path(tmp)
    root.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        p = root / f"fake_{i}.m4a"
        p.write_bytes(b"\x00")
        paths.append(str(p))
    return paths


class AsyncTranscribeFlowTestCase(unittest.TestCase):
    """Полный async flow: старт → poll progress → результат."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = _make_backend(self.tmp.name)

    def _call(self, method: str, params: dict) -> dict:
        resp = self.service.handle_request({"id": "t", "method": method, "params": params})
        self.assertTrue(resp.get("ok"), f"unexpected error: {resp}")
        return resp["result"]

    def test_async_flow_two_files_done(self) -> None:
        paths = _create_fake_audio_files(self.tmp.name, 2)
        start = self._call(
            "transcribe_paths_async",
            {
                "paths": paths,
                "quality_profile": "balanced",
                "cleanup_profile": "soft",
                "translation_mode": "off",
                "translation_style": "neutral",
                "translate_and_paste": False,
            },
        )
        job_id = start.get("job_id")
        self.assertIsInstance(job_id, str)
        self.assertTrue(job_id.startswith("j-"))

        saw_stt = False
        saw_progress_index = False
        final: dict | None = None

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            progress = self._call("get_transcribe_progress", {"job_id": job_id})
            status = progress.get("status")
            if progress.get("current_stage") == "stt":
                saw_stt = True
            if progress.get("file_index") in (1, 2):
                saw_progress_index = True
            if status in ("done", "failed", "cancelled"):
                final = progress
                break
            time.sleep(0.02)

        self.assertIsNotNone(final, "job не завершился за 10с")
        assert final is not None
        self.assertEqual(final["status"], "done")
        self.assertTrue(saw_stt, "ни один poll не зафиксировал current_stage=='stt'")
        self.assertTrue(saw_progress_index, "ни один poll не зафиксировал file_index in (1,2)")
        self.assertIsInstance(final.get("items"), list)
        self.assertEqual(len(final["items"]), 2)

    def test_get_progress_unknown_job(self) -> None:
        """Polling несуществующего job_id либо возвращает ok=false, либо status=failed/unknown — главное не падать."""
        resp = self.service.handle_request(
            {"id": "t", "method": "get_transcribe_progress", "params": {"job_id": "j-nope"}}
        )
        # Допускаем оба варианта обработки; тест проверяет только что backend не падает.
        self.assertIn("ok", resp)


class AsyncCancelTestCase(unittest.TestCase):
    """Отмена задания на лету."""

    def test_cancel_mid_flight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Каждая стадия спит 0.1s → 2 стадии × 3 файла = ~0.6s общей работы.
            service = _make_backend(tmp, stage_sleep=0.1)
            paths = _create_fake_audio_files(tmp, 3)

            resp = service.handle_request(
                {
                    "id": "t",
                    "method": "transcribe_paths_async",
                    "params": {"paths": paths, "quality_profile": "balanced"},
                }
            )
            self.assertTrue(resp.get("ok"))
            job_id = resp["result"]["job_id"]

            # Отменяем до завершения всех трёх.
            time.sleep(0.05)
            cancel_resp = service.handle_request(
                {"id": "t", "method": "cancel_transcribe_job", "params": {"job_id": job_id}}
            )
            self.assertTrue(cancel_resp.get("ok"))
            self.assertTrue(cancel_resp["result"].get("cancelled"))

            # Ждём финализации.
            final: dict | None = None
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                progress_resp = service.handle_request(
                    {"id": "t", "method": "get_transcribe_progress", "params": {"job_id": job_id}}
                )
                if not progress_resp.get("ok"):
                    break
                status = progress_resp["result"].get("status")
                if status in ("done", "failed", "cancelled"):
                    final = progress_resp["result"]
                    break
                time.sleep(0.02)

            self.assertIsNotNone(final, "job не завершился за 5с")
            assert final is not None
            self.assertEqual(final["status"], "cancelled")
            # Не все 3 файла успели обработаться (контракт: отмена после текущего файла).
            self.assertLess(len(final.get("items", [])), 3)


if __name__ == "__main__":
    unittest.main()
