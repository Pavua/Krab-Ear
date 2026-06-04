"""Shared test factory for domain models.

Provides make_test_item() and make_test_items() so tests work with real
HistoryItem instances instead of MagicMock objects, preventing silent
attribute-typo bugs.
"""

from __future__ import annotations

from typing import Any

from backend.models import HistoryItem

# Sensible defaults covering the most commonly used fields.
_ITEM_DEFAULTS: dict[str, Any] = {
    "id": "test-id-001",
    "ts": "2026-01-01T12:00:00Z",
    "text": "Test transcript.",
    "paste_status": "ok",
    "source_lang": "ru",
    "confidence": 0.92,
    "audio_duration_sec": 12.5,
}


def make_test_item(**overrides: Any) -> HistoryItem:
    """Return a real HistoryItem with sensible defaults.

    All keyword arguments are forwarded to HistoryItem(), overriding the
    corresponding default value.  Fields not listed in _ITEM_DEFAULTS or
    *overrides* keep their dataclass defaults (empty string, None, False, []).

    Example::

        item = make_test_item(text="Привет мир", confidence=0.75)
        assert item.source_lang == "ru"   # from defaults
        assert item.confidence == 0.75    # from override
    """
    kwargs = {**_ITEM_DEFAULTS, **overrides}
    return HistoryItem(**kwargs)


def make_test_items(n: int, **overrides: Any) -> list[HistoryItem]:
    """Return a list of *n* HistoryItem objects.

    Each item gets a unique ``id`` of the form ``"test-id-{i:03d}"`` unless
    ``id`` is explicitly supplied in *overrides* (in which case all items
    share that id, which is intentional for some test scenarios).

    Example::

        items = make_test_items(3, source_lang="es")
        assert len(items) == 3
        assert items[0].id == "test-id-000"
        assert items[2].source_lang == "es"
    """
    has_explicit_id = "id" in overrides
    return [
        make_test_item(**({} if has_explicit_id else {"id": f"test-id-{i:03d}"}), **overrides)
        for i in range(n)
    ]
