"""Тесты для optional speaker labels в форматах экспорта (MD/SRT/JSON/CSV/Obsidian).

Покрывает задачу #feat/export-speaker-labels:
- Markdown с 2 спикерами
- SRT без меток (флаг False)
- JSON с 3 спикерами
- CSV с колонкой speaker
- SpeakerManager с именованными спикерами
- Нет диаризации → обычный текст
- Флаг disabled → обычный текст
- Локали RU / ES / EN для fallback-имён
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import HistoryService
from backend.state_store import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp: str) -> StateStore:
    return StateStore(Path(tmp) / "data")


class FakeSpeakerManager:
    """Подменяет SpeakerManager с предустановленными псевдонимами."""

    def __init__(self, aliases: dict[str, str]) -> None:
        self._aliases = aliases

    def get_alias(self, speaker_id: str) -> str | None:
        return self._aliases.get(speaker_id)


def _diar(turns: list[dict]) -> dict:
    return {"enabled": True, "speaker_turns": turns}


# ---------------------------------------------------------------------------
# Тест 1: Markdown — 2 спикера, include_speaker_labels=True
# ---------------------------------------------------------------------------

class TestMarkdownTwoSpeakers(unittest.TestCase):
    """handle_export_history с двумя спикерами (include_speaker_labels=True)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(self.tmp.name)
        self.svc = HistoryService(store=self.store)

        turns = [
            {"speaker": "SPEAKER_00", "text": "Привет", "start": 0.0, "end": 1.0},
            {"speaker": "SPEAKER_01", "text": "Добрый день", "start": 1.5, "end": 3.0},
        ]
        self.store.add_history_item(
            text="Привет Добрый день",
            paste_status="ok",
            source_lang="ru",
            diarization=_diar(turns),
        )

    def test_speaker_labels_present_in_markdown(self) -> None:
        export = self.svc.handle_export_history({"include_speaker_labels": True})
        content = export["content"]
        self.assertIn("**Спикер 1:**", content)
        self.assertIn("**Спикер 2:**", content)

    def test_speaker_labels_absent_when_disabled(self) -> None:
        export = self.svc.handle_export_history({"include_speaker_labels": False})
        content = export["content"]
        # Без меток должны быть сырые ID
        self.assertIn("[SPEAKER_00]:", content)
        # "**Спикер N:**" должно отсутствовать (метки отключены)
        self.assertNotIn("**Спикер 1:**", content)
        self.assertNotIn("**Спикер 2:**", content)


# ---------------------------------------------------------------------------
# Тест 2: SRT — 1 спикер без меток / с метками
# ---------------------------------------------------------------------------

class TestSRTLabels(unittest.TestCase):
    """handle_export_history_srt — проверка флага include_speaker_labels."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(self.tmp.name)
        self.svc = HistoryService(store=self.store)

        turns = [
            {"speaker": "SPEAKER_00", "text": "Монолог спикера", "start": 0.0, "end": 5.0},
            {"speaker": "SPEAKER_01", "text": "Ответ", "start": 5.5, "end": 7.0},
        ]
        item = self.store.add_history_item(
            text="Монолог спикера Ответ",
            paste_status="ok",
            source_lang="ru",
            diarization=_diar(turns),
        )
        self.item_id = item.id

    def test_no_speaker_labels_in_srt(self) -> None:
        result = self.svc.handle_export_history_srt(
            {"id": self.item_id, "include_speaker_labels": False}
        )
        self.assertIn("content", result)
        self.assertIn("[SPEAKER_00]:", result["content"])
        self.assertNotIn("Спикер", result["content"])

    def test_speaker_label_in_srt_when_enabled(self) -> None:
        result = self.svc.handle_export_history_srt(
            {"id": self.item_id, "include_speaker_labels": True}
        )
        self.assertIn("Спикер 1:", result["content"])
        self.assertNotIn("[SPEAKER_00]:", result["content"])


# ---------------------------------------------------------------------------
# Тест 3: JSON — 3 спикера с speaker_name
# ---------------------------------------------------------------------------

class TestJSONThreeSpeakers(unittest.TestCase):
    """handle_export_history_json — 3 спикера с speaker_name в segments."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(self.tmp.name)
        self.svc = HistoryService(store=self.store)

        turns = [
            {"speaker": "SPEAKER_00", "text": "А", "start": 0.0, "end": 1.0},
            {"speaker": "SPEAKER_01", "text": "Б", "start": 1.0, "end": 2.0},
            {"speaker": "SPEAKER_02", "text": "В", "start": 2.0, "end": 3.0},
        ]
        self.store.add_history_item(
            text="А Б В",
            paste_status="ok",
            source_lang="ru",
            diarization=_diar(turns),
        )

    def test_json_has_speaker_name_field(self) -> None:
        out_dir = Path(self.tmp.name) / "data" / "transcripts"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = self.svc.handle_export_history_json(
            {"include_speaker_labels": True, "save_to_file": True}
        )
        file_path = result.get("path")
        self.assertIsNotNone(file_path)
        payload = json.loads(Path(file_path).read_text())
        entries = payload["entries"]
        self.assertEqual(len(entries), 1)
        diar = entries[0]["diarization"]
        self.assertIsNotNone(diar)
        segs = diar["segments"]
        self.assertGreaterEqual(len(segs), 3)
        for seg in segs:
            self.assertIn("speaker_name", seg, f"Segment missing speaker_name: {seg}")
        names = [s["speaker_name"] for s in segs]
        self.assertIn("Спикер 1", names)
        self.assertIn("Спикер 2", names)
        self.assertIn("Спикер 3", names)


