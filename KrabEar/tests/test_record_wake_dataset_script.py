"""Контракт скрипта записи датасета: план, качество клипов, продолжение, без доступа к микрофону."""
import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "record_wake_dataset.py"


def _load():
    spec = importlib.util.spec_from_file_location("record_wake_dataset", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass ищет свой модуль в sys.modules
    spec.loader.exec_module(mod)
    return mod


def test_plan_has_enough_positives_and_negatives():
    m = _load()
    plan = m.build_plan()
    pos = sum(len(b.prompts) for b in plan if b.label == "pos")
    neg = sum(len(b.prompts) for b in plan if b.label == "neg")
    assert pos >= 80 and neg >= 80
    assert len({b.key for b in plan}) == len(plan)
    assert all(b.label in ("pos", "neg") and b.prompts for b in plan)


def test_importing_and_plan_do_not_open_microphone():
    # sounddevice/numpy импортируются только внутри record_clip.
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    top_imports = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    top_imports |= {n.module for n in tree.body if isinstance(n, ast.ImportFrom)}
    assert "sounddevice" not in top_imports and "numpy" not in top_imports


def test_assess_clip_flags_clipping_and_silence():
    m = _load()
    assert any("клиппинг" in w for w in m.assess_clip([32767, -32768] * 100)["warnings"])
    assert any("тихо" in w for w in m.assess_clip([0] * 1000)["warnings"])
    ok = m.assess_clip([4000, -4000] * 500)
    assert ok["warnings"] == [] and 0.1 < ok["rms"] < 0.2
    assert m.assess_clip([])["warnings"]


def test_pending_indices_resume(tmp_path):
    m = _load()
    block = m.build_plan()[0]
    assert m.pending_indices(block, tmp_path) == list(range(len(block.prompts)))
    (tmp_path / m.clip_filename(block, 0)).write_bytes(b"x")
    (tmp_path / m.clip_filename(block, 3)).write_bytes(b"x")
    assert m.pending_indices(block, tmp_path) == [i for i in range(len(block.prompts)) if i not in (0, 3)]


def test_wav_and_manifest_roundtrip(tmp_path):
    import json
    import wave
    m = _load()
    m.write_wav(tmp_path / "a.wav", (b"\x01\x00" * 160))
    with wave.open(str(tmp_path / "a.wav")) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
    m.append_manifest(tmp_path, {"file": "a.wav", "prompt": "Краб"})
    assert json.loads((tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[0])["prompt"] == "Краб"


def test_dataset_dir_is_gitignored():
    ignore = (ROOT / "wake_word_models" / ".gitignore").read_text(encoding="utf-8")
    assert "/owner_recordings/" in ignore
