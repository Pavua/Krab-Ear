"""PasteFormatter — умное форматирование вставляемого текста под целевое приложение.

Позволяет автоматически адаптировать транскрибированный текст под контекст:
Telegram — короткий стиль без точки; Notes — маркированный список с датой;
Email — формальный стиль с приветствием; code editor — обёртка в комментарий.

Кастомные форматтеры персистируются в {data_dir}/paste_formatters.json.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger("KrabEar.Core.PasteFormatter")

# Precompiled regex — разбиение на предложения по знакам конца (lookbehind)
_RE_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")

# ---------------------------------------------------------------------------
# Встроенные форматтеры (чистые функции)
# ---------------------------------------------------------------------------


_TELEGRAM_MAX_LENGTH = 4000  # Bot API hard limit is 4096; leave 96 chars headroom for affixes


def _fmt_telegram(text: str) -> str:
    """Telegram: без точки в конце, разбивка длинных предложений, лимит 4096 символов.

    Telegram Bot API отклоняет сообщения длиннее 4096 символов с ошибкой
    MESSAGE_TOO_LONG. Используем 4000 для небольшого запаса под аффиксы.
    """
    text = text.strip()
    # Убираем trailing period/точку
    if text.endswith("."):
        text = text[:-1]
    # Разбиваем на предложения, если текст длинный (>120 символов)
    if len(text) > 120:
        sentences = _RE_SENT_SPLIT.split(text)
        text = "\n".join(s.strip() for s in sentences if s.strip())
    # F1 fix: enforce Telegram 4096-char limit (Bot API hard limit)
    if len(text) > _TELEGRAM_MAX_LENGTH:
        truncated = text[:_TELEGRAM_MAX_LENGTH].rsplit(" ", 1)[0]
        text = truncated + "…"
    return text


def _fmt_notes(text: str) -> str:
    """Notes: заголовок с датой + маркированный список предложений."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    text = text.strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) <= 1:
        bullets = f"• {text}"
    else:
        bullets = "\n".join(f"• {s.strip()}" for s in sentences if s.strip())
    return f"[{ts}]\n{bullets}"


def _fmt_email(text: str) -> str:
    """Email: формальный стиль — приветствие, текст, подпись-заглушка."""
    text = text.strip()
    # Capitalize first letter
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    # Добавить точку в конце если нет терминальной пунктуации
    if text and text[-1] not in ".!?":
        text += "."
    return f"Здравствуйте,\n\n{text}\n\nС уважением"


def _fmt_code_editor(text: str) -> str:
    """Code editor: оборачивает текст в блочный комментарий, сохраняет отступы."""
    text = text.strip()
    lines = text.splitlines()
    commented = "\n".join(f"// {line}" if line.strip() else "//" for line in lines)
    return f"/*\n{commented}\n*/"


def _fmt_default(text: str) -> str:
    """Default: без изменений."""
    return text


# Реестр встроенных форматтеров: имя_приложения → callable
_BUILTIN_FORMATTERS: dict[str, Any] = {
    "telegram": _fmt_telegram,
    "notes": _fmt_notes,
    "email": _fmt_email,
    "code_editor": _fmt_code_editor,
    "default": _fmt_default,
}

# Метаданные встроенных форматтеров для list_formatters()
_BUILTIN_META: list[dict] = [
    {
        "name": "telegram",
        "label": "Telegram",
        "description": "Без точки в конце, разбивка длинных предложений на строки",
        "builtin": True,
    },
    {
        "name": "notes",
        "label": "Apple Notes",
        "description": "Заголовок с меткой времени и маркированный список",
        "builtin": True,
    },
    {
        "name": "email",
        "label": "Email",
        "description": "Формальный стиль с приветствием и подписью",
        "builtin": True,
    },
    {
        "name": "code_editor",
        "label": "Code Editor",
        "description": "Обёртка в блочный комментарий, сохранение отступов",
        "builtin": True,
    },
    {
        "name": "default",
        "label": "По умолчанию",
        "description": "Текст без изменений",
        "builtin": True,
    },
]

# ---------------------------------------------------------------------------
# Применение правил кастомного форматтера
# ---------------------------------------------------------------------------


def _apply_rules(text: str, rules: dict) -> str:
    """Применяет словарь правил к тексту.

    Поддерживаемые ключи:
        strip_trailing_period (bool) — убрать точку в конце
        capitalize (bool) — заглавная первая буква
        prepend (str) — добавить строку перед текстом
        append (str) — добавить строку после текста
        max_length (int) — обрезать до N символов (по словам)
        bullet_sentences (bool) — каждое предложение на новой строке с «•»
    """
    text = text.strip()

    if rules.get("capitalize") and text and text[0].islower():
        text = text[0].upper() + text[1:]

    if rules.get("strip_trailing_period") and text.endswith("."):
        text = text[:-1]

    if rules.get("bullet_sentences"):
        sentences = _RE_SENT_SPLIT.split(text)
        if len(sentences) > 1:
            text = "\n".join(f"• {s.strip()}" for s in sentences if s.strip())

    # F3 fix: apply prepend/append BEFORE max_length so the cap accounts for affixes
    if rules.get("prepend"):
        text = f"{rules['prepend']}\n{text}"

    if rules.get("append"):
        text = f"{text}\n{rules['append']}"

    # F4 fix: use explicit None + > 0 check so max_length=0 is honoured (returns empty)
    max_len = rules.get("max_length")
    if max_len is not None and isinstance(max_len, int) and max_len >= 0 and len(text) > max_len:
        if max_len == 0:
            text = ""
        else:
            words = text[:max_len].rsplit(" ", 1)
            text = words[0] + "…"

    return text