# ---------------------------------------------------------------------------
# Тест 4: CSV — колонка speaker с именами
# ---------------------------------------------------------------------------

class TestCSVSpeakerColumn(unittest.TestCase):
    """handle_export_history_csv — проверка колонки speaker."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(self.tmp.name)
        self.svc = HistoryService(store=self.store)

        turns = [
            {"speaker": "SPEAKER_00", "text": "текст", "start": 0.0, "end": 5.0},
        ]
        self.store.add_history_item(
            text="текст",
            paste_status="ok",
            source_lang="ru",
            diarization=_diar(turns),
        )

    def test_csv_speaker_column_with_labels(self) -> None:
        import csv
        import io

        out_dir = Path(self.tmp.name) / "data" / "transcripts"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = self.svc.handle_export_history_csv(
            {"include_speaker_labels": True, "copy_to_clipboard": False, "save_to_file": True}
        )
        file_path = result.get("file")
        self.assertIsNotNone(file_path)
        content = Path(file_path).read_text()
        reader = list(csv.DictReader(io.StringIO(content)))
        self.assertEqual(len(reader), 1)
        # Колонка называется "speaker" (обновлено с "speakers")
        speaker_col = reader[0].get("speaker") or reader[0].get("speakers", "")
        self.assertEqual(speaker_col, "Спикер 1")

    def test_csv_speaker_column_without_labels(self) -> None:
        import csv
        import io

        out_dir = Path(self.tmp.name) / "data" / "transcripts"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = self.svc.handle_export_history_csv(
            {"include_speaker_labels": False, "copy_to_clipboard": False, "save_to_file": True}
        )
        file_path = result.get("file")
        content = Path(file_path).read_text()
        reader = list(csv.DictReader(io.StringIO(content)))
        speaker_col = reader[0].get("speaker") or reader[0].get("speakers", "")
        self.assertEqual(speaker_col, "SPEAKER_00")


# ---------------------------------------------------------------------------
# Тест 5: SpeakerManager с псевдонимами
# ---------------------------------------------------------------------------

class TestSpeakerManagerAlias(unittest.TestCase):
    """_resolve_speaker_name использует псевдоним из SpeakerManager."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = HistoryService(store=_make_store(self.tmp.name))
        self.svc._speaker_manager = FakeSpeakerManager(
            {"SPEAKER_00": "Анна", "SPEAKER_01": "Иван"}
        )

    def test_alias_used_when_available(self) -> None:
        name = self.svc._resolve_speaker_name("SPEAKER_00", lang="ru")
        self.assertEqual(name, "Анна")

    def test_alias_for_second_speaker(self) -> None:
        name = self.svc._resolve_speaker_name("SPEAKER_01", lang="ru")
        self.assertEqual(name, "Иван")

    def test_fallback_when_no_alias(self) -> None:
        name = self.svc._resolve_speaker_name("SPEAKER_02", lang="ru")
        self.assertEqual(name, "Спикер 3")


