#!/usr/bin/env python3
"""Гард осиротевших контролов панели: объявлен, выглядит живым, одно звено не подключено.

ЗАЧЕМ
-----
В Swift-агенте панель собрана вручную из AppKit-контролов. Один и тот же класс
отказа уже случался дважды: объект объявлен, тесты зелёные, а в проде он мёртв,
потому что одно звено проводки отсутствует. Так ``setupErrorBus()`` месяцами
не вызывался, и весь показ ошибок пользователю не работал при 100% зелёном CI.

Для контрола панели звеньев ровно три, и провал любого делает его декоративным:

  1. объявление — stored-свойство класса ``NSButton`` / ``NSPopUpButton`` /
     ``NSSlider`` / ``NSStepper`` / ``NSTextField`` (и тематические наследники);
  2. проводка — контролу заданы ``target`` И ``action`` (``#selector``), либо
     он создан конструктором, который принимает оба;
  3. включение в иерархию — передан в ``addArrangedSubview`` / ``addSubview``
     или в конструктор строки (``cdMakeRow``, ``makeSwitchRow``,
     ``cdMakeSliderRow``, ``makeSettingRow``).

🔴 ИЗВЕСТНАЯ СЛЕПАЯ ЗОНА — ПОЭТОМУ СКРИПТ НЕ ПОДКЛЮЧЁН НИ В Makefile, НИ В CI
--------------------------------------------------------------------------
Замер мутацией 02.09.2026: у ``audioDeviceSelector`` снята проводка
(``target``/``action`` удалены) — гард по-прежнему отчитался CLEAN. Причина в
сужении критерия: для value-контрола звено 2 засчитывается, если значение
где-то читается, а чтение живёт внутри его же ``@objc``-обработчика. Стоит
обработчику остаться без ``action``, и он становится недостижим, но выглядит
как потребитель.

Сужение было правильным по своей задаче — оно срезало 11 ложных срабатываний
из 12, — но в этой форме гард не ловит ровно тот баг, ради которого написан.
До ужесточения (чтение внутри собственного обработчика контрола не считается
потреблением) он годится как разовый искатель, а не как страж: всегда зелёный
монитор хуже отсутствующего, потому что создаёт ложное чувство покрытия.

Свою работу как искатель он выполнил: нашёл живую дыру (пикер микрофона,
починен в этой же волне) при нуле ложных срабатываний на 154 кандидатах.

КРИТЕРИЙ (главное решение этого гарда)
---------------------------------------
Находкой считается **stored-свойство** интерактивного контрола, у которого
в теле своего типа (класс + extension'ы) отсутствует звено 2 ИЛИ звено 3.

Широкие критерии отвергнуты замером на живом дереве (12 находок,
11 ложных — гард с таким шумом не доживает до второго применения):

  * «любой ``NSTextField``, включая подписи» — десятки ``labelWithString``.
    Подпись не обязана иметь ``target``/``action``. Не кандидат.
  * «computed / associated object тоже кандидат» — секция держит локальный
    алиас (``let toggle = selectionTranslateToggle``), и поиск по имени
    свойства врёт «осиротел». Граница — stored-свойства.
  * «IUO/optional без инициализатора» (``var endChainButton: NSButton?``) —
    это слот под контрол, созданный локально и уже проведённый. Декларативный
    класс отказа — ``let fooButton = NSButton(...)``, а не дырка под чужой
    ``endBtn``.
  * «NSPopUpButton/чекбокс без ``action`` = мёртв» — ложь: значение часто
    читает соседняя кнопка (``titleOfSelectedItem``, ``.state == .on``).
    Звено 2 для value-контрола — action ИЛИ чтение значения. Командная
    кнопка по-прежнему обязана иметь target+action.
  * «``.target =`` только на самом имени» — ``configure(listenButton)``
    ставит target/action параметру. Без учёта хелпера три живые кнопки HUD
    выглядели осиротевшими.

``NSTextField`` без ``labelWithString`` (поле ввода) обязан быть в иерархии,
но не обязан иметь ``target``/``action``: значение часто читает соседняя
кнопка.

Swift AST в проекте недоступен, разбор регексовый. Проводка и иерархия
ищутся только в теле того типа, которому принадлежит свойство: иначе
одноимённый ``statusLabel`` чужого класса «спасал» бы чужую дыру.

Usage::

    python3 scripts/audit_orphan_panel_controls.py
    python3 scripts/audit_orphan_panel_controls.py --fail-on-found
    python3 scripts/audit_orphan_panel_controls.py --json
    python3 scripts/audit_orphan_panel_controls.py --selftest
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = (
    REPO_ROOT / "native" / "KrabEarAgent" / "Sources" / "KrabEarAgent"
)

# Интерактивные контролы панели. NSTextView / NSStackView / NSTabView сюда
# не входят: у них другой контракт (делегат, контейнер), не три звена кнопки.
CONTROL_TYPES = (
    "ThemePrimaryButton",
    "ThemeSecondaryButton",
    "ThemeButton",
    "NSPopUpButton",
    "NSSegmentedControl",
    "NSSearchField",
    "NSComboBox",
    "NSColorWell",
    "NSDatePicker",
    "NSStepper",
    "NSSlider",
    "NSSwitch",
    "NSButton",
    "NSTextField",
)

# Value-контролы (попап, слайдер, чекбокс) могут жить за счёт чтения значения.
# Командная кнопка (Theme*Button / обычный NSButton без checkbox) — нет.
_VALUE_KINDS = frozenset({
    "NSPopUpButton",
    "NSSlider",
    "NSStepper",
    "NSSegmentedControl",
    "NSComboBox",
    "NSSearchField",
    "NSSwitch",
    "NSColorWell",
    "NSDatePicker",
})
_VALUE_READ_ATTRS = (
    "state",
    "indexOfSelectedItem",
    "titleOfSelectedItem",
    "selectedItem",
    "doubleValue",
    "integerValue",
    "floatValue",
    "stringValue",
    "objectValue",
    "attributedStringValue",
)

_CONTROL_ALT = "|".join(CONTROL_TYPES)

_SKIP_PARTS = ("/Tests/", "/.build/", "/DerivedData/", "/.venv")

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

_TYPE_RE = re.compile(
    r"^\s*(?:(?:public|private|fileprivate|internal|open|final|@\S+)\s+)*"
    r"(?:class|struct|actor|extension)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.]*)"
)

_MODS = (
    r"(?:(?:private|fileprivate|internal|public|open|"
    r"nonisolated(?:\(\s*unsafe\s*\))?|lazy|weak|unowned(?:\([^)]*\))?|"
    r"static|override|final|mutating|async|@\S+)\s+)*"
)

_DECL_RE = re.compile(
    rf"^\s*(?P<mods>{_MODS})"
    rf"(?P<kind>let|var)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*"
    rf"(?::\s*(?P<type>{_CONTROL_ALT})(?P<opt>[?!])?)?"
    rf"(?P<rest>.*)$"
)

_RHS_TYPE_RE = re.compile(rf"(?:^|=)\s*(?:try\s+)?({_CONTROL_ALT})\s*(?:\.init)?\s*\(")

_LABEL_CTOR_RE = re.compile(
    r"\b(?:labelWithString|wrappingLabelWithString)\s*:"
)

_TARGET_NON_NIL_RE = re.compile(r"\btarget\s*:\s*(?!nil\b)")
_ACTION_SELECTOR_RE = re.compile(r"\baction\s*:\s*#selector\s*\(")

# Конструкторы строк панели. «control:» / «button:» / «slider:» — именованный
# аргумент, который кладёт контрол в иерархию, даже если addSubview нет рядом.
_HIERARCHY_CALLEES = (
    "addArrangedSubview",
    "insertArrangedSubview",
    "addSubview",
    "insertSubview",
    "cdMakeRow",
    "cdMakeSliderRow",
    "makeSwitchRow",
    "makeSettingRow",
)


@dataclass
class ControlDecl:
    file: str
    line: int
    name: str
    type_name: str
    control_kind: str
    init_text: str


@dataclass
class Finding:
    file: str
    line: int
    name: str
    type_name: str
    control_kind: str
    missing: list[str]

    def render(self) -> str:
        links = "; ".join(self.missing)
        return (
            f"{self.file}:{self.line}  {self.type_name}.{self.name} "
            f"({self.control_kind})\n"
            f"    не хватает: {links}"
        )

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "name": self.name,
            "type_name": self.type_name,
            "control_kind": self.control_kind,
            "missing": list(self.missing),
        }


@dataclass
class ScanResult:
    controls: list[ControlDecl] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def _strip_block_comments(text: str) -> str:
    """Вырезает /* … */, сохраняя переводы строк, чтобы номера строк не съехали."""
    return _BLOCK_COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def _strip_line_comment(line: str) -> str:
    in_string = False
    escape = False
    chars: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if in_string:
            chars.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            chars.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
            break
        chars.append(ch)
        i += 1
    return "".join(chars)


def _wipe_strings(line: str) -> str:
    """Заменяет содержимое "..." пробелами, чтобы скобки внутри строк не считались."""
    out: list[str] = []
    in_string = False
    escape = False
    for ch in line:
        if in_string:
            if escape:
                out.append(" ")
                escape = False
            elif ch == "\\":
                out.append(" ")
                escape = True
            elif ch == '"':
                out.append('"')
                in_string = False
            else:
                out.append(" " if ch != "\n" else "\n")
            continue
        if ch == '"':
            in_string = True
            out.append('"')
            continue
        out.append(ch)
    return "".join(out)


def _brace_delta(line: str) -> int:
    s = _wipe_strings(line)
    return s.count("{") - s.count("}")


def _ctor_wired(text: str) -> bool:
    return bool(_TARGET_NON_NIL_RE.search(text) and _ACTION_SELECTOR_RE.search(text))


def _balanced_slice(text: str, open_idx: int, open_ch: str, close_ch: str) -> str | None:
    depth = 0
    for i in range(open_idx, len(text)):
        ch = text[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[open_idx : i + 1]
    return None


def _capture_init_text(lines: list[str], start_idx: int, rest: str) -> str:
    """Текст инициализатора stored-свойства, начиная с '=' на строке объявления."""
    eq = rest.find("=")
    if eq < 0:
        return ""
    after = rest[eq:]
    combined = [after]
    # Однострочный инициализатор без скобок/замыкания.
    stripped = after.strip()
    if stripped != "=" and not stripped.endswith("{") and stripped.count("(") == stripped.count(")"):
        if not stripped.endswith("("):
            return after.strip()

    depth_brace = after.count("{") - after.count("}")
    depth_paren = _wipe_strings(after).count("(") - _wipe_strings(after).count(")")
    j = start_idx + 1
    # Замыкание `= { ... }()` или многострочный вызов конструктора.
    while j < len(lines) and (depth_brace > 0 or depth_paren > 0 or after.strip() in ("=", "= {")):
        nxt = _strip_line_comment(lines[j])
        combined.append(nxt)
        wiped = _wipe_strings(nxt)
        depth_brace += wiped.count("{") - wiped.count("}")
        depth_paren += wiped.count("(") - wiped.count(")")
        j += 1
        if j - start_idx > 80:
            break
        if depth_brace <= 0 and depth_paren <= 0 and "".join(combined).strip() not in ("=", "= {"):
            break
    return "\n".join(combined)


def _infer_kind(type_annot: str | None, init_text: str) -> str | None:
    if type_annot:
        return type_annot
    m = _RHS_TYPE_RE.search(init_text)
    if m:
        return m.group(1)
    m = _RHS_TYPE_RE.search("=" + init_text if init_text.startswith(" ") else init_text)
    if m:
        return m.group(1)
    return None


def _is_label_control(kind: str, init_text: str) -> bool:
    if kind != "NSTextField":
        return False
    return bool(_LABEL_CTOR_RE.search(init_text))


def _collect_from_sources(sources: dict[str, str]) -> tuple[list[ControlDecl], dict[str, str]]:
    """Возвращает объявления контролов и склеенные тела типов (класс + extension)."""
    decls: list[ControlDecl] = []
    bodies: dict[str, list[str]] = {}

    for path, raw in sources.items():
        text = _strip_block_comments(raw)
        lines = text.splitlines()
        depth = 0
        stack: list[tuple[str, int]] = []  # (type_name, body_depth)

        i = 0
        while i < len(lines):
            raw_line = lines[i]
            line = _strip_line_comment(raw_line)
            start_depth = depth

            while stack and start_depth < stack[-1][1]:
                stack.pop()

            type_m = _TYPE_RE.match(line)
            pending_type: str | None = None
            if type_m:
                pending_type = type_m.group("name")

            if pending_type is not None:
                # Тело начинается на этой же строке или на ближайшей '{'.
                rel = line[type_m.end():]
                if "{" in _wipe_strings(rel) or "{" in _wipe_strings(line):
                    # body_depth = depth after the opening brace of this type.
                    wiped_full = _wipe_strings(line)
                    # Глубина тела = текущая глубина + 1 после первой '{' этой декларации.
                    body_depth = start_depth + 1
                    stack.append((pending_type, body_depth))
                else:
                    # '{' на следующей строке — доберём на следующих итерациях через
                    # отдельный маркер. Для Swift-агента тип почти всегда с '{' на той же
                    # строке; этот хвост нужен selftest'у и редким переносам.
                    j = i + 1
                    while j < len(lines):
                        nxt = _strip_line_comment(lines[j])
                        if "{" in _wipe_strings(nxt):
                            stack.append((pending_type, start_depth + 1))
                            break
                        if nxt.strip():
                            break
                        j += 1

            if stack:
                bodies.setdefault(stack[-1][0], []).append(line)

            at_type_body = bool(stack) and start_depth == stack[-1][1]
            if at_type_body:
                decl_m = _DECL_RE.match(line)
                if decl_m:
                    mods = decl_m.group("mods") or ""
                    rest = decl_m.group("rest") or ""
                    name = decl_m.group("name")
                    kind_annot = decl_m.group("type")
                    # static/class/weak — не owned stored-контрол панели.
                    if re.search(r"\b(?:static|class|weak)\b", mods):
                        pass
                    else:
                        computed = "=" not in rest and "{" in rest
                        # IUO/optional без '=' — слот под чужой локальный контрол,
                        # не декларация «let foo = NSButton(...)».
                        slot = "=" not in rest and "{" not in rest
                        if not computed and not slot:
                            init_text = _capture_init_text(lines, i, rest)
                            control_kind = _infer_kind(kind_annot, init_text or rest)
                            if control_kind and not _is_label_control(control_kind, init_text):
                                decls.append(
                                    ControlDecl(
                                        file=path,
                                        line=i + 1,
                                        name=name,
                                        type_name=stack[-1][0],
                                        control_kind=control_kind,
                                        init_text=init_text,
                                    )
                                )

            depth += _brace_delta(line)
            if depth < 0:
                depth = 0
            i += 1

    joined = {
        name: _wipe_strings("\n".join(parts)) for name, parts in bodies.items()
    }
    return decls, joined


def _wiring_helpers(body: str) -> set[str]:
    """Методы типа, которые ставят target+action на свой первый параметр.

    Паттерн CallObserverHUD.configure(_ button: ThemeButton, …): кнопка
    передаётся в хелпер, а ``listenButton.target =`` в теле типа нет.
    """
    helpers: set[str] = set()
    for m in re.finditer(
        r"\bfunc\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*(?:_ )?([A-Za-z_][A-Za-z0-9_]*)\s*:",
        body,
    ):
        fname, param = m.group(1), m.group(2)
        brace = body.find("{", m.end())
        if brace < 0:
            continue
        fnbody = _balanced_slice(body, brace, "{", "}")
        if not fnbody:
            continue
        p = re.escape(param)
        if re.search(rf"\b{p}\.target\s*=\s*(?!nil\b)", fnbody) and re.search(
            rf"\b{p}\.action\s*=\s*#selector\s*\(", fnbody
        ):
            helpers.add(fname)
    return helpers


def _is_value_capable(decl: ControlDecl, body: str) -> bool:
    if decl.control_kind in _VALUE_KINDS:
        return True
    if decl.control_kind != "NSButton":
        return False
    if re.search(r"\b(?:checkboxWithTitle|radioButtonWithTitle)\s*:", decl.init_text):
        return True
    name = re.escape(decl.name)
    if re.search(
        rf"(?<![A-Za-z0-9_.])(?:self\.)?{name}\.setButtonType\(\s*\.(?:switch|radio|onOff)",
        body,
    ):
        return True
    return False


def _value_is_read(decl: ControlDecl, body: str) -> bool:
    name = re.escape(decl.name)
    ident = rf"(?:self\.)?{name}"
    attr = "|".join(_VALUE_READ_ATTRS)
    # ``foo.state = x`` — запись при синхронизации UI, не потребление.
    # ``foo.state == .on`` — потребление; ``==`` нельзя путать с присвоением.
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_.]){ident}\.(?:{attr})\b(?!\s*=(?!=))",
            body,
        )
    )


def _has_wiring(decl: ControlDecl, body: str, helpers: set[str]) -> bool:
    if _ctor_wired(decl.init_text):
        return True
    name = re.escape(decl.name)
    ident = rf"(?:self\.)?{name}"
    has_target = re.search(rf"(?<![A-Za-z0-9_.]){ident}\.target\s*=\s*(?!nil\b)", body)
    has_action = re.search(
        rf"(?<![A-Za-z0-9_.]){ident}\.action\s*=\s*#selector\s*\(", body
    )
    if has_target and has_action:
        return True

    for helper in helpers:
        if re.search(rf"\b{re.escape(helper)}\(\s*{ident}\s*[,)]", body):
            return True

    if _is_value_capable(decl, body) and _value_is_read(decl, body):
        return True

    # Локальный тёзк, затем self.name = local — паттерн tabSelector:
    # let tabSelector = NSSegmentedControl(..., target: self, action: #selector).
    assign_re = re.compile(
        rf"(?<![A-Za-z0-9_])(?:(?:let|var)\s+)?(?:self\.)?{name}\s*=\s*"
    )
    for m in assign_re.finditer(body):
        prefix = body[max(0, m.start() - 8) : m.start()]
        if prefix.endswith("."):
            continue
        rhs = body[m.end() :]
        paren = rhs.find("(")
        if paren < 0 or paren > 80:
            continue
        head = rhs[:paren]
        if "." in head.strip():
            continue
        call = _balanced_slice(rhs, paren, "(", ")")
        if call and _ctor_wired(head + call):
            return True
    return False


def _has_hierarchy(decl: ControlDecl, body: str) -> bool:
    name = re.escape(decl.name)
    ident = rf"(?:self\.)?{name}"
    boundary = rf"(?<![A-Za-z0-9_]){ident}(?![A-Za-z0-9_])"
    flat = re.sub(r"\s+", " ", body)

    for callee in _HIERARCHY_CALLEES:
        if re.search(rf"\b{callee}\(\s*{boundary}", flat):
            return True

    if re.search(rf"\b(?:control|button|slider|field)\s*:\s*{boundary}", flat):
        return True

    for m in re.finditer(r"\bviews\s*:\s*\[", body):
        arr = _balanced_slice(body, m.end() - 1, "[", "]")
        if arr and re.search(boundary, arr):
            return True
    return False


def classify_controls(
    decls: list[ControlDecl], bodies: dict[str, str]
) -> list[Finding]:
    findings: list[Finding] = []
    helpers_by_type = {t: _wiring_helpers(b) for t, b in bodies.items()}
    for decl in decls:
        body = bodies.get(decl.type_name, "")
        helpers = helpers_by_type.get(decl.type_name, set())
        missing: list[str] = []
        # Поле ввода (NSTextField) не обязано иметь action — см. шапку.
        needs_link2 = decl.control_kind != "NSTextField"
        if needs_link2 and not _has_wiring(decl, body, helpers):
            missing.append("проводка (target и action)")
        if not _has_hierarchy(decl, body):
            missing.append("включение в иерархию")
        if missing:
            findings.append(
                Finding(
                    file=decl.file,
                    line=decl.line,
                    name=decl.name,
                    type_name=decl.type_name,
                    control_kind=decl.control_kind,
                    missing=missing,
                )
            )
    return findings


def scan_sources(sources: dict[str, str]) -> ScanResult:
    decls, bodies = _collect_from_sources(sources)
    return ScanResult(controls=decls, findings=classify_controls(decls, bodies))


def _is_skipped(path: Path) -> bool:
    text = str(path)
    return any(part in text for part in _SKIP_PARTS)


def read_sources(root: Path) -> dict[str, str]:
    sources: dict[str, str] = {}
    if root.is_file() and root.suffix == ".swift":
        rel = str(root)
        sources[rel] = root.read_text(encoding="utf-8", errors="replace")
        return sources
    for swift in sorted(root.rglob("*.swift")):
        if _is_skipped(swift):
            continue
        try:
            rel = str(swift.relative_to(REPO_ROOT))
        except ValueError:
            rel = str(swift)
        sources[rel] = swift.read_text(encoding="utf-8", errors="replace")
    return sources


# ---------------------------------------------------------------------------
# Selftest: заведомо плохие и заведомо хорошие образцы через ТОТ ЖЕ детектор.
# ---------------------------------------------------------------------------

_BAD_SAMPLES: dict[str, tuple[dict[str, str], str, str]] = {
    "нет ни проводки, ни иерархии": (
        {
            "A.swift": (
                "class Panel {\n"
                "    let orphanButton = NSButton(title: \"X\", target: nil, action: nil)\n"
                "}\n"
            )
        },
        "orphanButton",
        "both",
    ),
    "проводка есть, иерархии нет": (
        {
            "A.swift": (
                "class Panel {\n"
                "    let ghostButton = NSButton(title: \"X\", target: nil, action: nil)\n"
                "    func setup() {\n"
                "        ghostButton.target = self\n"
                "        ghostButton.action = #selector(tap)\n"
                "    }\n"
                "}\n"
            )
        },
        "ghostButton",
        "hierarchy",
    ),
    "иерархия есть, проводки нет": (
        {
            "A.swift": (
                "class Panel {\n"
                "    let muteButton = NSButton(checkboxWithTitle: \"M\", target: nil, action: nil)\n"
                "    func setup() {\n"
                "        stack.addArrangedSubview(muteButton)\n"
                "    }\n"
                "}\n"
            )
        },
        "muteButton",
        "wiring",
    ),
    "хелпер строки без action": (
        {
            "A.swift": (
                "class Panel {\n"
                "    let volumeSlider = NSSlider(value: 1, minValue: 0, maxValue: 2, target: nil, action: nil)\n"
                "    func setup() {\n"
                "        let row = cdMakeSliderRow(label: \"Vol\", slider: volumeSlider, valueLabel: lab)\n"
                "    }\n"
                "}\n"
            )
        },
        "volumeSlider",
        "wiring",
    ),
    "попап в иерархии, значение никто не читает": (
        {
            "A.swift": (
                "class Panel {\n"
                "    let audioDeviceSelector = NSPopUpButton(frame: .zero, pullsDown: false)\n"
                "    func setup() {\n"
                "        audioDeviceSelector.addItem(withTitle: \"Default\")\n"
                "        row.addArrangedSubview(audioDeviceSelector)\n"
                "    }\n"
                "}\n"
            )
        },
        "audioDeviceSelector",
        "wiring",
    ),
}

_GOOD_SAMPLES: dict[str, tuple[dict[str, str], str]] = {
    "три звена через присвоение": (
        {
            "A.swift": (
                "class Panel {\n"
                "    let saveButton = ThemeSecondaryButton(title: \"Save\", target: nil, action: nil)\n"
                "    func setup() {\n"
                "        saveButton.target = self\n"
                "        saveButton.action = #selector(onSave)\n"
                "        bar.addArrangedSubview(saveButton)\n"
                "    }\n"
                "}\n"
            )
        },
        "saveButton",
    ),
    "конструктор с target/action и addSubview": (
        {
            "A.swift": (
                "class Panel {\n"
                "    let goButton = NSButton(title: \"Go\", target: self, action: #selector(onGo))\n"
                "    func setup() {\n"
                "        view.addSubview(goButton)\n"
                "    }\n"
                "}\n"
            )
        },
        "goButton",
    ),
    "makeSwitchRow": (
        {
            "A.swift": (
                "class Panel {\n"
                "    let flagButton = NSButton(checkboxWithTitle: \"\", target: nil, action: nil)\n"
                "    func setup() {\n"
                "        flagButton.target = self\n"
                "        flagButton.action = #selector(onFlag)\n"
                "        let row = makeSwitchRow(label: \"Flag\", button: flagButton)\n"
                "    }\n"
                "}\n"
            )
        },
        "flagButton",
    ),
    "cdMakeRow": (
        {
            "A.swift": (
                "class Panel {\n"
                "    let modeSelector = NSPopUpButton(frame: .zero, pullsDown: false)\n"
                "    func setup() {\n"
                "        modeSelector.target = self\n"
                "        modeSelector.action = #selector(onMode)\n"
                "        let row = cdMakeRow(label: \"Mode\", control: modeSelector)\n"
                "    }\n"
                "}\n"
            )
        },
        "modeSelector",
    ),
    "подпись не кандидат": (
        {
            "A.swift": (
                "class Panel {\n"
                "    let statusLabel = NSTextField(labelWithString: \"Hi\")\n"
                "}\n"
            )
        },
        "statusLabel",
    ),
    "локальная кнопка в методе не кандидат": (
        {
            "A.swift": (
                "class Panel {\n"
                "    func build() {\n"
                "        let tmpButton = NSButton(title: \"Tmp\", target: nil, action: nil)\n"
                "    }\n"
                "}\n"
            )
        },
        "tmpButton",
    ),
    "поле ввода без action, но в иерархии": (
        {
            "A.swift": (
                "class Panel {\n"
                "    let nameField = NSTextField(frame: .zero)\n"
                "    func setup() {\n"
                "        stack.addArrangedSubview(nameField)\n"
                "    }\n"
                "}\n"
            )
        },
        "nameField",
    ),
    "computed-свойство не кандидат": (
        {
            "A.swift": (
                "class Panel {\n"
                "    var extraButton: NSButton {\n"
                "        NSButton(title: \"E\", target: nil, action: nil)\n"
                "    }\n"
                "}\n"
            )
        },
        "extraButton",
    ),
    "проводка в extension другого файла": (
        {
            "A.swift": (
                "class Panel {\n"
                "    let helpButton = ThemeSecondaryButton(title: \"Help\", target: nil, action: nil)\n"
                "}\n"
            ),
            "A+Help.swift": (
                "extension Panel {\n"
                "    func wire() {\n"
                "        helpButton.target = self\n"
                "        helpButton.action = #selector(onHelp)\n"
                "        row.addArrangedSubview(helpButton)\n"
                "    }\n"
                "}\n"
            ),
        },
        "helpButton",
    ),
    "локальный тёзк с конструктором target/action": (
        {
            "A.swift": (
                "class Panel {\n"
                "    var tabSelector: NSSegmentedControl!\n"
                "    func build() {\n"
                "        let tabSelector = NSSegmentedControl(labels: [\"A\", \"B\"], "
                "trackingMode: .selectOne, target: self, action: #selector(onTab))\n"
                "        self.tabSelector = tabSelector\n"
                "        stack.addArrangedSubview(tabSelector)\n"
                "    }\n"
                "}\n"
            )
        },
        "tabSelector",
    ),
    "попап как value-пикер без action": (
        {
            "A.swift": (
                "class Panel {\n"
                "    let profilePresetSelector = NSPopUpButton(frame: .zero, pullsDown: false)\n"
                "    func apply() {\n"
                "        row.addArrangedSubview(profilePresetSelector)\n"
                "        let name = profilePresetSelector.titleOfSelectedItem ?? \"\"\n"
                "    }\n"
                "}\n"
            )
        },
        "profilePresetSelector",
    ),
    "чекбокс, значение читают при Submit": (
        {
            "A.swift": (
                "class Panel {\n"
                "    let autostartCheckbox = NSButton(checkboxWithTitle: \"Auto\", target: nil, action: nil)\n"
                "    func finish() {\n"
                "        stack.addArrangedSubview(autostartCheckbox)\n"
                "        let on = autostartCheckbox.state == .on\n"
                "    }\n"
                "}\n"
            )
        },
        "autostartCheckbox",
    ),
    "проводка через хелпер-алиас": (
        {
            "A.swift": (
                "class HUD {\n"
                "    private let listenButton = ThemeButton()\n"
                "    func build() {\n"
                "        configure(listenButton, symbol: \"speaker\") { }\n"
                "        let buttons = NSStackView(views: [listenButton])\n"
                "    }\n"
                "    func configure(_ button: ThemeButton, symbol: String, action: @escaping () -> Void) {\n"
                "        button.target = self\n"
                "        button.action = #selector(self.tapped(_:))\n"
                "    }\n"
                "}\n"
            )
        },
        "listenButton",
    ),
    "optional-слот не кандидат": (
        {
            "A.swift": (
                "class Detail {\n"
                "    private var endChainButton: NSButton?\n"
                "    func loadView() {\n"
                "        let endBtn = ThemePrimaryButton(title: \"End\", target: self, action: #selector(onEnd))\n"
                "        row.addArrangedSubview(endBtn)\n"
                "        self.endChainButton = endBtn\n"
                "    }\n"
                "}\n"
            )
        },
        "endChainButton",
    ),
}


def selftest() -> int:
    """Классификатор обязан ловить известно-плохое и молчать на известно-хорошем.

    Гард, тихо переставший различать, отчитывался бы CLEAN вечно — то же самое
    правило, что у соседних аудитов репозитория.
    """
    failures: list[str] = []

    for label, (src, name, kind) in _BAD_SAMPLES.items():
        result = scan_sources(src)
        hits = [f for f in result.findings if f.name == name]
        if not hits:
            failures.append(f"известно-плохой «{label}»: находки нет")
            continue
        missing = hits[0].missing
        if kind == "both" and len(missing) < 2:
            failures.append(
                f"известно-плохой «{label}»: ждали оба звена, получили {missing}"
            )
        elif kind == "wiring" and not any("проводка" in m for m in missing):
            failures.append(
                f"известно-плохой «{label}»: ждали дыру в проводке, получили {missing}"
            )
        elif kind == "hierarchy" and not any("иерархию" in m for m in missing):
            failures.append(
                f"известно-плохой «{label}»: ждали дыру в иерархии, получили {missing}"
            )

    for label, (src, name) in _GOOD_SAMPLES.items():
        result = scan_sources(src)
        hits = [f for f in result.findings if f.name == name]
        # Для «не кандидат» свойства может не быть и в controls.
        if hits:
            failures.append(
                f"ложное срабатывание на «{label}»: {hits[0].missing}"
            )

    if failures:
        print("SELFTEST FAILED:")
        for item in failures:
            print("  -", item)
        return 1
    print(
        f"SELFTEST OK: {len(_BAD_SAMPLES)} плохих образцов пойманы, "
        f"{len(_GOOD_SAMPLES)} хороших — чисто"
    )
    return 0


def format_report(result: ScanResult) -> str:
    lines = [
        "=" * 78,
        "AUDIT ORPHAN PANEL CONTROLS",
        "=" * 78,
        (
            f"Контролов-кандидатов: {len(result.controls)}. "
            f"Находок: {len(result.findings)}."
        ),
    ]
    if not result.findings:
        lines.append("CLEAN — осиротевших контролов панели не найдено.")
        return "\n".join(lines)
    lines.append("")
    for finding in result.findings:
        lines.append(finding.render())
        lines.append("")
    lines.append(
        "Каждая находка — объявленный контрол без проводки и/или без места "
        "в иерархии видов. Провал любого звена делает контрол декоративным."
    )
    return "\n".join(lines)


def format_json(result: ScanResult) -> str:
    return json.dumps(
        {
            "controls_total": len(result.controls),
            "count": len(result.findings),
            "findings": [f.as_dict() for f in result.findings],
        },
        ensure_ascii=False,
        indent=2,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--root",
        default=str(DEFAULT_ROOT),
        help="каталог Swift-исходников для скана",
    )
    parser.add_argument(
        "--fail-on-found",
        action="store_true",
        help="ненулевой код, если есть находки",
    )
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="проверить классификатор на встроенных образцах",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    root = Path(args.root)
    if not root.exists():
        print(f"каталог не найден: {root}", file=sys.stderr)
        return 2

    result = scan_sources(read_sources(root))
    print(format_json(result) if args.json else format_report(result))
    if args.fail_on_found and result.findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
