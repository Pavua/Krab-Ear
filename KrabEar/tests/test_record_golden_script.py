"""Тесты scripts/record_golden.py: парсер, выбор микрофона, приём записи.

Сценарий `docs/golden/r2-scenario.md` — источник фраз для записи
golden-набора (стенд качества STT, эпик R2). Парсер обязан выдавать
ровно 30 RU + 12 ES + 8 EN со сквозными id `ru_001…` и не тащить
markdown-обёртки в текст фразы.

Выбор устройства: дефолт не должен молча брать BlackHole / loopback,
если в списке есть физический вход. Живой микрофон в тесте не нужен —
подсовываем список устройств.

Приём WAV: размер выше порога недостаточен. Цифровая тишина (peak≈0)
не считается готовой записью — иначе явный --device на loopback даёт
нулевые файлы как «успех».
"""
from __future__ import annotations

import array
import importlib.util
import io
import math
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "record_golden.py"
SCENARIO_PATH = REPO_ROOT / "docs" / "golden" / "r2-scenario.md"

# Короткий preroll после первых сэмплов: иначе «Говори» совпадает с init ffmpeg.
MIN_PREROLL_SEC = 0.3
WAV_HEADER_BYTES = 44
CAPTURE_MARKER = b"GOLDENPREROLLHEAD"


