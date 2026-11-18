"""E6 (2026-11-18): точность стирания .md, живая выдача импорта и сиблинги превью.

Дополняет красные тесты разбора E6 (`test_e6_history_privacy_gate_2026_11_16.py`):
 * удаление записи стирает ТОЛЬКО свой .md и не трогает чужие файлы того же дня;
 * запись с диаризацией опознаётся по телу в формате writer'а;
 * `get_transcribe_progress`: задача, начатая при включённой приватности, отдаёт результат
   инициатору (панель импорта берёт тексты только отсюда), а начатая при выключенной — скрывается;
 * `preview_text` читается только через `_preview_snapshot()` (ответы start/stop/state).
"""

from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

import pytest

from _plaintext_export_test_helpers import history_service_with_off_policy
from backend.state_store import StateStore
from backend.transcript_writer import TranscriptWriter

_BACKEND = Path(__file__).resolve().parents[1] / "backend"


def _history(tmp_path: Path):
    store = StateStore(tmp_path / "data")
    return store, history_service_with_off_policy(store=store)


def _tdir(store) -> Path:
    path = store.data_dir / "transcripts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_delete_keeps_a_neighbours_first_of_day_file(tmp_path):
    store, svc = _history(tmp_path)
    mine = store.add_history_item(text="мой приватный текст", paste_status="ok")
    other = store.add_history_item(text="чужой совсем другой текст", paste_status="ok")
    # Первый файл дня принадлежит соседней записи, у моей — файла нет.
    neighbour = TranscriptWriter.write_transcript({"text": other.text, "ts": other.ts}, _tdir(store))
    assert neighbour.name.endswith("-Транскрибация.md")
    svc.handle_delete_history_item({"id": mine.id})
    assert neighbour.exists(), "удаление записи стёрло .md соседней записи"


def test_delete_keeps_an_unrelated_import_file(tmp_path):
    store, svc = _history(tmp_path)
    mine = store.add_history_item(text="текст удаляемой записи", paste_status="ok")
    unrelated = _tdir(store) / f"{time.strftime('%Y%m%d_%H%M%S')}_other.md"
    unrelated.write_text("# Транскрипт\n\n## Текст\n\nсовсем другой документ\n", encoding="utf-8")
    svc.handle_delete_history_item({"id": mine.id})
    assert unrelated.exists()


def test_a_short_text_does_not_erase_files_that_merely_contain_it(tmp_path):
    store, svc = _history(tmp_path)
    mine = store.add_history_item(text="да", paste_status="ok")
    other = _tdir(store) / f"{time.strftime('%Y%m%d_%H%M%S')}_long.md"
    other.write_text("# Транскрипт\n\n## Текст\n\nон сказал да и ушёл\n", encoding="utf-8")
    svc.handle_delete_history_item({"id": mine.id})
    assert other.exists(), "короткий текст опознал файл по подстроке"


def test_import_file_with_a_dialogue_section_is_erased(tmp_path):
    store, svc = _history(tmp_path)
    dialogue = "[Спикер 1]: здравствуйте\n[Спикер 2]: добрый день"
    mine = store.add_history_item(text=dialogue, paste_status="ok")
    md = _tdir(store) / f"{time.strftime('%Y%m%d_%H%M%S')}_call.md"
    md.write_text(f"# Транскрипт: call.m4a\n\n## Диалог\n\n{dialogue}\n", encoding="utf-8")
    svc.handle_delete_history_item({"id": mine.id})
    assert not md.exists()


def test_diarized_writer_file_is_matched_by_the_writer_body(tmp_path):
    store, svc = _history(tmp_path)
    mine = store.add_history_item(text="первая реплика вторая реплика", paste_status="ok")
    item = {
        "text": mine.text, "ts": mine.ts,
        "diarization": {"enabled": True, "speaker_turns": [
            {"speaker": "A", "text": "первая реплика"}, {"speaker": "B", "text": "вторая реплика"}]},
    }
    md = TranscriptWriter.write_transcript(item, _tdir(store))
    candidates = svc._transcript_md_candidates(mine.ts, item)
    assert md in candidates


def test_candidates_without_an_item_keep_the_old_time_only_behaviour(tmp_path):
    store, svc = _history(tmp_path)
    mine = store.add_history_item(text="текст", paste_status="ok")
    TranscriptWriter.write_transcript({"text": "текст", "ts": mine.ts}, _tdir(store))
    assert svc._transcript_md_candidates(mine.ts) == []


