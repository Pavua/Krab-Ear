# -*- coding: utf-8 -*-
"""P0e 2026-08-17: REST /v1/stream native STT под тот же singleflight, что POST.

VG бьёт POST /v1/stt/transcribe и WS /v1/stream в один REST PID. ingest() STT
не делает (F3) — гейт только вокруг transcribe в _process_window.
IPC LiveSubsService gate не получает (default None).

Handoff: docs/superpowers/plans/2026-08-17-p0e-stream-stt-singleflight.md
"""
from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import numpy as np  # noqa: E402

from backend.live_subs_service import LiveSubsService  # noqa: E402
from backend.translator import TranslationResult  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _window(n_samples: int = 16000) -> dict:
    return {
        "seq": 1,
        "audio": np.zeros(n_samples, dtype=np.float32),
        "sample_rate": 16000,
        "target_lang": "off",
        "start_ts": 0.0,
        "end_ts": 1.0,
    }


def _make_service(
    stt_text: str = "hello",
    stt_acquire=None,
    stt_release=None,
    transcribe_side_effect=None,
) -> LiveSubsService:
    transcriber = MagicMock()
    if transcribe_side_effect is not None:
        transcriber.transcribe.side_effect = transcribe_side_effect
    else:
        transcriber.transcribe.return_value = {"text": stt_text, "language": "en"}
    tr_result = TranslationResult(
        text="привет", status="ok", source_lang="en", target_lang="ru",
        mode="ru", engine="stub",
    )
    translator = MagicMock()
    translator.translate.return_value = tr_result
    return LiveSubsService(
        transcriber=transcriber,
        translator=translator,
        stt_acquire=stt_acquire,
        stt_release=stt_release,
    )


def _live_subs_ctor_kwargs(source: str) -> list[set[str]]:
    tree = ast.parse(source)
    found: list[set[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name != "LiveSubsService":
            continue
        found.append({kw.arg for kw in node.keywords if kw.arg})
    return found


class AcquireFalseSkipsTranscribeTest(unittest.TestCase):
    """(a) занято POST → окно дропается, transcribe не зовётся."""

    def test_acquire_false_does_not_call_transcribe(self) -> None:
        acquire = MagicMock(return_value=False)
        release = MagicMock()
        svc = _make_service(stt_acquire=acquire, stt_release=release)
        result = svc._process_window(_window())
        acquire.assert_called_once()
        self.assertEqual(acquire.call_args[0][0], 0)
        svc._transcriber.transcribe.assert_not_called()
        release.assert_not_called()
        self.assertEqual(result["text"], "")
        self.assertIsNone(result["translation"])
        self.assertEqual(svc.dropped_windows, 1)
        svc.close()

    def test_default_none_still_transcribes(self) -> None:
        """IPC-путь: без kwargs гейт молчит, существующие тесты без kwargs зелёные."""
        svc = _make_service()
        result = svc._process_window(_window())
        svc._transcriber.transcribe.assert_called_once()
        self.assertEqual(result["text"], "hello")
        self.assertEqual(svc.dropped_windows, 0)
        svc.close()


class AcquireTrueReleasesInFinallyTest(unittest.TestCase):
    """(b) acquire True → transcribe + release в finally даже при исключении."""

    def test_acquire_true_calls_transcribe_and_release(self) -> None:
        acquire = MagicMock(return_value=True)
        release = MagicMock()
        svc = _make_service(stt_acquire=acquire, stt_release=release)
        result = svc._process_window(_window())
        acquire.assert_called_once()
        self.assertEqual(acquire.call_args[0][0], 0)
        svc._transcriber.transcribe.assert_called_once()
        release.assert_called_once()
        self.assertEqual(result["text"], "hello")
        svc.close()

    def test_release_runs_when_transcribe_raises(self) -> None:
        acquire = MagicMock(return_value=True)
        release = MagicMock()
        svc = _make_service(
            stt_acquire=acquire,
            stt_release=release,
            transcribe_side_effect=RuntimeError("mlx boom"),
        )
        with self.assertRaises(RuntimeError):
            svc._process_window(_window())
        svc._transcriber.transcribe.assert_called_once()
        release.assert_called_once()
        svc.close()


class RestWsConstructorPassesGateTest(unittest.TestCase):
    """(c) REST WS передаёт gate; IPC BackendService — нет."""

    def test_rest_ws_live_subs_ctor_passes_stt_gate(self) -> None:
        source = (_REPO_ROOT / "KrabEar" / "backend" / "rest_server.py").read_text(
            encoding="utf-8",
        )
        ctors = _live_subs_ctor_kwargs(source)
        self.assertEqual(len(ctors), 1, "ожидался ровно один LiveSubsService() в rest_server")
        self.assertIn("stt_acquire", ctors[0])
        self.assertIn("stt_release", ctors[0])

    def test_ipc_backend_live_subs_ctor_has_no_rest_gate(self) -> None:
        source = (_REPO_ROOT / "KrabEar" / "backend" / "service.py").read_text(
            encoding="utf-8",
        )
        ctors = _live_subs_ctor_kwargs(source)
        self.assertGreaterEqual(len(ctors), 1)
        for kwargs in ctors:
            self.assertNotIn("stt_acquire", kwargs)
            self.assertNotIn("stt_release", kwargs)


if __name__ == "__main__":
    unittest.main()
