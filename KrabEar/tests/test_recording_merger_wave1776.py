"""wave1776 — RecordingMerger cluster fixes (2 HIGH + 3 MED).

Использует РЕАЛЬНЫЙ StateStore во временной директории + HistoryService,
смонтированный как в production (merger.cascade_delete_fn → cascade_delete_artifacts),
чтобы проверить КАСКАДНОЕ удаление, в т.ч. стирание .md-транскрипта.

Покрывает:
  HIGH 1 — merge(delete_originals=True) СТИРАЕТ .md каждого источника.
  MED 4  — защищённый (is_protected) источник НЕ удаляется.
  MED 3  — merged item имеет is_protected/privacy_mode/favorite=True, если у любого
           источника True; word_timestamps/speaker_turns конкатенированы.
  MED 5  — merge атомарна: берёт store._lock() один раз вокруг append-merged+tombstones.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_merger import RecordingMerger  # noqa: E402
from backend.history_service import HistoryService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from backend.models import HistoryItem  # noqa: E402


def _md_name_for_ts(ts: str) -> str:
    """Имя .md файла, которое матчит _transcript_md_candidates glob."""
    dt = datetime.fromisoformat(ts)
    date_str = dt.strftime("%Y-%m-%d")
    time_str = dt.strftime("%H%M%S")
    return f"{date_str}-Транскрибация-{time_str}.md"


class _Wave1776Base(unittest.TestCase):
    """Общий setUp: реальный StateStore + HistoryService + смонтированный merger."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.store = StateStore(data_dir=self.data_dir)
        self.transcripts_dir = self.data_dir / "transcripts"
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)

        self.history = HistoryService(store=self.store)
        self.merger = RecordingMerger()
        # Production wiring (см. service.py wave1776 late-injection): merge сам пишет
        # tombstone атомарно, cascade_delete_artifacts доделывает .md/semantic/etc.
        self.merger.cascade_delete_fn = self.history.cascade_delete_artifacts
        self._ts_counter = 0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _add(self, text: str, **kwargs) -> object:
        """Добавляет запись с УНИКАЛЬНЫМ ts (через unlocked append).

        add_history_item присваивает ts с точностью до секунды → быстрые подряд
        вызовы дают одинаковый ts (и одинаковое имя .md).  Здесь раздаём
        возрастающие секундные ts, чтобы каждый источник имел отдельный .md файл.
        """
        self._ts_counter += 1
        ts = (
            datetime(2026, 4, 12, 9, 0, 0, tzinfo=timezone.utc)
            + timedelta(seconds=self._ts_counter)
        ).isoformat(timespec="seconds")
        item = HistoryItem.create(text=text, paste_status="success", **kwargs)
        item.ts = ts
        with self.store._lock():
            self.store._append_ndjson(self.store.history_path, item.to_dict())
        return item

    def _write_md(self, item) -> Path:
        md = self.transcripts_dir / _md_name_for_ts(item.ts)
        md.write_text(f"# Транскрипт\n\n{item.text}\n", encoding="utf-8")
        return md


# ---------------------------------------------------------------------------
# HIGH 1 — .md erase purge gap
# ---------------------------------------------------------------------------


class TestMergeErasesTranscriptMd(_Wave1776Base):
    """HIGH 1: при merge(delete_originals=True) .md каждого источника стирается."""

    def test_merge_delete_originals_erases_source_md(self) -> None:
        a = self._add("Первый фрагмент")
        b = self._add("Второй фрагмент")
        md_a = self._write_md(a)
        md_b = self._write_md(b)
        self.assertTrue(md_a.exists())
        self.assertTrue(md_b.exists())

        result = self.merger.merge_items(
            [a.id, b.id], self.store, delete_originals=True
        )

        # Каноническое каскадное удаление должно стереть .md обоих источников.
        self.assertFalse(md_a.exists(), "источник .md A пережил merge-delete (privacy gap)")
        self.assertFalse(md_b.exists(), "источник .md B пережил merge-delete (privacy gap)")
        self.assertCountEqual(result["deleted_ids"], [a.id, b.id])

    def test_merge_without_delete_keeps_md(self) -> None:
        a = self._add("Keep A")
        b = self._add("Keep B")
        md_a = self._write_md(a)
        md_b = self._write_md(b)

        self.merger.merge_items([a.id, b.id], self.store, delete_originals=False)

        # Без delete_originals .md остаются на месте.
        self.assertTrue(md_a.exists())
        self.assertTrue(md_b.exists())


# ---------------------------------------------------------------------------
# MED 4 — protected source is never auto-deleted
# ---------------------------------------------------------------------------


