"""Чистые helpers обучения на голосе владельца (wake_word_models/owner_data.py).

Синтетические WAV в tmp: личные записи владельца тестами не читаются.
"""

from __future__ import annotations

import ast
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WAKE_MODELS_DIR = os.path.join(PROJECT_ROOT, "wake_word_models")
for _p in (PROJECT_ROOT, WAKE_MODELS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import owner_data as od  # noqa: E402

SR = od.SAMPLE_RATE


def _tone(sec: float, amp: float = 0.3, freq: float = 440.0) -> list[int]:
    n = int(SR * sec)
    return [int(32767 * amp * math.sin(2 * math.pi * freq * i / SR)) for i in range(n)]


def _silence(sec: float) -> list[int]:
    return [0] * int(SR * sec)


def _manifest(root: Path, rows: list[dict]) -> None:
    (root / "manifest.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def _clip(self, name: str, label: str, block: str, **extra) -> dict:
        od.write_wav_mono16(self.root / name, _tone(0.3))
        return {"file": name, "label": label, "block": block, "rate": SR, **extra}

    def test_loads_and_sorts(self) -> None:
        _manifest(self.root, [self._clip("neg_a_000.wav", "neg", "neg_a"),
                              self._clip("pos_a_000.wav", "pos", "pos_a")])
        clips = od.load_owner_manifest(self.root)
        self.assertEqual([c.label for c in clips], ["neg", "pos"])  # по блоку: neg_a < pos_a

    def test_duplicate_file_takes_last_row(self) -> None:
        a = self._clip("pos_a_000.wav", "pos", "pos_a")
        b = dict(a, block="pos_b")
        _manifest(self.root, [a, b])
        self.assertEqual([c.block for c in od.load_owner_manifest(self.root)], ["pos_b"])

    def test_missing_file_is_skipped_not_fatal(self) -> None:
        _manifest(self.root, [self._clip("pos_a_000.wav", "pos", "pos_a"),
                              {"file": "pos_a_001.wav", "label": "pos", "block": "pos_a", "rate": SR}])
        self.assertEqual(len(od.load_owner_manifest(self.root)), 1)

    def test_path_traversal_name_rejected(self) -> None:
        for bad in ("../x.wav", "/etc/x.wav", "a/b.wav", "x.txt", ""):
            _manifest(self.root, [{"file": bad, "label": "pos", "block": "b", "rate": SR}])
            with self.assertRaises(od.OwnerDataError, msg=bad):
                od.load_owner_manifest(self.root)

    def test_symlink_rejected(self) -> None:
        target = self.root / "real.wav"
        od.write_wav_mono16(target, _tone(0.3))
        os.symlink(target, self.root / "link_000.wav")
        _manifest(self.root, [{"file": "link_000.wav", "label": "pos", "block": "b", "rate": SR}])
        with self.assertRaises(od.OwnerDataError):
            od.load_owner_manifest(self.root)

    def test_bad_label_or_rate_rejected(self) -> None:
        _manifest(self.root, [self._clip("pos_a_000.wav", "maybe", "pos_a")])
        with self.assertRaises(od.OwnerDataError):
            od.load_owner_manifest(self.root)
        _manifest(self.root, [dict(self._clip("pos_a_000.wav", "pos", "pos_a"), rate=44100)])
        with self.assertRaises(od.OwnerDataError):
            od.load_owner_manifest(self.root)

    def test_missing_manifest_or_bad_json(self) -> None:
        with self.assertRaises(od.OwnerDataError):
            od.load_owner_manifest(self.root)
        (self.root / "manifest.jsonl").write_text("{не json}\n", encoding="utf-8")
        with self.assertRaises(od.OwnerDataError):
            od.load_owner_manifest(self.root)

    def test_golden_only_language_files(self) -> None:
        for name in ("ru_001.wav", "es_012.wav", "en_003.wav", "compare_report.json",
                     "isolated_compare.py", "pcm_summary.txt", "ru_x.wav", "ru_001.wav.bak"):
            (self.root / name).write_bytes(b"x")
        names = [p.name for p in od.list_golden_clips(self.root)]
        self.assertEqual(names, ["en_003.wav", "es_012.wav", "ru_001.wav"])
        self.assertEqual(od.list_golden_clips(self.root / "нет"), [])


class SplitTests(unittest.TestCase):
    def _clips(self, per_block: dict[str, int]) -> list[od.OwnerClip]:
        return [od.OwnerClip(Path(f"{b}_{i:03d}.wav"), "pos", b) for b, n in per_block.items() for i in range(n)]

    def test_no_clip_in_both_parts_and_all_assigned(self) -> None:
        clips = self._clips({"a": 20, "b": 10, "c": 16})
        train, val = od.split_clips(clips, val_ratio=0.2, seed=1)
        self.assertFalse({c.path for c in train} & {c.path for c in val})
        self.assertEqual(len(train) + len(val), len(clips))

    def test_every_block_with_two_or_more_has_val_and_train(self) -> None:
        clips = self._clips({"a": 2, "b": 5, "c": 1})
        train, val = od.split_clips(clips, val_ratio=0.2, seed=1)
        for block in ("a", "b"):
            self.assertTrue(any(c.block == block for c in val), block)
            self.assertTrue(any(c.block == block for c in train), block)
        self.assertEqual([c.block for c in val if c.block == "c"], [])  # одиночный — только train

    def test_deterministic_by_seed_and_differs_across_seeds(self) -> None:
        clips = self._clips({"a": 30})
        a1 = od.split_clips(clips, seed=5)
        a2 = od.split_clips(clips, seed=5)
        self.assertEqual(a1, a2)
        self.assertNotEqual({c.path for c in od.split_clips(clips, seed=6)[1]}, {c.path for c in a1[1]})

    def test_ratio_validated(self) -> None:
        for bad in (0.0, 1.0, -0.1, 2):
            with self.assertRaises(od.OwnerDataError):
                od.split_clips(self._clips({"a": 4}), val_ratio=bad)


class AudioHelpersTests(unittest.TestCase):
    def test_active_region_finds_speech_with_pad(self) -> None:
        samples = _silence(1.0) + _tone(0.4) + _silence(1.0)
        start, end = od.active_region(samples)
        self.assertLessEqual(start, SR)                    # речь начинается с 1.0 с, запас слева
        self.assertGreater(start, SR - int(0.2 * SR))
        self.assertGreaterEqual(end, SR + int(0.4 * SR))
        self.assertLess(end, SR + int(0.4 * SR) + int(0.2 * SR))

    def test_active_region_none_for_silence_and_quiet(self) -> None:
        self.assertIsNone(od.active_region(_silence(2.0)))
        self.assertIsNone(od.active_region(_tone(2.0, amp=0.005)))  # ниже абсолютного порога
        self.assertIsNone(od.active_region([]))

    def test_trim_positive_removes_silence_and_caps_length(self) -> None:
        clip = od.trim_positive(_silence(1.0) + _tone(0.4) + _silence(1.0))
        self.assertIsNotNone(clip)
        self.assertLess(len(clip), int(0.9 * SR))
        long_clip = od.trim_positive(_tone(3.0), max_len=28000)
        self.assertEqual(len(long_clip), 28000)
        self.assertIsNone(od.trim_positive(_silence(2.0)))

    def test_trim_long_keeps_tail_where_trigger_word_is(self) -> None:
        head = _tone(1.5, amp=0.3, freq=300)
        tail = _tone(0.5, amp=0.3, freq=900)
        out = od.trim_positive(head + tail, max_len=int(1.0 * SR))
        self.assertEqual(len(out), int(1.0 * SR))
        # хвост (слово-триггер) сохранён: последние сэмплы совпадают с концом исходного участка
        region_end = od.active_region(head + tail)[1]
        self.assertEqual(out[-1], (head + tail)[region_end - 1])

    def test_crop_windows(self) -> None:
        self.assertEqual(od.crop_windows([]), [])
        short = _tone(1.0)
        self.assertEqual(od.crop_windows(short), [short])
        wins = od.crop_windows(_tone(2.5), window=32000, hop=8000)
        self.assertTrue(all(len(w) == 32000 for w in wins))
        self.assertGreaterEqual(len(wins), 2)

    def test_wav_roundtrip_and_format_check(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.wav"
            data = _tone(0.2)
            od.write_wav_mono16(p, data)
            self.assertEqual(od.read_wav_mono16(p), data)
            self.assertFalse((Path(td) / "x.wav.tmp").exists())
            import wave
            with wave.open(str(Path(td) / "st.wav"), "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(b"\x00\x00" * 100)
            with self.assertRaises(od.OwnerDataError):
                od.read_wav_mono16(Path(td) / "st.wav")

    def test_prepare_owner_clips_pseudonymizes_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src"
            src.mkdir()
            od.write_wav_mono16(src / "pos_a_000.wav", _silence(0.5) + _tone(0.4) + _silence(1.0))
            od.write_wav_mono16(src / "pos_a_001.wav", _silence(2.0))          # тихий — пропуск
            od.write_wav_mono16(src / "neg_a_000.wav", _tone(2.5))
            clips = [od.OwnerClip(src / "pos_a_000.wav", "pos", "pos_a"),
                     od.OwnerClip(src / "pos_a_001.wav", "pos", "pos_a"),
                     od.OwnerClip(src / "neg_a_000.wav", "neg", "neg_a")]
            out_pos = root / "out_pos"
            c = od.prepare_owner_clips(clips[:2], out_pos, prefix="owner_pos")
            self.assertEqual(c, {"in": 2, "out": 1, "skipped_silent": 1, "segments": 0})
            self.assertEqual([p.name for p in out_pos.iterdir()], ["owner_pos_0000.wav"])
            c = od.prepare_owner_clips(clips[2:], root / "out_neg", prefix="owner_neg")
            self.assertGreaterEqual(c["out"], 2)
            for p in (root / "out_neg").iterdir():
                self.assertNotIn("neg_a", p.name)  # исходные имена не переносятся


class ShortSegmentTests(unittest.TestCase):
    """Негативы той же формы, что позитивы: короткий отрезок речи, ставимый в КОНЕЦ окна."""

    def _speech(self) -> list[int]:
        return _silence(0.3) + _tone(1.2, freq=300) + _silence(0.2) + _tone(1.0, freq=700) + _silence(0.3)

    def test_segments_have_requested_length_range_and_count(self) -> None:
        segs = od.short_segments(self._speech(), count=6, min_len=4800, max_len=12800, seed=1)
        self.assertEqual(len(segs), 6)
        for seg in segs:
            self.assertTrue(4800 <= len(seg) <= 12800, len(seg))

    def test_deterministic_by_seed_and_varies_across_seeds(self) -> None:
        a = od.short_segments(self._speech(), count=4, seed=7)
        self.assertEqual(a, od.short_segments(self._speech(), count=4, seed=7))
        self.assertNotEqual(a, od.short_segments(self._speech(), count=4, seed=8))

    def test_silence_or_too_quiet_gives_nothing(self) -> None:
        self.assertEqual(od.short_segments(_silence(2.0), count=5, seed=1), [])
        self.assertEqual(od.short_segments(_tone(2.0, amp=0.004), count=5, seed=1), [])

    def test_segments_carry_speech_not_silence(self) -> None:
        for seg in od.short_segments(self._speech(), count=8, seed=3):
            self.assertIsNotNone(od.active_region(seg))

    def test_short_speech_is_taken_whole_when_shorter_than_requested(self) -> None:
        segs = od.short_segments(_tone(0.2, freq=500), count=2, min_len=4800, max_len=12800, seed=1)
        self.assertTrue(all(len(s) <= 4800 for s in segs))

    def test_prepare_adds_segments_only_for_requested_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src"
            src.mkdir()
            od.write_wav_mono16(src / "neg_ru_speech_000.wav", self._speech())
            od.write_wav_mono16(src / "neg_room_silence_000.wav", _tone(2.0, amp=0.05))
            clips = [od.OwnerClip(src / "neg_ru_speech_000.wav", "neg", "neg_ru_speech"),
                     od.OwnerClip(src / "neg_room_silence_000.wav", "neg", "neg_room_silence")]
            c = od.prepare_owner_clips(clips, root / "out", prefix="neg_train", short_per_clip=5,
                                       short_block_prefixes=("neg_ru_speech",))
            self.assertEqual(c["segments"], 5)
            windows = [p for p in (root / "out").iterdir()]
            self.assertEqual(len(windows), c["out"])
            c0 = od.prepare_owner_clips(clips, root / "out0", prefix="neg_train")
            self.assertEqual(c0["segments"], 0)
            self.assertEqual(c["out"], c0["out"] + 5)


class SyntheticNoiseTests(unittest.TestCase):
    """Шумовые негативы: белый/коричневый/розовый/гул на разных уровнях (гейт меряет тишину и белый шум)."""

    def _rms(self, samples: list[int]) -> float:
        return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0

    def test_kinds_have_length_and_target_rms(self) -> None:
        for kind in od.NOISE_KINDS:
            noise = od.synth_noise(kind, n_samples=32000, rms=0.01, seed=1)
            self.assertEqual(len(noise), 32000, kind)
            self.assertAlmostEqual(self._rms(noise), 0.01, delta=0.0025, msg=kind)
            self.assertTrue(all(-32768 <= x <= 32767 for x in noise), kind)

    def test_deterministic_and_kinds_differ(self) -> None:
        a = od.synth_noise("white", n_samples=4000, rms=0.01, seed=5)
        self.assertEqual(a, od.synth_noise("white", n_samples=4000, rms=0.01, seed=5))
        self.assertNotEqual(a, od.synth_noise("white", n_samples=4000, rms=0.01, seed=6))
        self.assertNotEqual(a, od.synth_noise("brown", n_samples=4000, rms=0.01, seed=5))

    def test_unknown_kind_and_bad_args_rejected(self) -> None:
        with self.assertRaises(od.OwnerDataError):
            od.synth_noise("blue", n_samples=100, rms=0.01, seed=1)
        with self.assertRaises(od.OwnerDataError):
            od.synth_noise("white", n_samples=0, rms=0.01, seed=1)
        with self.assertRaises(od.OwnerDataError):
            od.synth_noise("white", n_samples=100, rms=0.0, seed=1)

    def test_write_clips_cycles_kinds_and_levels_with_pseudonymous_names(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            n = od.write_synthetic_noise_clips(out, count=24, seed=3)
            self.assertEqual(n, 24)
            names = sorted(p.name for p in out.iterdir())
            self.assertEqual(len(names), 24)
            self.assertTrue(all(nm.startswith("noise_") and nm.endswith(".wav") for nm in names))
            levels = {round(self._rms(od.read_wav_mono16(out / nm)), 3) for nm in names}
            self.assertGreaterEqual(len(levels), 3, "уровни должны различаться")
            self.assertEqual(od.write_synthetic_noise_clips(out / "none", count=0, seed=1), 0)

    def test_repeat_copies_adds_numbered_copies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src"
            src.mkdir()
            od.write_wav_mono16(src / "neg_room_silence_000.wav", _tone(2.0, amp=0.05))
            clips = [od.OwnerClip(src / "neg_room_silence_000.wav", "neg", "neg_room_silence")]
            c = od.prepare_owner_clips(clips, root / "out", prefix="neg_train_q1")
            c2 = od.prepare_owner_clips(clips, root / "out", prefix="neg_train_q2")
            self.assertEqual(len(list((root / "out").iterdir())), c["out"] + c2["out"])


class ModuleHygieneTests(unittest.TestCase):
    def test_no_heavy_imports_and_no_path_logging(self) -> None:
        tree = ast.parse((Path(WAKE_MODELS_DIR) / "owner_data.py").read_text(encoding="utf-8"))
        heavy = {"numpy", "torch", "scipy", "openwakeword", "audiomentations", "torchaudio"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertFalse(names & heavy, f"тяжёлый импорт {names & heavy}")
        # личные имена не должны попасть в логи: в модуле нет logging/print
        src = (Path(WAKE_MODELS_DIR) / "owner_data.py").read_text(encoding="utf-8")
        self.assertNotIn("logging", src)
        self.assertNotIn("print(", src)


if __name__ == "__main__":
    unittest.main()