# --- get_transcribe_progress ----------------------------------------------------------------

class _Settings:
    def __init__(self, privacy):
        self.privacy = privacy

    def cached_settings(self, nowait: bool = False):
        return {"privacy_mode_enabled": self.privacy}


def _core(privacy: bool):
    from backend.job_tracker import JobTracker
    from backend.recording_core_service import RecordingCoreService

    core = RecordingCoreService.__new__(RecordingCoreService)
    core._settings_svc = _Settings(privacy)
    core._preview_lock = threading.Lock()
    core._job_tracker = JobTracker()
    return core


def _finished_job(core, **fields):
    job = core._job_tracker.create_job(total_files=1)
    core._job_tracker.update(job, **fields)
    core._job_tracker.update(
        job, status="done", current_file="/Users/x/секрет.m4a",
        items=[{"text": "полный текст", "audio_duration_sec": 1.0}], errors=["Файл не найден: секрет.m4a"],
    )
    return job


def test_progress_of_a_job_started_under_privacy_still_reaches_its_initiator():
    core = _core(True)
    job = _finished_job(core, privacy_at_start=True)
    out = core.handle_get_transcribe_progress({"job_id": job})
    assert out["items"] and out["items"][0]["text"] == "полный текст"


def test_progress_of_a_job_started_without_privacy_is_hidden_once_privacy_is_on():
    core = _core(True)
    job = _finished_job(core, privacy_at_start=False)
    out = core.handle_get_transcribe_progress({"job_id": job})
    blob = repr(out)
    assert out["items"] == []
    assert out["current_file"] == ""
    assert "секрет" not in blob and "полный текст" not in blob
    assert len(out["errors"]) == 1  # число ошибок сохранено, имена файлов — нет


def test_progress_is_untouched_while_privacy_is_off():
    core = _core(False)
    job = _finished_job(core, privacy_at_start=False)
    out = core.handle_get_transcribe_progress({"job_id": job})
    assert out["items"] and out["current_file"].endswith("секрет.m4a")


# --- превью ------------------------------------------------------------------------------------

def test_preview_snapshot_scrubs_text_that_was_accumulated_before_privacy_turned_on():
    core = _core(True)
    core._preview_text = "накопленный до включения приватности"
    core._preview_duration_sec = 4.0
    assert core._preview_snapshot() == ("", 4.0)


def test_preview_snapshot_keeps_the_text_when_privacy_is_off():
    core = _core(False)
    core._preview_text = "обычное превью"
    core._preview_duration_sec = 2.0
    assert core._preview_snapshot() == ("обычное превью", 2.0)


def test_preview_text_is_read_only_through_the_snapshot_helper():
    """Ни один ответ не читает `self._preview_text` мимо `_preview_snapshot()` (AST, не подстрока)."""
    tree = ast.parse((_BACKEND / "recording_core_service.py").read_text(encoding="utf-8"))
    allowed = {"_preview_snapshot", "preview_text"}  # хелпер и публичное свойство-аксессор
    offenders: list[tuple[str, int]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node):  # noqa: N802
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

        def visit_Attribute(self, node):  # noqa: N802
            if (
                node.attr == "_preview_text"
                and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Name) and node.value.id == "self"
                and not (set(self.stack) & allowed)
            ):
                offenders.append((self.stack[-1] if self.stack else "<module>", node.lineno))
            self.generic_visit(node)

    Visitor().visit(tree)
    assert not offenders, f"чтение _preview_text мимо _preview_snapshot: {offenders}"


@pytest.mark.parametrize("value", [None, 0, "", "yes", 1])
def test_privacy_getters_treat_a_non_bool_flag_as_on(tmp_path, value):
    core = _core(True)
    core._settings_svc = _Settings(value)
    assert core._privacy_mode_enabled() is True
    _, svc = _history(tmp_path)
    svc._settings_svc = _Settings(value)
    assert svc._is_privacy_mode() is True


def test_privacy_getters_treat_a_missing_key_as_off(tmp_path):
    class _Empty:
        def cached_settings(self, nowait: bool = False):
            return {}

    core = _core(False)
    core._settings_svc = _Empty()
    assert core._privacy_mode_enabled() is False
    _, svc = _history(tmp_path)
    svc._settings_svc = _Empty()
    assert svc._is_privacy_mode() is False
