#!/usr/bin/env python3
"""Запись эталонного набора R2 (golden) — интерактивный помощник.

Зачем: стенд R2 меряет WER/латентность STT на одних и тех же записях.
Пишет по одному WAV (16 kHz mono PCM16 — канон пайплайна) на фразу из
docs/golden/r2-scenario.md. Аудио остаётся ЛОКАЛЬНО, в git не попадает.

Использование (или двойной клик по «Record Golden Set.command»):
    python3 scripts/record_golden.py --list       # показать фразы
    python3 scripts/record_golden.py --dry-run    # проверка без записи
    python3 scripts/record_golden.py              # живая запись (Enter: старт/стоп)
    python3 scripts/record_golden.py --device :2  # явный вход; не перетирается
    python3 scripts/record_golden.py --force      # перезаписать всё

Без --device не берёт виртуальный loopback (BlackHole и аналоги), если есть
физический вход. Несколько физических без MacBook/Built-in — ошибка со списком.

Повторный запуск продолжает с первой незаписанной фразы (resume).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = REPO_ROOT / "docs" / "golden" / "r2-scenario.md"
DEFAULT_OUT = Path.home() / "Library" / "Application Support" / "KrabEar" / "golden"
LANG_CODES = {"RU": "ru", "ES": "es", "EN": "en"}
MIN_WAV_BYTES = 16000  # ~0.5 c при 16 kHz mono PCM16 (32 КБ/с)
_VIRTUAL_MARKERS = (
    "blackhole",
    "loopback",
    "soundflower",
    "zoom audio",
    "zoomaudiodevice",
    "vb-cable",
    "vb cable",
    "vb-audio",
    "cable input",
    "cable output",
    "aggregate device",
    "multi-output",
    "multi output",
    "virtual",
    "krisp",
    "teams audio",
    "microsoft teams",
    "discord",
    "steam streaming",
)
_BUILTIN_MARKERS = ("macbook", "built-in", "built in")


@dataclass(frozen=True)
class Phrase:
    phrase_id: str
    lang: str
    text: str


@dataclass(frozen=True)
class AudioInput:
    index: int
    name: str

    @property
    def ffmpeg_spec(self) -> str:
        return f":{self.index}"


class RecordDeviceError(ValueError):
    """Не удалось выбрать физический микрофон без явного --device."""


def parse_scenario(text: str) -> list[Phrase]:
    """Вытаскивает фразы из секций «## Фразы RU|ES|EN» сценария."""
    phrases: list[Phrase] = []
    current: str | None = None
    counters: dict[str, int] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            parts = line[3:].strip().split()
            current = LANG_CODES.get(parts[1].upper()) if len(parts) >= 2 and parts[0] == "Фразы" else None
            continue
        if current is None:
            continue
        match = re.match(r"^(\d+)\.\s+(.+)$", line)
        if not match:
            continue
        counters[current] = counters.get(current, 0) + 1
        phrases.append(
            Phrase(
                phrase_id=f"{current}_{counters[current]:03d}",
                lang=current,
                text=match.group(2).strip(),
            )
        )
    return phrases


def is_virtual_input(name: str) -> bool:
    lowered = name.casefold()
    return any(marker in lowered for marker in _VIRTUAL_MARKERS)


def is_builtin_microphone(name: str) -> bool:
    lowered = name.casefold()
    return any(marker in lowered for marker in _BUILTIN_MARKERS)


def parse_avfoundation_audio_devices(ffmpeg_output: str) -> list[AudioInput]:
    """Разбирает stderr `ffmpeg -f avfoundation -list_devices` на аудиовходы."""
    devices: list[AudioInput] = []
    in_audio = False
    device_re = re.compile(r"\[(\d+)\]\s+(.+?)\s*$")
    for raw in ffmpeg_output.splitlines():
        line = raw.strip()
        lowered = line.casefold()
        if "avfoundation audio devices:" in lowered:
            in_audio = True
            continue
        if "avfoundation video devices:" in lowered:
            in_audio = False
            continue
        if not in_audio:
            continue
        match = device_re.search(line)
        if not match:
            continue
        devices.append(AudioInput(index=int(match.group(1)), name=match.group(2).strip()))
    return devices


