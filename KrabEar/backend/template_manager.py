"""TemplateManager — управление текстовыми шаблонами быстрой вставки.

Позволяет хранить именованные шаблоны с поддержкой переменных {variable}.
Шаблоны сохраняются в {data_dir}/templates.json.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Встроенные шаблоны (доступны всегда, можно переопределить)
_BUILTIN_TEMPLATES: list[dict[str, Any]] = [
    {
        "name": "greeting_ru",
        "text": "Здравствуйте, {name}! Рады вас приветствовать.",
        "category": "greeting",
        "builtin": True,
    },
    {
        "name": "farewell_ru",
        "text": "До свидания, {name}! Хорошего дня.",
        "category": "farewell",
        "builtin": True,
    },
    {
        "name": "email_signature",
        "text": "С уважением,\n{sender_name}\n{sender_title}",
        "category": "email",
        "builtin": True,
    },
]


class TemplateManager:
    """Управляет текстовыми шаблонами быстрой вставки.

    Шаблоны хранятся в {data_dir}/templates.json как список объектов.
    Поддерживает переменную подстановку вида {variable_name}.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)
        self._file = self._data_dir / "templates.json"
        self._lock = threading.Lock()
        self._data_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Внутренние хелперы
    # ------------------------------------------------------------------

    def _load(self) -> list[dict[str, Any]]:
        """Загружает шаблоны из файла. Возвращает объединение builtin + user."""
        user_templates: list[dict[str, Any]] = []
        if self._file.exists():
            try:
                raw = self._file.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, list):
                    user_templates = [t for t in data if isinstance(t, dict)]
            except Exception as exc:
                _log.warning("Ошибка загрузки templates.json: %s", exc)

        # Builtin-шаблоны добавляются только если нет пользовательского с тем же именем
        user_names = {t.get("name") for t in user_templates}
        result = list(user_templates)
        for bt in _BUILTIN_TEMPLATES:
            if bt["name"] not in user_names:
                result.append(dict(bt))
        return result

    def _save_user(self, templates: list[dict[str, Any]]) -> None:
        """Сохраняет только пользовательские (не builtin) шаблоны."""
        user_only = [t for t in templates if not t.get("builtin", False)]
        self._file.write_text(
            json.dumps(user_only, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def get_templates(self) -> list[dict[str, Any]]:
        """Возвращает все шаблоны (builtin + пользовательские)."""
        with self._lock:
            return self._load()

    def add_template(
        self,
        name: str,
        text: str,
        category: str = "general",
    ) -> dict[str, Any]:
        """Добавляет или обновляет шаблон.

        Args:
            name: Уникальное имя шаблона (латиница/цифры/подчёркивание).
            text: Текст шаблона. Поддерживает {variable} подстановку.
            category: Категория шаблона (greeting, farewell, email, general и т.д.).

        Returns:
            Созданный/обновлённый объект шаблона.
        """
        name = name.strip()
        text = text.strip()
        category = category.strip() or "general"

        if not name:
            raise ValueError("Имя шаблона не может быть пустым")
        if not text:
            raise ValueError("Текст шаблона не может быть пустым")
        if not re.match(r"^[\w\-]+$", name):
            raise ValueError(f"Имя шаблона содержит недопустимые символы: {name!r}")

        template: dict[str, Any] = {
            "name": name,
            "text": text,
            "category": category,
            "builtin": False,
        }

        with self._lock:
            templates = self._load()
            # Заменяем существующий или добавляем новый
            updated = False
            for i, t in enumerate(templates):
                if t.get("name") == name:
                    templates[i] = template
                    updated = True
                    break
            if not updated:
                templates.append(template)
            self._save_user(templates)
            _log.debug("Шаблон %r %s", name, "обновлён" if updated else "добавлен")
            return dict(template)

    def remove_template(self, name: str) -> bool:
        """Удаляет шаблон по имени.

        Args:
            name: Имя шаблона для удаления.

        Returns:
            True если шаблон найден и удалён, False если не найден.
        """
        name = name.strip()
        with self._lock:
            templates = self._load()
            before = len(templates)
            templates = [t for t in templates if t.get("name") != name]
            if len(templates) == before:
                return False
            self._save_user(templates)
            _log.debug("Шаблон %r удалён", name)
            return True

    def purge_all(self) -> bool:
        """Полностью удаляет ВСЕ пользовательские шаблоны (privacy-purge / wipe-all).

        W1771 GAP-2: пользовательские шаблоны хранят свободный текст ``text``
        без какой-либо фильтрации — email-подписи с реальными именами/телефонами,
        приветствия с {name}-плейсхолдерами вокруг настоящих имён. Это PII, и
        templates.json обязан исчезать при полной очистке данных (ранее файл был
        ошибочно в allowlist как «app config»). Встроенные (builtin) шаблоны при
        этом не теряются — они зашиты в коде и снова появятся при следующем
        ``_load()``; на диске удаляется только пользовательский слой.

        Удаляет файл templates.json под ``_lock`` (in-memory-кэша у менеджера нет —
        он читает с диска при каждом обращении, так что отдельно «очищать RAM»
        не требуется). Вызывается ТОЛЬКО из handle_purge_all_data. Идемпотентен.

        Returns:
            True если файл существовал и был удалён, False если его не было.
        """
        with self._lock:
            existed = self._file.exists()
            self._file.unlink(missing_ok=True)
            if existed:
                _log.info("purge_all: templates.json удалён (privacy-purge)")
            return existed

    def apply_template(
        self,
        name: str,
        variables: dict[str, str] | None = None,
    ) -> str:
        """Применяет шаблон с подстановкой переменных.

        Args:
            name: Имя шаблона.
            variables: Словарь переменных для подстановки {key} → value.

        Returns:
            Готовый текст с подставленными переменными.

        Raises:
            KeyError: Если шаблон с таким именем не найден.
        """
        with self._lock:
            templates = self._load()

        target: dict[str, Any] | None = None
        for t in templates:
            if t.get("name") == name:
                target = t
                break
        if target is None:
            raise KeyError(f"Шаблон не найден: {name!r}")

        text: str = target["text"]
        if variables:
            text = re.sub(
                r'\{(\w+)\}',
                lambda m: str(variables[m.group(1)]) if m.group(1) in variables else m.group(0),
                text,
            )
        return text

    # ------------------------------------------------------------------
    # IPC handlers
    # ------------------------------------------------------------------

    def handle_get_templates(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: возвращает список всех шаблонов."""
        return {"templates": self.get_templates()}

    def handle_add_template(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: добавляет или обновляет шаблон."""
        name = str(params.get("name", "")).strip()
        text = str(params.get("text", "")).strip()
        category = str(params.get("category", "general")).strip()
        template = self.add_template(name=name, text=text, category=category)
        return {"template": template}

    def handle_remove_template(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: удаляет шаблон по имени."""
        name = str(params.get("name", "")).strip()
        removed = self.remove_template(name)
        return {"removed": removed, "name": name}

    def handle_apply_template(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: применяет шаблон с переменными."""
        name = str(params.get("name", "")).strip()
        variables = params.get("variables") or {}
        if not isinstance(variables, dict):
            variables = {}
        text = self.apply_template(name=name, variables=variables)
        return {"text": text, "name": name}
