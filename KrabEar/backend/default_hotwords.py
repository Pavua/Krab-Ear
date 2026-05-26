"""default_hotwords.py — кураторский список дефолтных STT hotwords.

Используется для авто-сида hotwords store при первом запуске (или по запросу).
Hotwords передаются в Whisper как ``initial_prompt``, что сдвигает декодер
в сторону ожидаемого словаря и улучшает распознавание брендов / терминов.

Пример использования:
    from backend.default_hotwords import seed_hotwords, DEFAULT_DEV_HOTWORDS
    seed_hotwords(settings_svc, only_if_empty=True)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.settings_service import SettingsService

# ---------------------------------------------------------------------------
# Кураторский список по категориям
# ---------------------------------------------------------------------------

_CATEGORIES: dict[str, list[str]] = {
    "ai": [
        "Claude",
        "Anthropic",
        "GPT",
        "OpenAI",
        "Llama",
        "Gemma",
        "Mistral",
        "DeepSeek",
        "Qwen",
        "Gemini",
        "Grok",
        "Perplexity",
        "Cohere",
        "Ollama",
        "LM Studio",
        "HuggingFace",
    ],
    "dev_tools": [
        "GitHub",
        "GitLab",
        "Bitbucket",
        "Telegram",
        "Obsidian",
        "Slack",
        "Notion",
        "Linear",
        "Figma",
        "Cursor",
        "VS Code",
        "Xcode",
        "PyCharm",
        "iTerm2",
        "Docker",
    ],
    "languages": [
        "Swift",
        "Rust",
        "Python",
        "TypeScript",
        "JavaScript",
        "Kotlin",
        "Go",
    ],
    "formats": [
        "Markdown",
        "JSON",
        "YAML",
        "CLAUDE.md",
        "AGENTS.md",
        "TOML",
        "NDJSON",
    ],
    "infra": [
        "Kubernetes",
        "Terraform",
        "NGINX",
        "PostgreSQL",
        "SQLite",
        "Redis",
        "Cloudflare",
        "Vercel",
        "Telnyx",
        "Twilio",
        "Sentry",
    ],
    "apple": [
        "AppleScript",
        "osascript",
        "NSWorkspace",
        "NSUserDefaults",
        "Foundation",
        "SwiftUI",
        "AppKit",
        "CoreML",
        "ScreenCaptureKit",
    ],
    "common": [
        "GitHub Actions",
        "pull request",
        "rebase",
        "merge",
        "commit",
        "subagent",
        "worktree",
        "IPC",
        "REST",
        "JSON-RPC",
        "WebSocket",
        "hotkey",
        "diarization",
        "transcription",
    ],
}

# Плоский список — без дублей, порядок: категория за категорией.
DEFAULT_DEV_HOTWORDS: list[str] = []
_seen: set[str] = set()
for _cat_words in _CATEGORIES.values():
    for _w in _cat_words:
        if _w not in _seen:
            DEFAULT_DEV_HOTWORDS.append(_w)
            _seen.add(_w)
del _seen, _cat_words, _w  # type: ignore[name-defined]


def get_default_hotwords(category: str | None = None) -> list[str]:
    """Возвращает дефолтный список hotwords, опционально отфильтрованный по категории.

    Args:
        category: одна из "ai", "dev_tools", "languages", "formats",
                  "infra", "apple", "common". ``None`` → все категории.

    Returns:
        Список строк без дублей.  Неизвестная категория → пустой список.
    """
    if category is None:
        return list(DEFAULT_DEV_HOTWORDS)
    return list(_CATEGORIES.get(category, []))


def seed_hotwords(
    settings_svc: "SettingsService",
    *,
    category: str | None = None,
    only_if_empty: bool = True,
) -> int:
    """Добавляет дефолтные hotwords в settings_svc.

    Args:
        settings_svc: Экземпляр SettingsService (хранит hotwords в settings.json).
        category: Фильтр по категории; ``None`` → все.
        only_if_empty: Если ``True`` и у пользователя уже есть хотя бы один
                       hotword — пропускаем (не перезаписываем его список).

    Returns:
        Количество фактически добавленных hotwords (0 если пропущено).
    """
    words_to_add: list[str] = get_default_hotwords(category)
    if not words_to_add:
        return 0

    current: list[str] = settings_svc.cached_settings().get("stt_hotwords", [])
    if not isinstance(current, list):
        current = []

    if only_if_empty and current:
        return 0

    # Мерж: не дублируем слова, которые уже есть.
    existing_set: set[str] = set(current)
    new_words: list[str] = [w for w in words_to_add if w not in existing_set]
    if not new_words:
        return 0

    merged: list[str] = current + new_words
    settings_svc.handle_set_settings({"stt_hotwords": merged})
    return len(new_words)
