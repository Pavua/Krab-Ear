"""G3-privacy-04 P04-11: аудит in-RAM purge засчитывает только ДОСТИЖИМЫЙ clear().

Раньше ``_collect_clear_calls`` шёл ``ast.walk`` по всему телу
``handle_purge_all_data`` и засчитывал любой ``self.<attr>.clear()``: в
неиспользуемой вложенной функции, в lambda, после безусловного ``return``/``raise``
или в ветке ``if False``. Гард рапортовал «покрыто», хотя в рантайме purge
этот clear не выполняет — stale PII остаётся в памяти. Теперь такие вызовы
не считаются.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "audit_inmemory_purge_coverage.py"


def _load():
    spec = importlib.util.spec_from_file_location("audit_inmemory_purge_coverage", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _load()
PROBE = ("_w", "clear")


def _cleared(body: str, extra: str = "") -> set:
    indented = "\n".join("        " + line for line in body.strip("\n").splitlines())
    src = (
        "class HistoryService:\n"
        "    def handle_purge_all_data(self, params):\n"
        f"{indented}\n"
        f"{extra}"
    )
    return MOD._cleared_in_source(src)


@pytest.mark.parametrize(
    "body",
    [
        "self._w.clear()",
        "if self._w is not None:\n    self._w.clear()",
        "try:\n    self._w.clear()\nexcept Exception:\n    pass",
        "try:\n    pass\nfinally:\n    self._w.clear()",
        "for _ in range(1):\n    self._w.clear()",
        "with self._lock:\n    self._w.clear()",
        "if True:\n    self._w.clear()",
        "if False:\n    pass\nelse:\n    self._w.clear()",
        "x = [self._w.clear() for _ in range(1)]",
        "try:\n    raise ValueError\nexcept ValueError:\n    self._w.clear()",
        "if self._x:\n    return {}\nself._w.clear()",
        "if self._x:\n    return {}\nelse:\n    pass\nself._w.clear()",
        "try:\n    return {}\nexcept ValueError:\n    pass\nself._w.clear()",
        "try:\n    self._a()\nexcept Exception:\n    return {}\nself._w.clear()",
        "try:\n    return {}\nfinally:\n    self._w.clear()",
        "try:\n    self._a()\nexcept Exception:\n    pass\nelse:\n    self._w.clear()",
        "for _ in self._xs:\n    return {}\nself._w.clear()",
        "if not False:\n    self._w.clear()",
        "with suppress(ValueError):\n    raise ValueError()\nself._w.clear()",
        "with self._lk:\n    raise ValueError()\nself._w.clear()",
        "with self._lk:\n    if self._x:\n        return {}\n    else:\n        raise ValueError()\nself._w.clear()",
        "try:\n    with suppress(ValueError):\n        raise ValueError()\nexcept Exception:\n    return {}\nelse:\n    self._w.clear()",
    ],
)
def test_reachable_clear_is_counted(body: str) -> None:
    assert PROBE in _cleared(body)


@pytest.mark.parametrize(
    "body",
    [
        "def _never():\n    self._w.clear()",
        "async def _never():\n    self._w.clear()",
        "cb = lambda: self._w.clear()",
        "return {}\nself._w.clear()",
        "raise RuntimeError('x')\nself._w.clear()",
        "if False:\n    self._w.clear()",
        "if 0:\n    self._w.clear()",
        "if True:\n    pass\nelse:\n    self._w.clear()",
        "while False:\n    self._w.clear()",
        "class _K:\n    def m(self):\n        self._w.clear()",
        "for _ in range(2):\n    break\n    self._w.clear()",
        "for _ in range(2):\n    continue\n    self._w.clear()",
        "while self._flag:\n    if self._x:\n        break\n        self._w.clear()",
        "if self._x:\n    return {}\nelse:\n    return {}\nself._w.clear()",
        "try:\n    return {}\nexcept Exception:\n    return {}\nself._w.clear()",
        "with self._lk:\n    return {}\nself._w.clear()",
        "try:\n    return {}\nexcept ValueError:\n    pass\nelse:\n    self._w.clear()",
        "try:\n    pass\nfinally:\n    return {}\nself._w.clear()",
        "if not True:\n    self._w.clear()",
        "if not 0:\n    pass\nelse:\n    self._w.clear()",
    ],
)
def test_unreachable_clear_is_not_counted(body: str) -> None:
    assert PROBE not in _cleared(body)


def test_clear_after_return_inside_branch_is_not_counted() -> None:
    body = "if self._flag:\n    return {}\n    self._w.clear()"
    assert PROBE not in _cleared(body)


def test_helper_called_only_from_unreachable_code_is_not_expanded() -> None:
    body = "return {}\nself._helper()"
    extra = "    def _helper(self):\n        self._w.clear()\n"
    assert PROBE not in _cleared(body, extra)


def test_helper_called_from_reachable_code_is_expanded() -> None:
    body = "self._helper()"
    extra = "    def _helper(self):\n        self._w.clear()\n"
    assert PROBE in _cleared(body, extra)


def test_unreachable_clear_inside_helper_is_not_counted() -> None:
    body = "self._helper()"
    extra = "    def _helper(self):\n        return\n        self._w.clear()\n"
    assert PROBE not in _cleared(body, extra)


def test_real_purge_still_has_zero_gaps() -> None:
    """Страховка: ужесточение не должно породить ложных дыр на боевом коде."""
    result = MOD.run_audit()
    assert result.gaps == [], [g.call_repr for g in result.gaps]