def list_avfoundation_audio_devices() -> list[AudioInput]:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-f",
            "avfoundation",
            "-list_devices",
            "true",
            "-i",
            "",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return parse_avfoundation_audio_devices((proc.stderr or "") + (proc.stdout or ""))


def _format_device_list(devices: list[AudioInput]) -> str:
    lines: list[str] = []
    for item in devices:
        kind = "виртуальный" if is_virtual_input(item.name) else "физический"
        lines.append(f"  {item.ffmpeg_spec}  {item.name}  ({kind})")
    return "\n".join(lines)


def resolve_record_device(
    devices: list[AudioInput],
    explicit_device: str | None = None,
) -> str:
    """Выбирает ffmpeg-спеку `:N`. Явный --device не перетирается."""
    if explicit_device is not None and str(explicit_device).strip():
        return str(explicit_device).strip()
    listing = _format_device_list(devices)
    if not devices:
        raise RecordDeviceError(
            "не нашёл ни одного аудиовхода avfoundation.\n"
            'Проверь: ffmpeg -f avfoundation -list_devices true -i ""'
        )
    physical = [item for item in devices if not is_virtual_input(item.name)]
    if not physical:
        raise RecordDeviceError(
            "нет физического микрофона — только виртуальные устройства "
            "(BlackHole / loopback).\n"
            "Укажи явно, если это намеренно: python3 scripts/record_golden.py --device :N\n"
            f"Доступные устройства:\n{listing}"
        )
    if len(physical) == 1:
        return physical[0].ffmpeg_spec
    macbooks = [item for item in physical if "macbook" in item.name.casefold()]
    if macbooks:
        return macbooks[0].ffmpeg_spec
    builtins = [item for item in physical if is_builtin_microphone(item.name)]
    if builtins:
        return builtins[0].ffmpeg_spec
    raise RecordDeviceError(
        "несколько физических входов, встроенный микрофон (MacBook / Built-in) "
        "не найден.\n"
        "Укажи явно: python3 scripts/record_golden.py --device :N\n"
        f"Доступные устройства:\n{listing}"
    )


def _is_recorded(path: Path) -> bool:
    return path.exists() and path.stat().st_size >= MIN_WAV_BYTES


def _record_one(device: str, out_path: Path) -> bool:
    """Пишет одну фразу: Enter — старт, Enter — стоп (SIGINT, корректный WAV).

    Пишем в `<id>.wav.part` и переименовываем ТОЛЬКО после чистого стопа:
    обрыв (Ctrl+C/EOF) не оставит половинку фразы, которую resume примет за
    готовую (обрывок → завышенный WER на эталоне).
    """
    part_path = out_path.with_name(out_path.name + ".part")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "avfoundation", "-i", device,
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", "-y", str(part_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
    stopped_cleanly = False
    try:
        time.sleep(0.4)
        if proc.poll() is not None:
            print("    ffmpeg завершился сразу — похоже, нет доступа к микрофону.")
            part_path.unlink(missing_ok=True)
            return False
        input("    ▶ Говори (Enter — остановить): ")
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
            stopped_cleanly = True
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    except (KeyboardInterrupt, EOFError):
        # Не оставляем осиротевший ffmpeg и половинку файла.
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        part_path.unlink(missing_ok=True)
        raise
    if not stopped_cleanly or not _is_recorded(part_path):
        print("    Файл пустой или слишком короткий — попробуем ещё раз.")
        part_path.unlink(missing_ok=True)
        return False
    os.replace(part_path, out_path)
    size_kb = out_path.stat().st_size // 1024
    print(f"    ✓ {out_path.name} ({size_kb} КБ)")
    return True


def _dry_run(phrases: list[Phrase], out_dir: Path) -> int:
    existing = sum(1 for p in phrases if _is_recorded(out_dir / f"{p.phrase_id}.wav"))
    print(f"DRY-RUN: {len(phrases)} фраз → {out_dir} (файлы не создаются, микрофон не нужен)")
    print(f"Уже записано на диске: {existing}")
    for phrase in phrases[:3]:
        print(f"  {phrase.phrase_id}: «{phrase.text}»")
    print("  … (остальные — тем же порядком)")
    return 0


