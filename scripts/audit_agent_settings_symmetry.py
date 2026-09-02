#!/usr/bin/env python3
"""Гард односторонней настройки: ключ, который панель читает, но не умеет записать.

ЗАЧЕМ
-----
Настройки агента ездят через один Swift-тип — ``AgentSettings`` в
``native/KrabEarAgent/Sources/KrabEarAgent/Models.swift``. У него две половины,
и они обязаны быть зеркальными:

  * ``init(from payload:)`` — разбирает ответ backend'а (``get_settings`` и эхо
    ``set_settings``);
  * ``toPayload()`` — собирает словарь, который панель отправляет обратно.

Панель сохраняет настройки ТОЛЬКО через ``applySettingsPatch``, а тот берёт
базу из ``toPayload()``. Значит ключ, который есть в ``init``, но отсутствует в
``toPayload()``, панель показать может, а изменить — уже нет: он существует в
модели, но не выезжает наружу ни при одном сохранении.

ЖИВОЙ СЛУЧАЙ (2026-09-02, #1975)
--------------------------------
``overlay_follow_cursor`` («оверлей следует за курсором») был добавлен в
``init(from:)`` и прочитан оверлеем, но в ``toPayload()`` не попал. Тесты
зелёные, поведение работает — и всё же владелец спросил «а где я могу это
включить?»: переключателя не было и появиться не могло, потому что сохранить
значение панель физически не умела. Настройка, включаемая только сырым
IPC-вызовом из терминала, для владельца равна отсутствующей.

Обратное направление — ключ в ``toPayload()`` без разбора в ``init`` — тоже
находка, и хуже: панель отправляет значение, но в эхе ответа его не читает,
поэтому локальная модель после каждого round-trip откатывается к дефолту.
Контрол в такой паре ведёт себя как «щёлкнул — вернулось обратно».

КРИТЕРИЙ
--------
Множество ключей ``payload["..."]`` внутри ``init(from payload:)`` обязано
совпадать с множеством строковых ключей словаря, возвращаемого ``toPayload()``.
Обе половины ищутся строго внутри ``struct AgentSettings`` — соседние типы
файла (у них свои payload-и) не участвуют.

Осознанно односторонние ключи перечисляются в ALLOWLIST с причиной; пустой
список — норма, каждая запись требует объяснения, почему настройку нельзя
сохранить из панели.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_SWIFT = REPO_ROOT / "native/KrabEarAgent/Sources/KrabEarAgent/Models.swift"

# Ключи, для которых односторонность — сознательное решение.
# Формат: "ключ": "почему панель не должна его отправлять".
ALLOWLIST: dict[str, str] = {}

_STRUCT_RE = re.compile(r"^\s*(?:public\s+)?struct\s+AgentSettings\b")
_INIT_RE = re.compile(r"^\s*init\(from payload:")
_TOPAYLOAD_RE = re.compile(r"^\s*func toPayload\(\)")
_READ_KEY_RE = re.compile(r'payload\[\s*"([a-z_0-9]+)"\s*\]')
_WRITE_KEY_RE = re.compile(r'^\s*"([a-z_0-9]+)"\s*:')


def _struct_bounds(lines: list[str]) -> tuple[int, int]:
    """Границы тела ``struct AgentSettings`` по отступу закрывающей скобки."""
    for i, line in enumerate(lines):
        if _STRUCT_RE.match(line):
            indent = len(line) - len(line.lstrip())
            close = " " * indent + "}"
            for j in range(i + 1, len(lines)):
                if lines[j].rstrip() == close:
                    return i, j
            return i, len(lines)
    raise LookupError("struct AgentSettings не найден в Models.swift")


def _member_bounds(lines: list[str], start: int, end: int, head: re.Pattern[str]) -> tuple[int, int]:
    """Границы метода структуры: от заголовка до закрывающей скобки его отступа."""
    for i in range(start, end):
        if head.match(lines[i]):
            indent = len(lines[i]) - len(lines[i].lstrip())
            close = " " * indent + "}"
            for j in range(i + 1, end):
                if lines[j].rstrip() == close:
                    return i, j
            return i, end
    raise LookupError(f"не найден член по образцу {head.pattern!r}")


def collect_keys(source: str) -> tuple[set[str], set[str], dict[str, int]]:
    """Возвращает (читаемые ключи, записываемые ключи, ключ → строка объявления)."""
    lines = source.splitlines()
    s_start, s_end = _struct_bounds(lines)
    i_start, i_end = _member_bounds(lines, s_start, s_end, _INIT_RE)
    p_start, p_end = _member_bounds(lines, s_start, s_end, _TOPAYLOAD_RE)

    read: set[str] = set()
    where: dict[str, int] = {}
    for n in range(i_start, i_end):
        for key in _READ_KEY_RE.findall(lines[n]):
            read.add(key)
            where.setdefault(key, n + 1)

    written: set[str] = set()
    for n in range(p_start, p_end):
        m = _WRITE_KEY_RE.match(lines[n])
        if m:
            written.add(m.group(1))
            where.setdefault(m.group(1), n + 1)
    return read, written, where


def audit(source: str) -> list[str]:
    read, written, where = collect_keys(source)
    findings: list[str] = []
    for key in sorted(read - written - set(ALLOWLIST)):
        findings.append(
            f"Models.swift:{where.get(key, 0)}: '{key}' разбирается в init(from:), "
            f"но отсутствует в toPayload() — панель может показать значение, но не сохранить его"
        )
    for key in sorted(written - read - set(ALLOWLIST)):
        findings.append(
            f"Models.swift:{where.get(key, 0)}: '{key}' отправляется в toPayload(), "
            f"но не читается в init(from:) — эхо backend'а сбросит его к дефолту"
        )
    return findings


_GOOD_SAMPLE = '''
struct AgentSettings {
    init(from payload: [String: Any]) {
        self.mode = (payload["mode"] as? String) ?? Self.default.mode
        self.follow = (payload["overlay_follow_cursor"] as? Bool) ?? false
    }
    func toPayload() -> [String: Any] {
        [
            "mode": mode,
            "overlay_follow_cursor": follow,
        ]
    }
}
'''

_BAD_READ_ONLY = '''
struct AgentSettings {
    init(from payload: [String: Any]) {
        self.mode = (payload["mode"] as? String) ?? Self.default.mode
        self.follow = (payload["overlay_follow_cursor"] as? Bool) ?? false
    }
    func toPayload() -> [String: Any] {
        [
            "mode": mode,
        ]
    }
}
'''

_BAD_WRITE_ONLY = '''
struct AgentSettings {
    init(from payload: [String: Any]) {
        self.mode = (payload["mode"] as? String) ?? Self.default.mode
    }
    func toPayload() -> [String: Any] {
        [
            "mode": mode,
            "overlay_follow_cursor": follow,
        ]
    }
}
'''


def selftest() -> int:
    cases = [
        ("зеркальная пара", _GOOD_SAMPLE, 0),
        ("читается, не отправляется", _BAD_READ_ONLY, 1),
        ("отправляется, не читается", _BAD_WRITE_ONLY, 1),
    ]
    failures = 0
    for name, sample, expected in cases:
        got = len(audit(sample))
        ok = got == expected
        print(f"  [{'ok' if ok else 'ПРОВАЛ'}] {name}: находок {got}, ожидалось {expected}")
        if not ok:
            failures += 1
    if failures:
        print(f"selftest: {failures} провал(ов) — классификатор сломан", file=sys.stderr)
        return 1
    print("selftest: классификатор различает зеркальную пару и обе односторонние")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fail-on-found", action="store_true",
                        help="ненулевой код возврата при находках (режим CI)")
    parser.add_argument("--selftest", action="store_true",
                        help="прогнать классификатор на заведомо плохих/хороших образцах")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    if not MODELS_SWIFT.exists():
        print(f"не найден {MODELS_SWIFT}", file=sys.stderr)
        return 2

    findings = audit(MODELS_SWIFT.read_text(encoding="utf-8", errors="replace"))
    if not findings:
        print("CLEAN: init(from:) и toPayload() покрывают один и тот же набор ключей")
        return 0

    print(f"НАЙДЕНО {len(findings)} односторонних ключ(ей):")
    for line in findings:
        print(f"  {line}")
    print("\nПочинить: добавить ключ в обе половины AgentSettings "
          "(и контрол в панель, иначе настройка остаётся недоступной владельцу).")
    return 1 if args.fail_on_found else 0


if __name__ == "__main__":
    raise SystemExit(main())
