"""CLI-утилита для пакетной или точечной транскрибации аудио с diarization.

Скрипт использует тот же AudioEngine, что и основной backend Krab Ear, чтобы
не расходиться по логике STT. Результат сохраняется в Markdown рядом с аудио,
что удобно для звонков и заметок в Obsidian.
"""

from __future__ import annotations
from core.engine import AudioEngine

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _build_markdown(audio_path: Path, result: dict[str, object]) -> str:
    """Собирает Markdown-отчёт по транскрибации."""
    diarization = result.get("diarization") if isinstance(result, dict) else None
    diarization = diarization if isinstance(diarization, dict) else {}
    speaker_turns = diarization.get("speaker_turns", [])
    speaker_turns = speaker_turns if isinstance(speaker_turns, list) else []

    lines = [
        f"# Транскрипт: {audio_path.name}",
        "",
        f"- Дата обработки: {datetime.now().isoformat(timespec='seconds')}",
        f"- Движок: {result.get('engine', 'unknown')}",
        f"- Модель: {result.get('model', 'unknown')}",
        f"- Уверенность: {result.get('confidence', 0.0)}",
        f"- Длительность обработки: {result.get('duration_ms', 0)} мс",
        f"- Diarization: {'включена' if diarization.get('enabled') else 'выключена'}",
    ]

    if diarization.get("error"):
        lines.append(f"- Ошибка diarization: {diarization['error']}")

    lines.extend(
        [
            "",
            "## Полный текст",
            "",
            str(result.get("text", "")).strip() or "_Пустой результат_",
            "",
            "## По спикерам",
            "",
        ]
    )

    if speaker_turns:
        for turn in speaker_turns:
            speaker = turn.get("speaker", "SPEAKER_UNKNOWN")
            start = float(turn.get("start", 0.0))
            end = float(turn.get("end", 0.0))
            text = str(turn.get("text", "")).strip() or "..."
            lines.append(f"- `{start:07.2f}s - {end:07.2f}s` **{speaker}**: {text}")
    else:
        lines.append("_Сегменты по спикерам не получены._")

    return "\n".join(lines).strip() + "\n"


def main() -> int:
    """Парсит аргументы, запускает STT и сохраняет Markdown."""
    parser = argparse.ArgumentParser(description="Транскрибация аудио с diarization для Krab Ear.")
    parser.add_argument("audio_path", help="Путь к аудиофайлу.")
    parser.add_argument("--output", help="Путь к Markdown-файлу. По умолчанию рядом с аудио.")
    parser.add_argument("--quality-profile", default="max", choices=["balanced", "max"])
    parser.add_argument("--cleanup-profile", default="soft", choices=["soft", "strict"])
    parser.add_argument("--domain", default="meeting")
    args = parser.parse_args()

    audio_path = Path(args.audio_path).expanduser().resolve()
    if not audio_path.exists():
        raise FileNotFoundError(f"Файл не найден: {audio_path}")

    output_path = Path(args.output).expanduser().resolve() if args.output else audio_path.with_suffix(".md")
    engine = AudioEngine()
    engine.set_quality_profile(args.quality_profile)
    result = engine.transcribe(
        str(audio_path),
        cleanup_profile=args.cleanup_profile,
        is_preview=False,
        domain=args.domain,
    )
    if result.get("status") == "error":
        raise RuntimeError(str(result.get("error", "Неизвестная ошибка транскрибации")))

    markdown = _build_markdown(audio_path, result)
    output_path.write_text(markdown, encoding="utf-8")
    print(output_path)
    return 0


if __name__ == "__main__":
    try:
        import multiprocessing as mp
        mp.set_start_method("spawn", force=True)
    except RuntimeError:
        pass  # Context already set.
    raise SystemExit(main())
