"""Политика skip при сбое импорта в REST-тестах (общая для test_rest_*.py).

Skip разрешён ТОЛЬКО когда не нашёлся модуль, объявленный как optional-зависимость.
Всё остальное — сбой кода приложения (backend/core/contracts), пропавший обязательный пакет
из requirements.txt, неизвестный модуль, дрейф stub («cannot import name») — должно падать
громко: CI ставит полный requirements.txt, поэтому там это поломка окружения, а не повод
для «вечного skip» с зелёным прогоном (ChatGPT-ревью #2298).
"""

from __future__ import annotations

import re
from pathlib import Path

_REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"

# Корни пакетов самого приложения: их отсутствие — дефект кода/путей, не optional.
_APP_ROOTS = frozenset({"backend", "core", "contracts", "KrabEar"})

# Optional-зависимости, объявленные в requirements.txt закомментированными блоками
# («Установи вручную …»), плюс платформенные, которые на не-Apple раннере не ставятся.
# import-имя != имя дистрибутива (sentence-transformers -> sentence_transformers).
_DECLARED_OPTIONAL = frozenset({
    "gigaam",
    "openwakeword",
    "sentence_transformers",
    # Apple-only (Metal): на ubuntu-раннере не ставится, rest_server их не требует на импорте.
    "mlx",
    "mlx_whisper",
})

# Различия import-имени и имени дистрибутива для обязательных пакетов.
_IMPORT_NAME_ALIASES = {
    "pyannote.audio": "pyannote",
    "pytest-xdist": "xdist",
}


def _normalize(dist_name: str) -> str:
    low = dist_name.strip().lower()
    return _IMPORT_NAME_ALIASES.get(low, low.replace("-", "_"))


def required_import_roots(requirements_text: str | None = None) -> frozenset[str]:
    """Корни import-имён обязательных пакетов (активные строки requirements.txt).

    Строки с environment-маркером (`; sys_platform == "darwin"`) не считаются обязательными
    на любой платформе — они не гарантированы на ubuntu-раннере.
    """
    text = _REQUIREMENTS.read_text(encoding="utf-8") if requirements_text is None else requirements_text
    roots = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ";" in line or line.startswith("-"):
            continue
        m = re.match(r"[A-Za-z0-9_.\-]+", line)
        if m:
            roots.add(_normalize(m.group(0)))
    return frozenset(roots)


def classify_import_failure(exc: BaseException, requirements_text: str | None = None) -> tuple[str, str]:
    """Вернуть ('skip', причина) только для объявленной optional-зависимости, иначе ('fail', причина)."""
    if not isinstance(exc, ModuleNotFoundError):
        return "fail", f"не ModuleNotFoundError: {exc!r}"
    name = exc.name
    if not name:
        return "fail", f"ModuleNotFoundError без имени модуля: {exc!r}"
    root = name.split(".")[0]
    if root in _APP_ROOTS:
        return "fail", f"не импортируется модуль приложения {name!r}"
    # Явный optional раньше «обязательных»: mlx-whisper числится в requirements.txt, но
    # Apple-only и на ubuntu-раннере не ставится — это объявленное исключение.
    if root in _DECLARED_OPTIONAL:
        return "skip", f"optional-зависимость {root!r} не установлена"
    if root in required_import_roots(requirements_text):
        return "fail", f"обязательный пакет {root!r} из requirements.txt не установлен"
    return "fail", f"модуль {name!r} не объявлен ни как обязательный, ни как optional"