# ---------------------------------------------------------------------------
# Тест 6: Нет диаризации → plain text
# ---------------------------------------------------------------------------

class TestNoDiarizationPlainText(unittest.TestCase):
    """Запись без диаризации экспортируется как plain text."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(self.tmp.name)
        self.svc = HistoryService(store=self.store)

        self.store.add_history_item(
            text="Обычный текст без спикеров",
            paste_status="ok",
        )

    def test_no_speaker_labels_without_diarization(self) -> None:
        export = self.svc.handle_export_history({"include_speaker_labels": True})
        content = export["content"]
        self.assertIn("Обычный текст без спикеров", content)
        self.assertNotIn("Спикер 1:", content)
        # "**Спикер N:**" должно отсутствовать (метки отключены)
        self.assertNotIn("**Спикер 1:**", content)
        self.assertNotIn("**Спикер 2:**", content)


# ---------------------------------------------------------------------------
# Тест 7: Флаг disabled → raw IDs (default behavior)
# ---------------------------------------------------------------------------

class TestFlagDisabledDefaultBehavior(unittest.TestCase):
    """При include_speaker_labels=False выводятся сырые ID."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(self.tmp.name)
        self.svc = HistoryService(store=self.store)

        turns = [
            {"speaker": "SPEAKER_00", "text": "Раз", "start": 0.0, "end": 1.0},
            {"speaker": "SPEAKER_01", "text": "Два", "start": 1.0, "end": 2.0},
        ]
        self.store.add_history_item(
            text="Раз Два",
            paste_status="ok",
            source_lang="ru",
            diarization=_diar(turns),
        )

    def test_raw_ids_when_disabled(self) -> None:
        export = self.svc.handle_export_history({"include_speaker_labels": False})
        content = export["content"]
        self.assertIn("[SPEAKER_00]:", content)
        self.assertIn("[SPEAKER_01]:", content)
        # "**Спикер N:**" должно отсутствовать (метки отключены)
        self.assertNotIn("**Спикер 1:**", content)
        self.assertNotIn("**Спикер 2:**", content)


# ---------------------------------------------------------------------------
# Тест 8: Локаль RU
# ---------------------------------------------------------------------------

class TestLocaleRU(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = HistoryService(store=_make_store(self.tmp.name))

    def test_ru_prefix(self) -> None:
        name = self.svc._resolve_speaker_name("SPEAKER_00", lang="ru")
        self.assertEqual(name, "Спикер 1")

    def test_ru_prefix_second(self) -> None:
        name = self.svc._resolve_speaker_name("SPEAKER_01", lang="ru")
        self.assertEqual(name, "Спикер 2")


# ---------------------------------------------------------------------------
# Тест 9: Локаль ES
# ---------------------------------------------------------------------------

class TestLocaleES(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = HistoryService(store=_make_store(self.tmp.name))

    def test_es_prefix(self) -> None:
        name = self.svc._resolve_speaker_name("SPEAKER_00", lang="es")
        self.assertEqual(name, "Hablante 1")

    def test_es_prefix_second(self) -> None:
        name = self.svc._resolve_speaker_name("SPEAKER_01", lang="es")
        self.assertEqual(name, "Hablante 2")


# ---------------------------------------------------------------------------
# Тест 10: Локаль EN + no lang defaults to RU
# ---------------------------------------------------------------------------

class TestLocaleEN(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = HistoryService(store=_make_store(self.tmp.name))

    def test_en_prefix(self) -> None:
        name = self.svc._resolve_speaker_name("SPEAKER_00", lang="en")
        self.assertEqual(name, "Speaker 1")

    def test_no_lang_defaults_ru(self) -> None:
        """Без языка fallback должен использовать русский префикс."""
        name = self.svc._resolve_speaker_name("SPEAKER_00", lang=None)
        self.assertEqual(name, "Спикер 1")


if __name__ == "__main__":
    unittest.main()
