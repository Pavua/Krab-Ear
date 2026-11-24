#!/usr/bin/env python3
"""Запись личного датасета для обучения wake-word модели «Краб».

Зачем: встроенная krab_ru обучена только на синтезе (Silero) и срабатывает на
тишине (замер 24.11.2026). Для рабочей модели нужны ВАШ голос («Краб», «эй, Краб»
в разной манере) и ваша обычная речь без этого слова как негативы.

Как работает: показывает фразу, по Enter пишет ровно CLIP_SEC секунд с микрофона
(16 кГц, моно, 16 бит), сохраняет WAV и строку в manifest.jsonl. Можно прервать и
продолжить — уже записанные клипы пропускаются.

Управление: Enter — записать; r — переписать предыдущий; s — пропустить блок;
q — выйти (прогресс сохранён).

Приватность: записи остаются локально в `~/Library/Application Support/KrabEar/wake_dataset/` (вне
репозитория и worktree, не синхронизируется iCloud, без отдельного запроса доступа к «Документам»),
никуда не отправляются. ВНИМАНИЕ: privacy-purge Krab Ear этот каталог НЕ чистит — при необходимости
удалить его нужно вручную (`rm -r`). Голос — биометрия: не копируйте каталог в облако.

Запуск (из корня репозитория):
    .venv_krab_ear/bin/python scripts/record_wake_dataset.py
или двойным кликом `scripts/record_wake_dataset.command`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 16000
CLIP_SEC = 2.5
# Порог «слишком тихо» и «клиппинг» для предупреждений (доли полной шкалы int16).
MIN_RMS = 0.004
CLIP_PEAK = 0.98

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = Path.home() / "Library" / "Application Support" / "KrabEar" / "wake_dataset"


@dataclass(frozen=True)
class Block:
    """Серия однотипных записей."""

    key: str
    label: str  # "pos" — слово-пробуждение есть, "neg" — нет
    hint: str  # как говорить
    prompts: tuple[str, ...]
    clip_sec: float = CLIP_SEC


def build_plan() -> list[Block]:
    """План записи: ~88 позитивов и ~81 негатив, примерно 12 минут."""
    return [
        Block("pos_normal", "pos", "обычным голосом, как зовёте ассистента", tuple(["Краб"] * 10 + ["Эй, Краб"] * 10)),
        Block("pos_quiet", "pos", "ТИХО, вполголоса, как будто рядом спят", tuple(["Краб"] * 5 + ["Эй, Краб"] * 5)),
        Block("pos_loud", "pos", "ГРОМКО, через комнату", tuple(["Краб"] * 5 + ["Эй, Краб"] * 5)),
        Block("pos_far", "pos", "с расстояния 2–3 метра от микрофона, обычной громкостью", tuple(["Краб"] * 8 + ["Эй, Краб"] * 8)),
        Block("pos_fast_slow", "pos", "быстро / протяжно, по подсказке в скобках", tuple(
            ["Краб (быстро)"] * 4 + ["Краб (протяжно: Кра-аб)"] * 4 + ["Эй, Краб (быстро)"] * 4 + ["Эй, Краб (протяжно)"] * 4)),
        Block("pos_in_phrase", "pos", "слово в начале обычной просьбы (произнесите просьбу целиком)", (
            "Краб, открой историю", "Краб, который час", "Эй, Краб, запиши заметку", "Краб, что на сегодня",
            "Краб, переведи это", "Эй, Краб, включи диктовку", "Краб, найди вчерашнюю запись", "Краб, помоги",
            "Эй, Краб, ты слышишь", "Краб, продиктую текст", "Краб, напомни про встречу", "Эй, Краб, прочитай сообщение",
            "Краб, останови запись", "Краб, где мои заметки", "Эй, Краб, сколько времени", "Краб, давай начнём")),
        Block("neg_lookalikes", "neg", "созвучные слова — НЕ «Краб»; говорите чётко", (
            "Краб-салат", "Грабли", "Кроб", "Крап", "Карп", "Драп", "Краем", "Кроп", "Эй, Карл", "Эй, Грэб",
            "Эй, Гриб", "Крупа", "Брат", "Рабочий", "Крабы", "Крепко")),
        Block("neg_ru_speech", "neg", "обычная русская речь, любые слова, но БЕЗ слова «краб»", (
            "Сегодня нужно отправить документы до обеда.", "Позвони мне, когда освободишься.",
            "Я думаю, что встреча перенесётся на завтра.", "Поставь, пожалуйста, чайник.",
            "Какая завтра погода в Торревьехе?", "Надо купить хлеб и молоко.",
            "Давай обсудим это позже вечером.", "Отправь мне ссылку на статью.",
            "Я уже закончил, можем начинать.", "Включи, пожалуйста, музыку потише.",
            "Это очень интересная идея, давай попробуем.", "Мне нужно записаться к врачу на следующей неделе.",
            "Сегодня на обед будет рыба с овощами.", "Ты видел вчерашний матч, это было что-то.",
            "Пароль придёт в сообщении, не забудь его ввести.", "Завтра рано вставать, пойду спать.",
            "Я оставил ключи на столе возле окна.", "Включи свет в коридоре, пожалуйста.",
            "Мы договорились встретиться возле остановки.", "Посмотри, пожалуйста, что там с оплатой.",
            "Скажи, пожалуйста, сколько это стоит.", "Подожди секунду, я сейчас вернусь.",
            "Я не уверен, что это сработает.", "Давай проверим ещё раз результат.")),
        Block("neg_es_speech", "neg", "обычная испанская речь, любые фразы", (
            "Buenos días, quería pedir una cita para la semana que viene.", "Gracias, muy amable, hasta luego.",
            "¿Me puede decir cuánto cuesta esto, por favor?", "Necesito que me envíen el contrato por correo.",
            "No entiendo, ¿puede repetirlo más despacio?", "Voy a llegar un poco tarde, lo siento.",
            "¿Dónde está la farmacia más cercana?", "Muchas gracias por su ayuda.",
            "Tengo una reserva a nombre de Pablo.", "¿A qué hora abre mañana la oficina?",
            "Me duele la cabeza desde ayer por la tarde.", "¿Puedo pagar con tarjeta, por favor?",
            "Necesito renovar la receta de mi medicación.", "El vuelo sale a las siete de la mañana.",
            "¿Cuál es el número de teléfono de la clínica?", "Perdone, creo que me he equivocado de sala.")),
        Block("neg_self_talk", "neg", "говорите сами с собой или с кем-то рядом: бормочите, смейтесь, вздыхайте, кашляйте", tuple(
            ["Свободная речь, 2–3 секунды"] * 12)),
        Block("neg_room_silence", "neg", "ничего не говорите: фон комнаты (тишина, шум, вентилятор)", tuple(
            ["Тишина / фон комнаты"] * 8), clip_sec=6.0),
        Block("neg_typing", "neg", "печатайте на клавиатуре и щёлкайте мышью (не говорите)", tuple(
            ["Клавиатура"] * 5), clip_sec=6.0),
    ]


def assess_clip(samples_int16: list[int] | "object") -> dict:
    """Качество клипа: rms/peak (доли шкалы) и предупреждения. Чистая функция для тестов."""
    values = list(samples_int16)
    if not values:
        return {"rms": 0.0, "peak": 0.0, "warnings": ["пустая запись"]}
    scale = 32768.0
    peak = max(abs(v) for v in values) / scale
    rms = (sum(v * v for v in values) / len(values)) ** 0.5 / scale
    warnings: list[str] = []
    if peak >= CLIP_PEAK:
        warnings.append("клиппинг: говорите тише или отодвиньтесь")
    if rms < MIN_RMS:
        warnings.append("слишком тихо: приблизьтесь к микрофону (для блока тишины это нормально)")
    return {"rms": round(rms, 5), "peak": round(peak, 4), "warnings": warnings}


def clip_filename(block: Block, index: int) -> str:
    return f"{block.key}_{index:03d}.wav"


def pending_indices(block: Block, out_dir: Path) -> list[int]:
    """Индексы клипов блока, которых ещё нет на диске (для продолжения после прерывания)."""
    return [i for i in range(len(block.prompts)) if not (out_dir / clip_filename(block, i)).exists()]


def write_wav(path: Path, samples_int16: bytes) -> None:
    """Атомарно: пишем во временный файл и переименовываем, иначе прерывание посреди записи оставило бы
    усечённый WAV, который pending_indices счёл бы готовым."""
    tmp = path.with_name(path.name + ".tmp")
    with wave.open(str(tmp), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(samples_int16)
    tmp.replace(path)


def append_manifest(out_dir: Path, record: dict) -> None:
    with (out_dir / "manifest.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def remove_manifest_entry(out_dir: Path, filename: str) -> None:
    """Убирает из manifest.jsonl строки про `filename` (при перезаписи клипа не плодим дубли)."""
    path = out_dir / "manifest.jsonl"
    if not path.exists():
        return
    kept = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(line).get("file") == filename:
                continue
        except ValueError:
            pass
        kept.append(line)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(f"{ln}\n" for ln in kept), encoding="utf-8")
    tmp.replace(path)


def is_unusable_positive(block: Block, quality: dict) -> bool:
    """Позитив с rms ниже порога не сохраняем: macOS при запрете микрофона (TCC) отдаёт нули, и такие
    «позитивы» отравили бы датасет ровно тем отказом, из-за которого krab_ru срабатывает на тишине."""
    return block.label == "pos" and quality["rms"] < MIN_RMS


def record_clip(seconds: float):
    """Пишет `seconds` с микрофона по умолчанию. Импорт sounddevice — только тут, чтобы тесты
    и `--plan` не открывали микрофон."""
    import numpy as np
    import sounddevice as sd

    frames = int(seconds * SAMPLE_RATE)
    data = sd.rec(frames, samplerate=SAMPLE_RATE, channels=1, dtype="int16")
    sd.wait()
    return np.asarray(data).reshape(-1)


def _check_microphone_stack() -> str | None:
    """Сообщение об ошибке, если запись невозможна (проверяем ДО первой фразы)."""
    try:
        import numpy  # noqa: F401
        import sounddevice  # noqa: F401
    except ImportError as exc:
        return f"не хватает пакета ({exc.name}). Запускайте через .venv_krab_ear: .venv_krab_ear/bin/python scripts/record_wake_dataset.py"
    return None


def run(out_dir: Path) -> int:
    problem = _check_microphone_stack()
    if problem:
        print(f"Ошибка: {problem}")
        return 2
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Ошибка: не удалось создать каталог записей {out_dir}: {exc}\nУкажите другой путь: --out /путь/к/каталогу")
        return 2
    plan = build_plan()
    total = sum(len(b.prompts) for b in plan)
    done = total - sum(len(pending_indices(b, out_dir)) for b in plan)
    print(f"Каталог записей: {out_dir}\nГотово {done} из {total}. Enter — записать, r — переписать предыдущий, s — пропустить блок, q — выйти.\n")
    last: tuple[Block, int] | None = None
    skipped: set[str] = set()
    while True:
        progressed = False
        for block in plan:
            if block.key in skipped:
                continue
            announced = False
            while True:
                todo = pending_indices(block, out_dir)
                if not todo:
                    break
                if not announced:
                    print(f"=== {block.key} ({block.label.upper()}) — {block.hint} ===")
                    announced = True
                idx = todo[0]
                prompt = block.prompts[idx]
                try:
                    cmd = input(f"[{idx + 1}/{len(block.prompts)}] «{prompt}»  → Enter: запись {block.clip_sec:g} с > ").strip().lower()
                except EOFError:
                    print("\nВвод закрыт. Прогресс сохранён.")
                    return 0
                if cmd == "q":
                    print("Прогресс сохранён. Запустите снова, чтобы продолжить.")
                    return 0
                if cmd == "s":
                    skipped.add(block.key)
                    break
                if cmd == "r":
                    if last is None:
                        print("  (нечего переписывать)")
                        continue
                    lb, li = last
                    name = clip_filename(lb, li)
                    (out_dir / name).unlink(missing_ok=True)
                    remove_manifest_entry(out_dir, name)
                    print(f"  удалён предыдущий клип: «{lb.prompts[li]}» — он будет записан заново")
                    last = None
                    progressed = True
                    if lb is not block:
                        break  # вернуться к блоку удалённого клипа на следующем проходе
                    continue
                time.sleep(0.25)
                print("  ● запись…", end="", flush=True)
                try:
                    samples = record_clip(block.clip_sec)
                except Exception as exc:  # PortAudioError и пр.: понятное сообщение вместо трассировки
                    print(f"\nОшибка записи с микрофона: {exc}\nПроверьте устройство ввода и доступ к микрофону для Терминала.")
                    return 3
                print(" готово")
                values = samples.tolist()
                quality = assess_clip(values)
                if values and max(abs(v) for v in values) == 0:
                    print("\n⚠ Микрофон отдал полную тишину (точные нули — так бывает при запрете доступа, а не в тихой комнате). Вероятно, у Терминала нет доступа к микрофону:\n"
                          "  Системные настройки → Конфиденциальность → Микрофон → разрешить Терминалу, и запустите скрипт заново.")
                    return 4
                for w in quality["warnings"]:
                    print(f"  ⚠ {w}")
                if is_unusable_positive(block, quality):
                    print("  ✗ клип НЕ сохранён (слишком тихо для позитива) — повторите ближе к микрофону")
                    continue
                path = out_dir / clip_filename(block, idx)
                write_wav(path, samples.tobytes())
                append_manifest(out_dir, {
                    "file": path.name, "label": block.label, "block": block.key, "prompt": prompt,
                    "sec": block.clip_sec, "rate": SAMPLE_RATE, "rms": quality["rms"], "peak": quality["peak"],
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
                last = (block, idx)
                progressed = True
            print()
        if not any(pending_indices(b, out_dir) for b in plan if b.key not in skipped) or not progressed:
            break
    print("Записано. Дальше — обучение (см. wake_word_models/README.md §8a).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="каталог для WAV и manifest.jsonl")
    parser.add_argument("--plan", action="store_true", help="только показать план, микрофон не открывать")
    args = parser.parse_args(argv)
    if args.plan:
        for b in build_plan():
            print(f"{b.key:18s} {b.label}  {len(b.prompts):3d} клипов × {b.clip_sec:g} с — {b.hint}")
        return 0
    try:
        return run(args.out)
    except KeyboardInterrupt:
        print("\nПрервано. Прогресс сохранён.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