# ---------------------------------------------------------------------------
# PasteFormatter
# ---------------------------------------------------------------------------

class PasteFormatter:
    """Форматирует вставляемый текст в зависимости от целевого приложения.

    Встроенные форматтеры: telegram, notes, email, code_editor, default.
    Кастомные форматтеры хранятся в paste_formatters.json и применяются
    через словарь правил (_apply_rules).
    """

    _FILENAME = "paste_formatters.json"

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        # dict: app_name → rules dict (только кастомные)
        self._custom: dict[str, dict] = {}
        if data_dir is not None:
            self._path: Path | None = Path(data_dir) / self._FILENAME
            self._load()
        else:
            self._path = None

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Загружает кастомные форматтеры из файла."""
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._custom = {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except Exception as exc:
            _log.warning("Не удалось загрузить кастомные форматтеры: %s", exc)

    def _save(self) -> None:
        """Сохраняет кастомные форматтеры в файл атомарно.

        W24 (LOW, data-loss): direct write_text truncates the file first; a
        crash or disk-full mid-write would corrupt paste_formatters.json, and
        the next _load() would silently return an empty dict, discarding all
        custom formatters.  Atomic pattern: write → fsync → os.replace (rename).
        """
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            content = json.dumps(self._custom, ensure_ascii=False, indent=2)
            tmp = self._path.with_suffix(".json.tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(content)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self._path)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
        except Exception as exc:
            _log.warning("Не удалось сохранить кастомные форматтеры: %s", exc)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def format_for_app(self, text: str, app_name: str) -> str:
        """Форматирует text под целевое приложение app_name.

        Порядок приоритета:
        1. Кастомный форматтер (rules dict)
        2. Встроенный форматтер по точному имени
        3. Встроенный форматтер по частичному совпадению (case-insensitive)
        4. default-форматтер (as-is)
        """
        if not isinstance(text, str):
            text = str(text)
        normalized = (app_name or "").strip().lower()

        # 1. Кастомный форматтер
        with self._lock:
            custom_rules = self._custom.get(normalized)

        if custom_rules is not None:
            try:
                return _apply_rules(text, custom_rules)
            except Exception as exc:
                _log.warning("Ошибка кастомного форматтера '%s': %s", app_name, exc)
                return text

        # 2. Точное совпадение с встроенным форматтером
        builtin_fn = _BUILTIN_FORMATTERS.get(normalized)
        if builtin_fn is not None:
            try:
                return builtin_fn(text)
            except Exception as exc:
                _log.warning("Ошибка встроенного форматтера '%s': %s", app_name, exc)
                return text

        # 3. Частичное совпадение (например "Telegram Desktop" → telegram)
        for key, fn in _BUILTIN_FORMATTERS.items():
            if key == "default":
                continue
            if normalized and (key in normalized or normalized in key):
                try:
                    return fn(text)
                except Exception as exc:
                    _log.warning("Ошибка форматтера '%s' (partial match): %s", key, exc)
                    return text

        # 4. default
        return _fmt_default(text)

    def list_formatters(self) -> list[dict]:
        """Возвращает список всех форматтеров (встроенных + кастомных)."""
        result = list(_BUILTIN_META)
        with self._lock:
            for name, rules in self._custom.items():
                result.append({
                    "name": name,
                    "label": rules.get("label", name),
                    "description": rules.get("description", "Кастомный форматтер"),
                    "builtin": False,
                    "rules": rules,
                })
        return result

    def add_custom_formatter(self, app_name: str, rules: dict) -> None:
        """Добавляет или обновляет кастомный форматтер.

        Args:
            app_name: Нижнерегистровый идентификатор приложения.
            rules: Словарь правил форматирования (см. _apply_rules).

        Raises:
            ValueError: если app_name пустой или rules не словарь.
        """
        if not app_name or not isinstance(app_name, str):
            raise ValueError("app_name не может быть пустым")
        if not isinstance(rules, dict):
            raise ValueError("rules должен быть словарём")
        normalized = app_name.strip().lower()
        if not normalized:
            raise ValueError("app_name не может быть пустым после нормализации")
        with self._lock:
            self._custom[normalized] = dict(rules)
            self._save()

    def remove_custom_formatter(self, app_name: str) -> bool:
        """Удаляет кастомный форматтер. Возвращает True если был удалён."""
        normalized = (app_name or "").strip().lower()
        with self._lock:
            if normalized in self._custom:
                del self._custom[normalized]
                self._save()
                return True
        return False

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_format_for_paste(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: format_for_paste — форматирует текст под целевое приложение.

        Параметры:
            text (str): исходный текст
            app_name (str): имя целевого приложения (например "telegram")

        Возвращает:
            {formatted_text: str, app_name: str, formatter_used: str}
        """
        text = str(params.get("text", ""))
        app_name = str(params.get("app_name", "default")).strip()
        formatted = self.format_for_app(text, app_name)
        # Определяем какой форматтер был использован
        normalized = app_name.lower()
        with self._lock:
            formatter_used = normalized if normalized in self._custom else None
        if formatter_used is None:
            formatter_used = normalized if normalized in _BUILTIN_FORMATTERS else "default"
        return {
            "formatted_text": formatted,
            "app_name": app_name,
            "formatter_used": formatter_used,
        }

    def handle_list_paste_formatters(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: list_paste_formatters — список всех форматтеров.

        Возвращает:
            {formatters: [...], total: N}
        """
        formatters = self.list_formatters()
        return {"formatters": formatters, "total": len(formatters)}