def _record(phrases: list[Phrase], out_dir: Path, device: str | None, force: bool) -> int:
    if sys.platform != "darwin":
        print("ERROR: запись golden доступна только на macOS (avfoundation).")
        return 1
    if shutil.which("ffmpeg") is None:
        print("ERROR: ffmpeg не найден в PATH. Установи: brew install ffmpeg")
        return 1

    selected_name = ""
    if not (device and str(device).strip()):
        try:
            inputs = list_avfoundation_audio_devices()
            device = resolve_record_device(inputs)
            selected_name = next(
                (item.name for item in inputs if item.ffmpeg_spec == device),
                "",
            )
        except RecordDeviceError as exc:
            print(f"ERROR: {exc}")
            return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    todo = [p for p in phrases if force or not _is_recorded(out_dir / f"{p.phrase_id}.wav")]
    skipped = len(phrases) - len(todo)
    print(f"Каталог: {out_dir}")
    name_suffix = f"  — {selected_name}" if selected_name else ""
    print(
        f"Микрофон (avfoundation): {device}{name_suffix}  "
        '(список устройств: ffmpeg -f avfoundation -list_devices true -i "")'
    )
    if skipped:
        print(f"Пропускаю {skipped} уже записанных (--force — перезаписать всё).")
    if not todo:
        print("Все фразы уже записаны. 👍")
        return 0
    print("Первый запуск: macOS может спросить доступ Терминала к микрофону — разреши.")
    print("Держи паузу ~1 секунду до и после фразы, говори своим обычным темпом.")

    done = 0
    try:
        for idx, phrase in enumerate(todo, 1):
            path = out_dir / f"{phrase.phrase_id}.wav"
            print(f"\n[{idx}/{len(todo)}] {phrase.phrase_id}:\n    «{phrase.text}»")
            while True:
                input("    Enter — начать запись (Ctrl+C — выйти): ")
                if _record_one(device, path):
                    done += 1
                    break
                answer = input("    Enter — повторить, q — пропустить: ").strip().lower()
                if answer == "q":
                    break
    except (KeyboardInterrupt, EOFError):
        print(f"\n\nОстановлено. Записано сейчас: {done}. Прогресс сохранён —")
        print("запусти снова, продолжу с первой незаписанной фразы.")
        return 130

    print(f"\nГотово: записано {done} из {len(todo)} этой сессией.")
    remaining = sum(1 for p in phrases if not _is_recorded(out_dir / f"{p.phrase_id}.wav"))
    if remaining:
        print(f"Осталось: {remaining} — запусти снова, чтобы продолжить.")
    else:
        print("Все фразы на месте. Сообщи координатору «записал» — дальше стенд R2.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Запись golden-набора R2 (эталонные фразы STT)")
    parser.add_argument("--list", action="store_true", help="только показать фразы")
    parser.add_argument("--dry-run", action="store_true", help="прогон без записи")
    parser.add_argument("--force", action="store_true", help="перезаписать существующие файлы")
    parser.add_argument(
        "--device",
        default=None,
        help='avfoundation-вход, например ":2". Если не задан — физический микрофон, не BlackHole',
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="каталог для WAV-файлов")
    args = parser.parse_args(argv)

    if not SCENARIO_PATH.exists():
        print(f"ERROR: сценарий не найден: {SCENARIO_PATH}")
        return 1
    phrases = parse_scenario(SCENARIO_PATH.read_text(encoding="utf-8"))
    if not phrases:
        print("ERROR: не распарсил ни одной фразы — проверь формат сценария.")
        return 1

    if args.list:
        for phrase in phrases:
            print(f"{phrase.phrase_id}  {phrase.text}")
        counts = Counter(p.lang for p in phrases)
        summary = ", ".join(f"{code.upper()}={counts[code]}" for code in ("ru", "es", "en") if code in counts)
        print(f"\nИтого: {summary} (всего {len(phrases)}).")
        return 0

    if args.dry_run:
        return _dry_run(phrases, Path(args.out))

    return _record(phrases, Path(args.out), args.device, args.force)


if __name__ == "__main__":
    sys.exit(main())
