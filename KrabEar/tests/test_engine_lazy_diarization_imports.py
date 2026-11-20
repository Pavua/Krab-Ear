"""Путь Whisper без диаризации не требует импорта PyTorch и pyannote."""

import os
import subprocess
import sys
from pathlib import Path


def test_engine_startup_skips_diarization_imports(tmp_path):
    source = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            """
import sys

sys.path.insert(0, sys.argv[1])
diarization_imports = {"torch", "pyannote"}
other_optional_imports = {
    "funasr", "mistral_inference", "mlx", "mlx_whisper", "nemo", "whisperx",
}
attempts = []


class ImportGuard:
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.partition(".")[0]
        if root in diarization_imports:
            attempts.append(fullname)
        if root in diarization_imports | other_optional_imports:
            raise ModuleNotFoundError(fullname, name=fullname)


sys.meta_path.insert(0, ImportGuard())
from core.engine import AudioEngine

engine = AudioEngine(skip_gigaam_warmup=True)
try:
    assert not attempts, attempts
finally:
    engine.close()
""",
            str(source),
        ],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(tmp_path), "KRAB_EAR_DATA_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
