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
    def _swap_after_validation(self, swap, done: list):
        real = aas._validate_audio_read_path

        def validate_then_swap(p, data_dir=None):
            resolved = real(p, data_dir)
            swap()
            done.append(True)  # подмена реально выполнилась (без исключения)
            return resolved

        return patch.object(aas, "_validate_audio_read_path", side_effect=validate_then_swap)

    def _assert_refused_without_analyzer(self, h: _HandlerHarness, name: str, done: list, run) -> None:
        # Узкий ValueError: исключение самой подмены (OSError) не должно сойти за отказ.
        with self.assertRaises(ValueError, msg=f"{name}: подмена после проверки не отклонена"):
            run()
        self.assertEqual(done, [True], f"{name}: callback подмены не выполнился")
        self.assertEqual(h.calls, [], f"{name}: анализатор вызван после подмены")

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
                done: list = []
                with self._swap_after_validation(swap, done):
                    self._assert_refused_without_analyzer(h, name, done, lambda: h.run(name, str(target)))

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
                done: list = []
                with self._swap_after_validation(swap, done):
                    self._assert_refused_without_analyzer(
                        h, name, done, lambda: h.run(name, str(sub / "a.wav")))


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

    def test_snapshot_suffix_comes_from_requested_path_not_symlink_target(self) -> None:
        # F07: раньше get_audio_info получал пользовательский путь — формат
        # определялся по ЕГО расширению, а не по расширению цели симлинка.
        real = self.root / "rec.flac"
        real.write_bytes(LEGIT)
        link = self.root / "rec.WAV"
        os.symlink(real, link)
        for name in _HandlerHarness.HANDLERS:
            with self.subTest(handler=name):
                h = _HandlerHarness()
                h.run(name, str(link))
                self.assertEqual(len(h.calls), 1)
                got_path, got_bytes = h.calls[0]
                self.assertEqual(got_bytes, LEGIT)
                self.assertEqual(Path(got_path).suffix, ".wav")

    def test_snapshot_size_cap_refuses_before_analyzer(self) -> None:
        target = self.root / "big.wav"
        target.write_bytes(b"x" * 11)
        h = _HandlerHarness()
        with patch.object(aas, "_MAX_SNAPSHOT_BYTES", 10):
            with self.assertRaises(ValueError):
                h.run("analyze_audio_quality", str(target))
        self.assertEqual(h.calls, [])


class TestA07SourceFdNeverLeaks(_TmpTree):
    """F01: исходный fd не должен утекать, если снимок не удалось подготовить."""

    def _open_fds(self) -> set[int]:
        return {int(n) for n in os.listdir("/dev/fd")}

    def _run_and_check_no_leak(self, failing_patch, exc_type) -> None:
        target = self.root / "leak.wav"
        target.write_bytes(LEGIT)
        opened: list[int] = []
        real_open = aas._open_pinned_regular_file

        def spy_open(resolved):
            fd = real_open(resolved)
            opened.append(fd)
            return fd

        h = _HandlerHarness()
        before = self._open_fds()
        with patch.object(aas, "_open_pinned_regular_file", side_effect=spy_open), failing_patch:
            with self.assertRaises(exc_type):
                h.run("analyze_audio_quality", str(target))
        self.assertEqual(h.calls, [])
        for fd in opened:
            with self.assertRaises(OSError, msg=f"исходный fd {fd} остался открытым"):
                os.fstat(fd)
        self.assertEqual(self._open_fds() - before, set(), "после отказа остались открытые fd")

    def test_tempdir_creation_failure_does_not_leak_source_fd(self) -> None:
        enospc = OSError(28, "No space left on device")
        self._run_and_check_no_leak(
            patch.object(aas.tempfile, "TemporaryDirectory", side_effect=enospc), OSError)

    def test_fdopen_failure_does_not_leak_source_fd(self) -> None:
        emfile = OSError(24, "Too many open files")
        self._run_and_check_no_leak(patch.object(aas.os, "fdopen", side_effect=emfile), OSError)


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

        with patch.object(engine.shutil, "copy2", side_effect=partial_copy) as copy:
            result = engine._copy_to_tmp_with_icloud_download(src)

        copy.assert_called_once()
        self.assertIsNone(result)
        self.assertEqual(self.leftovers("krab_ear_import_"), [])

    def test_interrupt_during_copy_removes_partial_copy_and_propagates(self) -> None:
        # F06: KeyboardInterrupt/SystemExit не глушатся в None, но неполная копия удаляется.
        from core import engine

        src_dir = tempfile.mkdtemp(prefix="ke_src_", dir=os.path.dirname(self.tmpdir))
        self.addCleanup(shutil.rmtree, src_dir, True)
        src = os.path.join(src_dir, "voice.m4a")
        with open(src, "wb") as f:
            f.write(b"audio")

        for exc_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(exc=exc_type.__name__):
                def partial_copy(_src, dst, *a, exc_type=exc_type, **k):
                    with open(dst, "wb") as out:
                        out.write(b"part")
                    raise exc_type()

                with patch.object(engine.shutil, "copy2", side_effect=partial_copy) as copy:
                    with self.assertRaises(exc_type):
                        engine._copy_to_tmp_with_icloud_download(src)
                copy.assert_called_once()
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