def _load_module():
    spec = importlib.util.spec_from_file_location("record_golden_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    # dataclasses резолвит аннотации через sys.modules[cls.__module__] — до exec_module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ParseScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()
        cls.phrases = cls.mod.parse_scenario(SCENARIO_PATH.read_text(encoding="utf-8"))

    def test_counts_per_language(self) -> None:
        counts: dict[str, int] = {}
        for phrase in self.phrases:
            counts[phrase.lang] = counts.get(phrase.lang, 0) + 1
        self.assertEqual(counts, {"ru": 30, "es": 12, "en": 8})

    def test_ids_are_sequential_and_unique(self) -> None:
        for lang, total in (("ru", 30), ("es", 12), ("en", 8)):
            ids = [p.phrase_id for p in self.phrases if p.lang == lang]
            self.assertEqual(ids, [f"{lang}_{i:03d}" for i in range(1, total + 1)])
        all_ids = [p.phrase_id for p in self.phrases]
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_first_phrases_sane(self) -> None:
        by_id = {p.phrase_id: p.text for p in self.phrases}
        self.assertTrue(by_id["ru_001"].startswith("Напомни"))
        self.assertTrue(by_id["es_001"].startswith("Recuérdame"))
        self.assertTrue(by_id["en_001"].startswith("Remind"))
        # Парсер не тащит markdown-обёртки и не режет текст.
        for text in by_id.values():
            self.assertNotIn("`", text)
            self.assertGreater(len(text), 10)

    def test_numbered_lines_outside_phrase_sections_ignored(self) -> None:
        """«Как записывать» тоже нумерован (1–6) — в фразы попасть не должен."""
        self.assertGreater(len(self.phrases), 0)
        for phrase in self.phrases:
            self.assertNotIn("mkdir", phrase.text)
            self.assertNotIn("ffmpeg", phrase.text)

    def test_empty_text_yields_no_phrases(self) -> None:
        self.assertEqual(self.mod.parse_scenario(""), [])

    def test_text_without_phrase_sections_yields_nothing(self) -> None:
        self.assertEqual(self.mod.parse_scenario("# Заголовок\n\n1. не фраза\n"), [])


FFMPEG_LIST_OWNER_MAC = """\
[AVFoundation indev @ 0x123] AVFoundation video devices:
[AVFoundation indev @ 0x123] [0] FaceTime HD Camera
[AVFoundation indev @ 0x123] AVFoundation audio devices:
[AVFoundation indev @ 0x123] [0] BlackHole 2ch
[AVFoundation indev @ 0x123] [1] ZoomAudioDevice
[AVFoundation indev @ 0x123] [2] MacBook Pro Microphone
"""


class ResolveRecordDeviceTests(unittest.TestCase):
    """Дефолтный выбор не берёт виртуальный loopback при живом физическом входе."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def _devices(self, *pairs: tuple[int, str]):
        return [self.mod.AudioInput(index=idx, name=name) for idx, name in pairs]

    def test_main_omitted_device_is_not_hardcoded_colon_zero(self) -> None:
        """Без --device скрипт не должен считать ':0' явным выбором владельца."""
        captured: dict[str, object] = {}

        def fake_record(phrases, out_dir, device, force):
            captured["device"] = device
            return 0

        with patch.object(self.mod, "_record", fake_record):
            rc = self.mod.main([])
        self.assertEqual(rc, 0)
        self.assertIsNone(captured.get("device"))

    def test_main_keeps_explicit_device_even_if_zero(self) -> None:
        captured: dict[str, object] = {}

        def fake_record(phrases, out_dir, device, force):
            captured["device"] = device
            return 0

        with patch.object(self.mod, "_record", fake_record):
            rc = self.mod.main(["--device", ":0"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured.get("device"), ":0")

    def test_default_skips_blackhole_when_physical_macbook_exists(self) -> None:
        # Живой инцидент: ffmpeg :0 = BlackHole, микрофон MacBook = :2.
        devices = self._devices(
            (0, "BlackHole 2ch"),
            (1, "ZoomAudioDevice"),
            (2, "MacBook Pro Microphone"),
        )
        chosen = self.mod.resolve_record_device(devices)
        self.assertEqual(chosen, ":2")
        self.assertNotEqual(chosen, ":0")

    def test_single_physical_input_is_taken_even_if_not_builtin(self) -> None:
        devices = self._devices(
            (0, "BlackHole 2ch"),
            (3, "USB PnP Sound Device"),
        )
        self.assertEqual(self.mod.resolve_record_device(devices), ":3")

    def test_multiple_physical_picks_builtin_microphone(self) -> None:
        devices = self._devices(
            (0, "BlackHole 16ch"),
            (1, "USB Microphone"),
            (4, "Built-in Microphone"),
        )
        self.assertEqual(self.mod.resolve_record_device(devices), ":4")

    def test_multiple_physical_without_builtin_raises_russian_list(self) -> None:
        devices = self._devices(
            (0, "BlackHole 2ch"),
            (1, "USB Microphone"),
            (2, "Scarlett 2i2"),
        )
        with self.assertRaises(self.mod.RecordDeviceError) as ctx:
            self.mod.resolve_record_device(devices)
        message = str(ctx.exception)
        self.assertIn("USB Microphone", message)
        self.assertIn("Scarlett 2i2", message)
        self.assertIn("BlackHole 2ch", message)
        self.assertIn(":0", message)
        self.assertIn(":1", message)
        self.assertIn("--device", message)
        self.assertRegex(message.lower(), r"физическ|микрофон|устройств")

    def test_only_virtual_devices_raise(self) -> None:
        devices = self._devices(
            (0, "BlackHole 2ch"),
            (1, "Loopback Audio"),
        )
        with self.assertRaises(self.mod.RecordDeviceError) as ctx:
            self.mod.resolve_record_device(devices)
        self.assertIn("BlackHole 2ch", str(ctx.exception))

    def test_explicit_device_is_not_overwritten(self) -> None:
        devices = self._devices(
            (0, "BlackHole 2ch"),
            (2, "MacBook Pro Microphone"),
        )
        self.assertEqual(
            self.mod.resolve_record_device(devices, explicit_device=":0"),
            ":0",
        )
        self.assertEqual(
            self.mod.resolve_record_device(devices, explicit_device=":1"),
            ":1",
        )

    def test_parse_avfoundation_audio_skips_video_devices(self) -> None:
        parsed = self.mod.parse_avfoundation_audio_devices(FFMPEG_LIST_OWNER_MAC)
        self.assertEqual(
            [(item.index, item.name) for item in parsed],
            [
                (0, "BlackHole 2ch"),
                (1, "ZoomAudioDevice"),
                (2, "MacBook Pro Microphone"),
            ],
        )

    def test_record_auto_selects_macbook_not_blackhole(self) -> None:
        """Проводка: device=None + список как у владельца → ffmpeg получает :2."""
        devices = self._devices(
            (0, "BlackHole 2ch"),
            (1, "ZoomAudioDevice"),
            (2, "MacBook Pro Microphone"),
        )
        recorded: list[str] = []

        def fake_record_one(device, out_path):
            recorded.append(device)
            return True

        phrases = [self.mod.Phrase(phrase_id="ru_001", lang="ru", text="тест")]
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(self.mod, "list_avfoundation_audio_devices", return_value=devices), \
                 patch.object(self.mod, "_record_one", fake_record_one), \
                 patch.object(self.mod.sys, "platform", "darwin"), \
                 patch.object(self.mod.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                 patch("builtins.input", return_value=""), \
                 redirect_stdout(buf):
                rc = self.mod._record(phrases, Path(tmp), device=None, force=True)
        self.assertEqual(rc, 0)
        self.assertEqual(recorded, [":2"])
        self.assertNotIn("Микрофон (avfoundation): :0", buf.getvalue())


def _write_mono_pcm16_wav(path: Path, samples: list[int], rate: int = 16000) -> None:
    """Канон golden: 16 kHz mono PCM16. Без numpy — как сам скрипт."""
    frames = array.array("h", samples)
    if sys.byteorder != "little":
        frames.byteswap()
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(frames.tobytes())


def _sine_pcm16(seconds: float = 1.0, amplitude: int = 8000, freq: float = 440.0, rate: int = 16000) -> list[int]:
    n = int(rate * seconds)
    return [
        int(amplitude * math.sin(2 * math.pi * freq * i / rate))
        for i in range(n)
    ]


class _FakeFfmpegProc:
    """ffmpeg: PCM сразу в part (живой захват), SIGINT только закрывает процесс."""

    def __init__(self, part_path: Path, samples: list[int]) -> None:
        self._part_path = part_path
        self._samples = samples
        self.returncode = None
        _write_mono_pcm16_wav(part_path, samples)

    def poll(self):
        return self.returncode

    def send_signal(self, _sig) -> None:
        _write_mono_pcm16_wav(self._part_path, self._samples)
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self) -> None:
        self.returncode = 1


class IsRecordedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def test_silent_wav_above_size_threshold_is_not_recorded(self) -> None:
        # Честный комментарий: смотрим поведение _is_recorded, не подстроку скрипта.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ru_001.wav"
            _write_mono_pcm16_wav(path, [0] * 16000)
            self.assertGreaterEqual(path.stat().st_size, self.mod.MIN_WAV_BYTES)
            self.assertFalse(self.mod._is_recorded(path))

    def test_wav_with_peak_energy_counts_as_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ru_002.wav"
            _write_mono_pcm16_wav(path, _sine_pcm16())
            self.assertGreaterEqual(path.stat().st_size, self.mod.MIN_WAV_BYTES)
            self.assertTrue(self.mod._is_recorded(path))

    def test_record_one_rejects_silence_and_does_not_leave_ready_wav(self) -> None:
        silent = [0] * 16000
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "ru_001.wav"
            part_path = out_path.with_name(out_path.name + ".part")

            def _popen(cmd, stdin=None):
                return _FakeFfmpegProc(Path(cmd[-1]), silent)

            captured = io.StringIO()
            with patch.object(self.mod.subprocess, "Popen", side_effect=_popen), \
                    patch.object(self.mod.time, "sleep"), \
                    patch("builtins.input", return_value=""), \
                    redirect_stdout(captured):
                ok = self.mod._record_one(":0", out_path)

            self.assertFalse(ok)
            self.assertFalse(out_path.exists())
            self.assertFalse(part_path.exists())
            output = captured.getvalue()
            self.assertIn("молчит", output)
            self.assertIn("list_devices", output)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self._hooks: list = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.now += float(seconds)
        for hook in list(self._hooks):
            hook(self.now)


class _DelayedCapture:
    """ffmpeg, который пишет сэмплы только после init_delay (как avfoundation)."""

    def __init__(self, clock: _FakeClock, init_delay: float, marker: bytes) -> None:
        self.clock = clock
        self.init_delay = init_delay
        self.marker = marker
        self.part_path: Path | None = None
        self.cmd: list[str] | None = None
        self.returncode: int | None = None
        self.wrote = False
        self.audio_at: float | None = None
        self.started_at = 0.0
        clock._hooks.append(self._on_tick)

    def popen(self, cmd, *args, **kwargs):
        self.cmd = list(cmd)
        out = Path(cmd[-1])
        self.part_path = out
        self.started_at = self.clock.now
        self.wrote = False
        self.audio_at = None
        self.returncode = None
        # Заголовок без PCM: процесс жив, но захвата ещё нет.
        out.write_bytes(b"RIFF" + b"\x00" * (WAV_HEADER_BYTES - 4))
        return self

    def _on_tick(self, now: float) -> None:
        if self.wrote or self.returncode is not None or self.part_path is None:
            return
        if now - self.started_at >= self.init_delay:
            pad = self.marker + (b"\x00" if len(self.marker) % 2 else b"")
            samples = list(array.array("h", pad)) + [32] * 10000
            _write_mono_pcm16_wav(self.part_path, samples)
            self.wrote = True
            self.audio_at = now

    def poll(self):
        return self.returncode

    def send_signal(self, sig) -> None:
        self.returncode = 0

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.returncode = 1


class RecordOnePrerollTests(unittest.TestCase):
    """Старт захвата не должен выкидывать начало фразы и обязан держать preroll."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def _run_record_one(self, init_delay: float, device: str = ":2"):
        clock = _FakeClock()
        cap = _DelayedCapture(clock, init_delay=init_delay, marker=CAPTURE_MARKER)
        prompts: list[tuple[float, str, int]] = []

        def fake_input(message: str) -> str:
            size = cap.part_path.stat().st_size if cap.part_path and cap.part_path.exists() else 0
            prompts.append((clock.now, message, size))
            cap._on_tick(clock.now)
            return ""

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "ru_013.wav"
            buf = io.StringIO()
            with patch.object(self.mod.subprocess, "Popen", cap.popen), \
                 patch.object(self.mod.time, "sleep", clock.sleep), \
                 patch.object(self.mod.time, "monotonic", clock.monotonic), \
                 patch("builtins.input", fake_input), \
                 redirect_stdout(buf):
                ok = self.mod._record_one(device, out_path)
            final = out_path.read_bytes() if out_path.exists() else b""
            return ok, cap, clock, prompts, final, buf.getvalue()

    def test_speak_prompt_waits_for_live_capture_and_short_preroll(self) -> None:
        """Регресс ru_013: blind sleep 0.4с + «Говори» до первых сэмплов."""
        init_delay = 0.8
        ok, cap, clock, prompts, final, _log = self._run_record_one(init_delay)
        speak = [(t, msg, size) for t, msg, size in prompts if "Говори" in msg]
        self.assertTrue(speak, "ожидал промпт «Говори» после готовности захвата")
        prompt_at, _msg, size_at_prompt = speak[0]
        self.assertTrue(
            cap.wrote,
            "промпт «Говори» до записи сэмплов — начало фразы уйдёт в init ffmpeg",
        )
        self.assertIsNotNone(cap.audio_at)
        self.assertGreaterEqual(
            size_at_prompt,
            WAV_HEADER_BYTES + len(CAPTURE_MARKER),
            "на «Говори» в файле ещё нет PCM — захват не живой",
        )
        self.assertGreaterEqual(
            prompt_at - cap.audio_at,
            MIN_PREROLL_SEC,
            "нет короткого preroll после первых сэмплов",
        )
        self.assertTrue(ok)
        self.assertIn(CAPTURE_MARKER, final)
        self.assertEqual(final.find(CAPTURE_MARKER), WAV_HEADER_BYTES)

    def test_record_one_keeps_explicit_device(self) -> None:
        ok, cap, _clock, _prompts, _final, _log = self._run_record_one(0.05, device=":2")
        self.assertTrue(ok)
        self.assertIsNotNone(cap.cmd)
        dash_i = cap.cmd.index("-i")
        self.assertEqual(cap.cmd[dash_i + 1], ":2")

    def test_speak_prompt_not_shown_if_capture_never_starts(self) -> None:
        ok, _cap, clock, prompts, final, _log = self._run_record_one(init_delay=99.0)
        self.assertFalse(ok)
        self.assertFalse(any("Говори" in msg for _, msg, _ in prompts))
        self.assertEqual(final, b"")
        self.assertGreaterEqual(clock.now, 1.0)


if __name__ == "__main__":
    unittest.main()
