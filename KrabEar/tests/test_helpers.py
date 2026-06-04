"""Shared test factory for domain models.

Provides make_test_item() and make_test_items() so tests work with real
HistoryItem instances instead of MagicMock objects, preventing silent
attribute-typo bugs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def make_test_item(
    days_ago: int | None = None,
    hour: int | None = None,
    item_id: str | None = None,
    **overrides: Any,
) -> HistoryItem:
    """Return a real HistoryItem with sensible defaults.

    Convenience parameters (not HistoryItem fields):
        days_ago (int | None): shift ts *N* days into the past.  ``0`` = today.
        hour (int | None): set hour component of the resulting ts (0–23).
        item_id (str | None): alias for the ``id`` field (backwards compat with
            old per-file ``_make_item(item_id=...)`` factories).

    If ``days_ago`` or ``hour`` is provided they compute a ``ts`` string
    (``YYYY-MM-DDTHH:MM:SSZ``) **unless** ``ts`` is also in *overrides*
    (explicit ``ts`` wins).

    All remaining keyword arguments are forwarded to HistoryItem(), overriding
    the corresponding default value.  Fields not listed in _ITEM_DEFAULTS or
    *overrides* keep their dataclass defaults (empty string, None, False, []).

    Example::

        item = make_test_item(text="Привет мир", confidence=0.75)
        assert item.source_lang == "ru"   # from defaults
        assert item.confidence == 0.75    # from override

        item = make_test_item(days_ago=3, hour=14)
        # ts ≈ "2026-06-01T14:00:00Z" (3 days ago at 14:00 UTC)

        item = make_test_item(item_id="my-id")
        assert item.id == "my-id"
    """
    # Resolve item_id alias (old factory compat).
    if item_id is not None and "id" not in overrides:
        overrides["id"] = item_id
    # Compute ts from days_ago / hour convenience params.
    if (days_ago is not None or hour is not None) and "ts" not in overrides:
        now = datetime.now(timezone.utc)
        if days_ago is not None:
            now = now - timedelta(days=days_ago)
        if hour is not None:
            now = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        overrides["ts"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
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


# ---------------------------------------------------------------------------
# Self-tests — keep pytest exit code 0 when this file is run standalone
# (CI collects test_*.py via glob; exit code 5 = "no tests" → treated as FAIL)
# ---------------------------------------------------------------------------

class TestMakeTestItemFactory:
    """Smoke tests for make_test_item() and make_test_items()."""

    def test_defaults_are_valid_history_item(self) -> None:
        item = make_test_item()
        assert item.id == "test-id-001"
        assert item.text == "Test transcript."
        assert item.source_lang == "ru"
        assert item.confidence == 0.92
        assert item.audio_duration_sec == 12.5

    def test_override_replaces_default(self) -> None:
        item = make_test_item(text="custom", confidence=0.5)
        assert item.text == "custom"
        assert item.confidence == 0.5
        assert item.source_lang == "ru"  # default preserved

    def test_days_ago_sets_ts(self) -> None:
        from datetime import datetime, timezone
        item = make_test_item(days_ago=0)
        dt = datetime.fromisoformat(item.ts.replace("Z", "+00:00"))
        today = datetime.now(timezone.utc).date()
        assert dt.date() == today

    def test_item_id_alias(self) -> None:
        item = make_test_item(item_id="my-special-id")
        assert item.id == "my-special-id"

    def test_explicit_ts_wins_over_days_ago(self) -> None:
        item = make_test_item(days_ago=5, ts="2026-03-15T10:00:00Z")
        assert item.ts == "2026-03-15T10:00:00Z"

    def test_make_test_items_unique_ids(self) -> None:
        items = make_test_items(3)
        ids = [i.id for i in items]
        assert ids == ["test-id-000", "test-id-001", "test-id-002"]

    def test_make_test_items_shared_override(self) -> None:
        items = make_test_items(2, source_lang="es")
        assert all(i.source_lang == "es" for i in items)
