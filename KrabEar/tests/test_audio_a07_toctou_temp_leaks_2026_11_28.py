"""G3-audio-01: A07 (TOCTOU в файловых IPC-обработчиках аудиоаналитики) и
A03/A13 (утечки временных файлов в core/engine.py).

A07: обработчик проверял путь `_validate_audio_read_path`, а анализатору
передавал исходную строку. Между проверкой и чтением путь можно подменить
симлинком (последний компонент или промежуточный каталог) — анализатор
прочитал бы чужой файл. Тесты подменяют путь СРАЗУ ПОСЛЕ настоящей проверки
и смотрят, какие байты реально прочитал вызванный анализатор.

A03/A13: временная копия iCloud-импорта и временный WAV для диаризации
оставались на диске при исключении (shutil.copy2 / subprocess.run), а
`returncode == 0` без готового WAV считался успехом.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import backend.audio_analytics_service as aas  # noqa: E402
from backend.audio_analytics_service import AudioAnalyticsService  # noqa: E402

LEGIT = b"LEGIT-AUDIO-BYTES"
SECRET = b"SECRET-OUTSIDE-BYTES"


def _make_service(audio_converter=None) -> AudioAnalyticsService:
    return AudioAnalyticsService(
        audio_converter=audio_converter or MagicMock(),
        quality_trends=MagicMock(),
        audio_fingerprinter=MagicMock(),
        word_timing_analyzer=MagicMock(),
        store=MagicMock(data_dir=None),
    )


class _HandlerHarness:
    """Вызывает один из пяти файловых обработчиков с анализатором-шпионом.

    Шпион читает байты по ПОЛУЧЕННОМУ пути — то есть ровно то, что прочитал
    бы настоящий анализатор (soundfile/libsndfile открывает путь сам).
    """

    HANDLERS = ("analyze_audio_quality", "analyze_silence", "get_audio_info",
                "get_waveform", "profile_noise")

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    def _spy(self, path) -> None:
        p = str(path)
        self.calls.append((p, Path(p).read_bytes()))

    def run(self, name: str, file_path: str, analyzer_error: Exception | None = None):
        def spy_and_maybe_fail(path, *a, **k):
            self._spy(path)
            if analyzer_error is not None:
                raise analyzer_error

        if name == "analyze_audio_quality":
            report = MagicMock()
            report.to_dict.return_value = {"ok": True}

            def af(path, *a, **k):
                spy_and_maybe_fail(path)
                return report

            with patch("core.audio_quality.analyze_file", side_effect=af):
                return _make_service().handle_analyze_audio_quality({"file_path": file_path})
        if name == "analyze_silence":
            def asf(path, *a, **k):
                spy_and_maybe_fail(path)
                return {"ok": True}

            with patch("core.silence_detector.analyze_silence_file", side_effect=asf):
                return _make_service().handle_analyze_silence({"file_path": file_path})
        if name == "get_audio_info":
            conv = MagicMock()
            info = MagicMock(duration=1.0, sample_rate=16000, channels=1, format="wav", size_mb=0.0)

            def gai(path, *a, **k):
                spy_and_maybe_fail(path)
                return info

            conv.get_audio_info.side_effect = gai
            return _make_service(audio_converter=conv).handle_get_audio_info({"path": file_path})
        if name == "get_waveform":
            gen = MagicMock()
            wf = MagicMock(points=[], duration_sec=0.0, sample_rate=0,
                           peak_amplitude=0.0, rms_amplitude=0.0)

            def gff(path, *a, **k):
                spy_and_maybe_fail(path)
                return wf

            gen.generate_from_file.side_effect = gff
            with patch("core.waveform_generator.WaveformGenerator", return_value=gen):
                return _make_service().handle_get_waveform({"file_path": file_path})
        if name == "profile_noise":
            profiler = MagicMock()
            profiler.profile.return_value.to_dict.return_value = {"ok": True}

            def sfr(path, *a, **k):
                spy_and_maybe_fail(path)
                return np.zeros(16, dtype=np.float32), 16000

            with patch("core.noise_profiler.NoiseProfiler", return_value=profiler), \
                 patch("soundfile.read", side_effect=sfr):
                return _make_service().handle_profile_noise({"file_path": file_path})
        raise AssertionError(name)


class _TmpTree(unittest.TestCase):
    def setUp(self) -> None:
        # Внутри разрешённого корня (tempdir), чтобы исходная проверка проходила.
        self.root = Path(tempfile.mkdtemp(prefix="ke_a07_")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "secret.wav"
        self.secret.write_bytes(SECRET)


class TestA07SymlinkSwapAfterValidation(_TmpTree):
    def _swap_after_validation(self, swap):
        real = aas._validate_audio_read_path

        def validate_then_swap(p, data_dir=None):
            resolved = real(p, data_dir)
            swap()
            return resolved

        return patch.object(aas, "_validate_audio_read_path", side_effect=validate_then_swap)

    def _assert_no_secret_read(self, h: _HandlerHarness, name: str, run) -> None:
        try:
            run()
        except (ValueError, OSError):
            pass  # отказ — допустимый исход; главное, что чужие байты не прочитаны
        for _path, data in h.calls:
            self.assertNotEqual(data, SECRET, f"{name}: анализатор прочитал подменённый файл")

    def test_final_component_symlink_swap_never_reaches_analyzer(self) -> None:
        for name in _HandlerHarness.HANDLERS:
            with self.subTest(handler=name):
                target = self.root / f"{name}.wav"
                target.write_bytes(LEGIT)

                def swap(target=target):
                    link = target.with_name(target.name + ".lnk")
                    os.symlink(self.secret, link)
                    os.replace(link, target)

                h = _HandlerHarness()
                with self._swap_after_validation(swap):
                    self._assert_no_secret_read(h, name, lambda: h.run(name, str(target)))

    def test_intermediate_directory_symlink_swap_is_refused(self) -> None:
        for name in _HandlerHarness.HANDLERS:
            with self.subTest(handler=name):
                sub = self.root / f"sub_{name}"
                sub.mkdir()
                (sub / "a.wav").write_bytes(LEGIT)
                decoy = self.root / f"decoy_{name}"
                decoy.mkdir()
                (decoy / "a.wav").write_bytes(SECRET)

                def swap(sub=sub, decoy=decoy):
                    sub.rename(sub.with_name(sub.name + ".moved"))
                    os.symlink(decoy, sub)

                h = _HandlerHarness()
                with self._swap_after_validation(swap):
                    self._assert_no_secret_read(h, name, lambda: h.run(name, str(sub / "a.wav")))


class TestA07PinnedSnapshotContract(_TmpTree):
    def test_analyzer_reads_validated_bytes_and_snapshot_is_removed_even_on_error(self) -> None:
        for name in _HandlerHarness.HANDLERS:
            with self.subTest(handler=name):
                target = self.root / f"ok_{name}.FLAC"
                target.write_bytes(LEGIT)
                h = _HandlerHarness()
                with self.assertRaises(RuntimeError):
                    h.run(name, str(target), analyzer_error=RuntimeError("analyzer boom"))
                self.assertEqual(len(h.calls), 1)
                got_path, got_bytes = h.calls[0]
                self.assertEqual(got_bytes, LEGIT)
                self.assertNotEqual(got_path, str(target), "анализатор должен читать закреплённый снимок")
                # Регистр расширения сохраняется в нижнем: get_audio_info берёт формат из суффикса.
                self.assertEqual(Path(got_path).suffix, ".flac")
                self.assertFalse(Path(got_path).exists(), "снимок не удалён после ошибки анализатора")
                self.assertEqual(target.read_bytes(), LEGIT, "исходный файл не должен меняться")

    def test_missing_file_still_raises_file_not_found(self) -> None:
        for name in _HandlerHarness.HANDLERS:
            with self.subTest(handler=name):
                h = _HandlerHarness()
                with self.assertRaises(FileNotFoundError):
                    h.run(name, str(self.root / "nope.wav"))
                self.assertEqual(h.calls, [])

    def test_fifo_is_refused_without_blocking(self) -> None:
        fifo = self.root / "pipe.wav"
        os.mkfifo(fifo)
        outcome: dict[str, object] = {}

        def call() -> None:
            try:
                _HandlerHarness().run("analyze_audio_quality", str(fifo))
                outcome["result"] = "no-error"
            except Exception as exc:  # noqa: BLE001 — фиксируем тип исхода
                outcome["result"] = exc

        t = threading.Thread(target=call, daemon=True)
        t.start()
        t.join(5.0)
        if t.is_alive():
            # Разблокировать зависший open(), чтобы не оставить поток навсегда.
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
            self.fail("обработчик заблокировался на FIFO")
        self.assertIsInstance(outcome.get("result"), ValueError)

    def test_snapshot_size_cap_refuses_before_analyzer(self) -> None:
        target = self.root / "big.wav"
        target.write_bytes(b"x" * 11)
        h = _HandlerHarness()
        with patch.object(aas, "_MAX_SNAPSHOT_BYTES", 10):
            with self.assertRaises(ValueError):
                h.run("analyze_audio_quality", str(target))
        self.assertEqual(h.calls, [])


# ---------------------------------------------------------------------------
# A03/A13 — core/engine.py
# ---------------------------------------------------------------------------

class _IsolatedTempdir(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="ke_a13_")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        p = patch.object(tempfile, "tempdir", self.tmpdir)
        p.start()
        self.addCleanup(p.stop)

    def leftovers(self, prefix: str) -> list[str]:
        return sorted(n for n in os.listdir(self.tmpdir) if n.startswith(prefix))


class TestA13IcloudCopyCleanup(_IsolatedTempdir):
    def test_failed_copy_removes_partial_temp_copy(self) -> None:
        from core import engine

        src_dir = tempfile.mkdtemp(prefix="ke_src_", dir=os.path.dirname(self.tmpdir))
        self.addCleanup(shutil.rmtree, src_dir, True)
        src = os.path.join(src_dir, "voice.m4a")
        with open(src, "wb") as f:
            f.write(b"audio")

        def partial_copy(_src, dst, *a, **k):
            with open(dst, "wb") as out:
                out.write(b"part")
            raise OSError(28, "No space left on device")

        with patch.object(engine.shutil, "copy2", side_effect=partial_copy):
            result = engine._copy_to_tmp_with_icloud_download(src)

        self.assertIsNone(result)
        self.assertEqual(self.leftovers("krab_ear_import_"), [])


class TestA03DiarizationTempWav(_IsolatedTempdir):
    def _prepare(self, path: str):
        from core.engine import AudioEngine
        return AudioEngine._prepare_audio_for_diarization(None, path)

    def test_subprocess_exception_removes_temp_wav(self) -> None:
        from core import engine

        with patch.object(engine.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self._prepare("/tmp/in.m4a")
        self.assertEqual(self.leftovers("krab_ear_diarization_"), [])

    def test_zero_exit_without_output_is_failure_and_removes_temp(self) -> None:
        from core import engine

        done = subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr="")
        with patch.object(engine.subprocess, "run", return_value=done):
            with self.assertRaises(RuntimeError):
                self._prepare("/tmp/in.m4a")
        self.assertEqual(self.leftovers("krab_ear_diarization_"), [])

    def test_nonzero_exit_still_raises_runtime_error_and_removes_temp(self) -> None:
        from core import engine

        done = subprocess.CompletedProcess(args=["ffmpeg"], returncode=1, stdout="", stderr="bad")
        with patch.object(engine.subprocess, "run", return_value=done):
            with self.assertRaises(RuntimeError):
                self._prepare("/tmp/in.m4a")
        self.assertEqual(self.leftovers("krab_ear_diarization_"), [])

    def test_success_returns_cleanup_flag_and_keeps_output(self) -> None:
        from core import engine

        def fake_run(cmd, *a, **k):
            Path(cmd[-1]).write_bytes(b"RIFFdata")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        with patch.object(engine.subprocess, "run", side_effect=fake_run):
            out, cleanup = self._prepare("/tmp/in.m4a")
        self.addCleanup(lambda: Path(out).unlink(missing_ok=True))
        self.assertTrue(cleanup)
        self.assertEqual(Path(out).read_bytes(), b"RIFFdata")

    def test_wav_input_is_passed_through_without_temp(self) -> None:
        out, cleanup = self._prepare("/tmp/in.wav")
        self.assertEqual((out, cleanup), ("/tmp/in.wav", False))
        self.assertEqual(self.leftovers("krab_ear_diarization_"), [])


if __name__ == "__main__":
    unittest.main()
