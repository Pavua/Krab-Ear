#!/usr/bin/env python3
"""Починка истории: цифра 0 на месте съеденного союза «и»/«y» (2026-08-30).

ОТКУДА БАГ
----------
`core/number_normalizer.py` держал союз прямо в паттерне числительных —
составные формы «двадцать и пять» → 25 обязаны работать. Но паттерн матчил и
группу ИЗ ОДНОГО союза, а парсер возвращал для неё стартовый `result = 0`.
Живой пример из звонка владельца:

    «до которого часа вы работаете И есть ли места»
    → «до которого часа вы работаете 0 есть ли места»

Нашла сессия Voice Gateway на живых звонках. 🔴 Первый замер дал 767 записей
(6.0%) — и был НЕВЕРЕН: 595 из них оказались зацикленной галлюцинацией STT
(«задания 0 ответы на вопросы, задания 0 ответы…» по 26 повторов в записи),
где ноль лишь размножен петлёй. Честный масштаб — 178 записей (1.4%),
455 замен. Массовая находка почти всегда прячет один повторяющийся источник. Сам нормализатор починен отдельно (PR #1966) — этот скрипт
чинит только УЖЕ ИСПОРЧЕННЫЕ записи, задним числом.

🔴 ПОЧЕМУ КРИТЕРИЙ УЖЕ ПРОСТОГО «0 МЕЖДУ СЛОВАМИ»
--------------------------------------------------
Настоящий ноль тоже стоит между буквенными словами: «температура 0 градусов»,
«осталось 0 секунд». Отличает их СЛЕДУЮЩЕЕ слово — единица измерения или
счётное существительное. Такие случаи не трогаем: лучше оставить испорченную
запись, чем испортить здоровую (правка исторических данных владельца
необратима по смыслу, даже когда обратима по файлу).

Второй отсев — ПРЕДЛОГ после нуля. «не должно быть 0 на балансе» — настоящий
ноль (баланс карты), и «число + предлог» вообще законная конструкция. Союз там
тоже возможен грамматически, так что часть верных починок мы теряем осознанно:
оставить испорченную запись дешевле, чем испортить здоровую.

Язык берём из самой записи: кириллица → «и», латиница → «y».
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time

# Единицы измерения и счётные слова: после них 0 — настоящий ноль, не союз.
_REAL_ZERO_NEXT = {
    "секунд", "секунды", "секунда", "сек",
    "минут", "минуты", "минута", "мин",
    "часов", "часа", "час",
    "дней", "дня", "день", "суток", "недель", "месяцев", "лет", "года", "год",
    "градусов", "градуса", "градус",
    "процентов", "процента", "процент",
    "рублей", "рубля", "рубль", "долларов", "доллара", "евро", "центов",
    "метров", "метра", "метр", "километров", "сантиметров", "миллиметров",
    "килограммов", "килограмм", "граммов", "грамм", "тонн", "литров",
    "штук", "штуки", "раз", "раза", "балл", "баллов", "баллы",
    "байт", "килобайт", "мегабайт", "гигабайт", "бит",
    "segundos", "minutos", "horas", "grados", "euros", "metros", "veces",
}

# Предлоги: «0 на балансе», «0 в кассе» — законная конструкция с числом.
_REAL_ZERO_NEXT |= {
    "на", "в", "во", "с", "со", "к", "ко", "по", "за", "под", "из", "от", "до",
    "у", "о", "об", "при", "про", "над", "перед", "без", "для", "через",
    "между", "около", "после", "среди",
    "en", "de", "por", "para", "con", "sin", "sobre", "entre", "hasta",
}

# Слова ПЕРЕД числом, после которых 0 — идентификатор или значение, не союз.
_REAL_ZERO_PREV = {
    "версия", "версии", "пункт", "пункте", "номер", "номере", "счёт", "счет",
    "уровень", "уровня", "этап", "шкале", "шкала", "равно", "равен", "ноль",
    "индекс", "код", "вариант", "релиз", "билд",
    "version", "nivel", "punto", "numero", "número",
}

_LETTER = r"[А-Яа-яЁёA-Za-zÀ-ÿ]"
_PATTERN = re.compile(rf"(?<=\s)({_LETTER}+)(\s+)0(\s+)({_LETTER}+)")


def _conjunction_for(text: str) -> str:
    """Язык записи решает, какой союз восстанавливать."""
    cyr = sum(1 for ch in text if "А" <= ch <= "я" or ch in "Ёё")
    lat = sum(1 for ch in text if ("a" <= ch.lower() <= "z"))
    return "и" if cyr >= lat else "y"


def repair_text(text: str):
    """Вернуть (новый_текст, число_замен, отсеянные_контексты)."""
    conj = _conjunction_for(text)
    skipped = []
    count = 0

    def _sub(m):
        nonlocal count
        prev, sp1, sp2, nxt = m.group(1), m.group(2), m.group(3), m.group(4)
        if nxt.lower() in _REAL_ZERO_NEXT or prev.lower() in _REAL_ZERO_PREV:
            skipped.append(f"{prev} 0 {nxt}")
            return m.group(0)
        count += 1
        return f"{prev}{sp1}{conj}{sp2}{nxt}"

    return _PATTERN.sub(_sub, text), count, skipped


def _backend_busy(data_dir: pathlib.Path) -> str | None:
    """Активная запись/встреча → чинить нельзя: backend допишет свою строку.

    Недоступный сокет ошибкой НЕ считаем: backend может быть просто не запущен,
    и тогда конкурента за файл нет вовсе.
    """
    import socket
    sock_path = data_dir / "krabear.sock"
    if not sock_path.exists():
        return None
    for method, key in (("get_recording_state", "is_recording"),
                        ("get_meeting_live_state", "active")):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect(str(sock_path))
            s.sendall(json.dumps({"id": "repair", "method": method, "params": {}}).encode() + b"\n")
            resp = json.loads(s.recv(65536).decode())
            s.close()
            if (resp.get("result") or {}).get(key):
                return method
        except (OSError, ValueError):
            return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", default=os.path.expanduser("~/Library/Application Support/KrabEar"))
    ap.add_argument("--apply", action="store_true", help="без него — только сухой прогон")
    ap.add_argument("--show", type=int, default=8, help="сколько примеров показать")
    args = ap.parse_args()

    data_dir = pathlib.Path(args.data_dir)
    hist = data_dir / "history.ndjson"
    if not hist.exists():
        print(f"нет файла истории: {hist}", file=sys.stderr)
        return 2

    busy = _backend_busy(data_dir)
    if busy and args.apply:
        print(f"ОТКАЗ: идёт запись/встреча ({busy}) — backend допишет строку поверх правки.")
        return 3

    lock_path = data_dir / "history.lock"
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        repaired_lines, total, changed, replacements = [], 0, 0, 0
        samples, skipped_samples, per_item = [], [], []
        for raw in hist.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            total += 1
            try:
                item = json.loads(raw)
            except ValueError:
                repaired_lines.append(raw)
                continue
            text = item.get("text")
            if not isinstance(text, str) or " 0 " not in text:
                repaired_lines.append(raw)
                continue
            new_text, n, skipped = repair_text(text)
            skipped_samples.extend(skipped)
            if n:
                changed += 1
                replacements += n
                if len(samples) < args.show:
                    # 🔴 Окно вокруг ПЕРВОГО РАСХОЖДЕНИЯ, а не вокруг первого
                    # « 0 »: в длинной диктовке нетронутый ноль часто стоит
                    # раньше правки, и пара «было/стало» выглядела одинаково —
                    # сухой прогон переставал показывать то, ради чего сделан.
                    idx = next((i for i, (a, b) in enumerate(zip(text, new_text)) if a != b), 0)
                    samples.append((text[max(0, idx - 45):idx + 45], new_text[max(0, idx - 45):idx + 45]))
                per_item.append((n, item.get("id", "?")))
                item["text"] = new_text
                repaired_lines.append(json.dumps(item, ensure_ascii=False))
            else:
                repaired_lines.append(raw)

        print(f"строк всего: {total}")
        print(f"записей с восстановленным союзом: {changed} (замен: {replacements})")
        print(f"отсеяно как настоящий ноль: {len(skipped_samples)}")
        if per_item:
            top = sorted(per_item, reverse=True)[:3]
            print("самые правленые записи: " + ", ".join(f"{n} замен" for n, _ in top))
        for ctx in sorted(set(skipped_samples))[:args.show]:
            print(f"    оставлено: …{ctx}…")
        print()
        for before, after in samples:
            print(f"  было:  …{before}…")
            print(f"  стало: …{after}…")
            print()

        if not args.apply:
            print("СУХОЙ ПРОГОН — файл не тронут. Для применения: --apply")
            return 0
        if not changed:
            print("чинить нечего.")
            return 0

        backup = hist.with_suffix(f".ndjson.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(hist, backup)
        fd, tmp = tempfile.mkstemp(dir=str(data_dir), prefix=".history-repair-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(repaired_lines) + "\n")
        shutil.copystat(hist, tmp)
        os.replace(tmp, hist)
        print(f"ПРИМЕНЕНО. Бэкап: {backup.name}")
        return 0
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
