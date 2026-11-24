"""2026-08-05, Fable LOW-D: source-контракт против повторного HIGH-1.

HIGH-1 (round 1): shared AudioRecorder получал тесный дефолт cap ПРЯМО в
конструкторе — это тихо ломало meeting (C2 Live Meeting Overlay), которая
делит recorder с диктовкой. Фикс перенёс потолок на per-session override
(RecordingCoreService._handle_start_recording_locked, зависит от owner).
Юнит-тест с _CapturingRecorder (test_recording_core_service.py) ловит
регрессию только на уровне kwargs в start() — он НЕ ловит, если кто-то
вернёт cap обратно в саму конструкцию AudioRecorder(...) в service.py,
потому что _CapturingRecorder туда не подставляется (BackendService строит
recorder сам, когда параметр не передан). Этот тест читает исходник
BackendService.__init__ напрямую.
"""

from __future__ import annotations

import inspect
import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.service import BackendService


class TestSharedRecorderConstructorHasNoCap(unittest.TestCase):
    def test_audio_recorder_construction_has_no_max_recording_samples_kwarg(self):
        # G3-audio-06 (A06-12): прежний regex ``AudioRecorder\(([^)]*)\)`` обрывался на первой
        # вложенной «)» и не увидел бы max_recording_samples в усложнённом конструкторе.
        import ast
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(BackendService.__init__)))
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and (
                (isinstance(n.func, ast.Name) and n.func.id == "AudioRecorder")
                or (isinstance(n.func, ast.Attribute) and n.func.attr == "AudioRecorder")
            )
        ]
        self.assertTrue(calls, "AudioRecorder(...) construction call not found in __init__")
        kwarg_names = {kw.arg for call in calls for kw in call.keywords}
        self.assertNotIn(
            "max_recording_samples",
            kwarg_names,
            "Shared AudioRecorder must NOT get a constructor-level "
            "max_recording_samples override — it is shared with meeting "
            "(C2 Live Meeting Overlay), which must not be capped "
            "(Fable HIGH-1). The dictation-specific cap belongs in "
            "RecordingCoreService._handle_start_recording_locked as a "
            "per-session recorder.start(max_recording_samples=...) override.",
        )


if __name__ == "__main__":
    unittest.main()