class TestMergeSkipsProtected(_Wave1776Base):
    """MED 4: is_protected источник не удаляется и его .md сохраняется."""

    def test_protected_source_not_deleted(self) -> None:
        a = self._add("Обычная запись")
        b = self._add("Защищённая запись", is_protected=True)
        md_a = self._write_md(a)
        md_b = self._write_md(b)

        result = self.merger.merge_items(
            [a.id, b.id], self.store, delete_originals=True
        )

        # Обычная удалена; защищённая — пропущена.
        self.assertIn(a.id, result["deleted_ids"])
        self.assertNotIn(b.id, result["deleted_ids"])
        self.assertIn(b.id, result["skipped_protected_ids"])

        # Защищённая запись остаётся активной (get_history_item_by_id → не None);
        # обычная — tombstoned (None).
        self.assertIsNotNone(self.store.get_history_item_by_id(b.id))
        self.assertIsNone(self.store.get_history_item_by_id(a.id))
        # Её .md уцелел; .md обычной — стёрт.
        self.assertTrue(md_b.exists(), "защищённый источник .md был ошибочно удалён")
        self.assertFalse(md_a.exists())


# ---------------------------------------------------------------------------
# MED 3 — preserved fields (OR-aggregate + concat)
# ---------------------------------------------------------------------------


class TestMergePreservesFields(_Wave1776Base):
    """MED 3: is_protected/privacy_mode/favorite OR-агрегируются; timestamps concat."""

    def test_or_aggregate_protected_privacy_favorite(self) -> None:
        a = self._add("A", is_protected=False, privacy_mode=False, favorite=False)
        b = self._add(
            "B", is_protected=True, privacy_mode=True, favorite=True
        )
        result = self.merger.merge_items([a.id, b.id], self.store)
        self.assertTrue(result["is_protected"])
        self.assertTrue(result["privacy_mode"])
        self.assertTrue(result["favorite"])

    def test_all_false_stays_false(self) -> None:
        a = self._add("A")
        b = self._add("B")
        result = self.merger.merge_items([a.id, b.id], self.store)
        self.assertFalse(result["is_protected"])
        self.assertFalse(result["privacy_mode"])
        self.assertFalse(result["favorite"])

    def test_word_timestamps_and_speaker_turns_concatenated(self) -> None:
        a = self._add(
            "Раз",
            word_timestamps=[{"word": "раз", "start": 0.0, "end": 0.5}],
            speaker_turns=[{"speaker": "A", "start": 0.0, "end": 0.5}],
        )
        b = self._add(
            "Два",
            word_timestamps=[{"word": "два", "start": 0.0, "end": 0.6}],
            speaker_turns=[{"speaker": "B", "start": 0.0, "end": 0.6}],
        )
        result = self.merger.merge_items([a.id, b.id], self.store)
        self.assertEqual(len(result["word_timestamps"]), 2)
        self.assertEqual(len(result["speaker_turns"]), 2)
        words = [w["word"] for w in result["word_timestamps"]]
        self.assertEqual(words, ["раз", "два"])

    def test_audio_path_first_non_empty_preserved(self) -> None:
        a = self._add("A", audio_path="")
        b = self._add("B", audio_path="/tmp/clip_b.wav")
        result = self.merger.merge_items([a.id, b.id], self.store)
        self.assertEqual(result["audio_path"], "/tmp/clip_b.wav")


# ---------------------------------------------------------------------------
# MED 5 — atomicity: single lock around append-merged + tombstones
# ---------------------------------------------------------------------------


class TestMergeAtomic(_Wave1776Base):
    """MED 5: append-merged + tombstones выполняются под ОДНИМ lock'ом без
    вложения (иначе flock на отдельном fd → deadlock в одном процессе)."""

    def test_merge_write_section_lock_not_nested(self) -> None:
        a = self._add("Atom A")
        b = self._add("Atom B")

        calls = {"write_section_count": 0, "max_depth": 0, "depth": 0}
        real_lock = self.store._lock

        from contextlib import contextmanager

        @contextmanager
        def _counting_lock():
            calls["depth"] += 1
            calls["max_depth"] = max(calls["max_depth"], calls["depth"])
            # «Write-секция» = lock, удерживаемый одновременно с append в history_path.
            # Засчитываем единственный lock, внутри которого пишется merged-запись.
            before = self._history_lines()
            with real_lock():
                try:
                    yield
                finally:
                    after = self._history_lines()
                    if after > before:
                        calls["write_section_count"] += 1
                    calls["depth"] -= 1

        self.store._lock = _counting_lock
        try:
            self.merger.merge_items([a.id, b.id], self.store, delete_originals=True)
        finally:
            self.store._lock = real_lock

        # Никакого вложенного lock'а (deadlock-safety).
        self.assertEqual(calls["max_depth"], 1)
        # merged-запись добавлена в истории ровно под одним lock-сегментом.
        self.assertEqual(calls["write_section_count"], 1)

    def _history_lines(self) -> int:
        try:
            return sum(
                1 for _ in self.store.history_path.read_text(encoding="utf-8").splitlines()
            )
        except FileNotFoundError:
            return 0

    def test_merge_atomic_writes_merged_and_tombstones(self) -> None:
        a = self._add("Persist A")
        b = self._add("Persist B")
        result = self.merger.merge_items(
            [a.id, b.id], self.store, delete_originals=True
        )
        # merged присутствует, оба оригинала tombstoned (исчезли из активных).
        self.assertIsNotNone(self.store.get_history_item_by_id(result["id"]))
        self.assertIsNone(self.store.get_history_item_by_id(a.id))
        self.assertIsNone(self.store.get_history_item_by_id(b.id))


if __name__ == "__main__":
    unittest.main()
