"""Синтетические проверки общей шкалы истории и файлов экспорта."""
import json
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.models import HistoryItem
from backend.timeline_export import TimelineExporter
from backend.timeline_view import TimelineViewGenerator
from backend.state_store import StateStore
from test_plaintext_export_integration import IsolatedBackend, synthetic_subprocess_env, swift_harness  # noqa: F401


@pytest.fixture
def entries():
    return [
        HistoryItem("a", "2026-10-25T02:15:00+02:00", "reunio\u0301n", source_lang="es",
                    audio_duration_sec=0.125),
        HistoryItem("b", "2026-10-25T00:30:00Z", "reunión", source_lang="es",
                    audio_duration_sec=1.25),
        HistoryItem("c", "2026-10-25T02:15:00+01:00", "проверка", source_lang="ru",
                    audio_duration_sec=2.5),
        HistoryItem("d", "2026-10-26T05:00:00+05:30", "и", audio_duration_sec=-1),
        HistoryItem("e", "2026-10-26T00:15:00", "синтетика"),
        HistoryItem("f", "invalid", "исключить"),
    ]


def test_hourly_utc_keeps_both_dst_occurrences_and_crosses_midnight(entries):
    blocks = TimelineViewGenerator().generate_timeline(entries)
    assert [(b.start_time, b.items_count, b.total_duration_sec) for b in blocks] == [
        ("2026-10-25T00:00:00+00:00", 2, 1.375),
        ("2026-10-25T01:00:00+00:00", 1, 2.5),
        ("2026-10-25T23:00:00+00:00", 1, 0),
        ("2026-10-26T00:00:00+00:00", 1, 0),
    ]
    assert [b.end_time for b in blocks[:-1]] == [
        "2026-10-25T01:00:00+00:00", "2026-10-25T02:00:00+00:00",
        "2026-10-26T00:00:00+00:00",
    ]


@pytest.mark.parametrize("group_by, expected", [
    ("day", [("2026-10-25", 4), ("2026-10-26", 1)]),
    ("week", [("2026-10-19", 4), ("2026-10-26", 1)]),
])
def test_day_and_week_share_utc_boundaries(entries, group_by, expected):
    blocks = TimelineViewGenerator().generate_timeline(entries, group_by)
    assert [(b.start_time[:10], b.items_count) for b in blocks] == expected
    assert blocks[0].end_time == blocks[1].start_time


def test_spring_dst_and_non_hour_offset_have_contiguous_utc_hours():
    items = [{"ts": ts} for ts in (
        "2026-03-29T01:55:00+01:00", "2026-03-29T03:05:00+02:00",
        "2026-03-29T06:45:00+05:30",
    )]
    blocks = TimelineViewGenerator().generate_timeline(items)
    assert [(b.start_time[11:16], b.items_count) for b in blocks] == [("00:00", 1), ("01:00", 2)]
    assert blocks[0].end_time == blocks[1].start_time


def test_heatmap_uses_same_utc_hour_and_day(entries):
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    with patch("backend.timeline_view.datetime", wraps=datetime) as clock:
        clock.now.return_value = now
        heatmap = TimelineViewGenerator().generate_activity_heatmap(entries)
    assert heatmap["total_items"] == 5
    assert heatmap["matrix"]["0"]["6"] == 2
    assert heatmap["matrix"]["0"]["0"] == 1
    assert heatmap["matrix"]["23"]["6"] == 1


def test_duration_and_unicode_summary_are_safe_to_serialize():
    items = [{"ts": "2026-10-25T12:00:00Z", "text": word, "audio_duration_sec": duration}
             for word, duration in [("reunión", 0.125), ("reunio\u0301n", "NaN"),
                                    ("кириллица", "inf"), ("проверка", -1)]]
    blocks = [b.to_dict() for b in TimelineViewGenerator().generate_timeline(items)]
    payload = json.loads(TimelineExporter().export_json(blocks))
    assert payload["total_duration_sec"] == 0.125
    assert payload["blocks"][0]["summary_text"].split(", ") == ["reunión", "кириллица", "проверка"]


def test_svg_shows_sparse_time_and_audio_duration(entries):
    blocks = [b.to_dict() for b in TimelineViewGenerator().generate_timeline(entries)]
    svg = TimelineExporter().export_svg(blocks, height=100)
    root = ET.fromstring(svg)
    bars = root.findall("{*}rect")
    assert len(bars) == 4
    assert all(float(bar.get("height")) > 0 for bar in bars)
    gap = float(bars[2].get("x")) - float(bars[1].get("x")) - float(bars[1].get("width"))
    assert gap > 20 * float(bars[0].get("width"))
    assert "1.375s" in bars[0].find("{*}title").text
    assert "10/25 00:00" in svg


def test_ical_reexport_keeps_event_identity_and_utc_boundaries(entries):
    blocks = [b.to_dict() for b in TimelineViewGenerator().generate_timeline(entries)]
    exporter = TimelineExporter()
    with patch("backend.timeline_export.datetime", wraps=datetime) as clock:
        clock.now.return_value = datetime(2026, 11, 1, tzinfo=timezone.utc)
        first = exporter.export_ical(blocks)
        clock.now.return_value = datetime(2026, 11, 2, tzinfo=timezone.utc)
        second = exporter.export_ical(blocks)
    assert first != second
    assert [line for line in first.splitlines() if not line.startswith("DTSTAMP:")] == [
        line for line in second.splitlines() if not line.startswith("DTSTAMP:")
    ]
    starts = [line for line in first.splitlines() if line.startswith("DTSTART:")]
    assert starts == ["DTSTART:20261025T000000Z", "DTSTART:20261025T010000Z",
                      "DTSTART:20261025T230000Z", "DTSTART:20261026T000000Z"]
    assert len({line for line in first.splitlines() if line.startswith("UID:")}) == len(starts)
    assert "Duration: 1.375s" in first.replace("\r\n ", "")


def test_ical_positive_intervals_and_utf8_folding():
    raw_item = {"ts": "2026-10-25T02:59:59.750+02:00", "audio_duration_sec": 0.5,
                "summary_text": "reunión 東京 🎙️ " * 9}
    blocks = [
        {"start_time": "2026-10-25T02:00:00+02:00", "end_time": "2026-10-25T00:00:00Z"},
        {"start_time": "2026-10-25T01:00:00Z"},
        raw_item,
    ]
    ical = TimelineExporter().export_ical(blocks)
    assert ical.count("BEGIN:VEVENT") == 1
    assert "DTSTART:20261025T005959Z\r\nDTEND:20261025T010001Z" in ical
    assert f"SUMMARY:{raw_item['summary_text']}" in ical.replace("\r\n ", "")
    assert all(len(line.encode("utf-8")) <= 75 for line in ical.split("\r\n"))


@pytest.mark.parametrize("fmt", ["svg", "json", "ical"])
def test_repeat_export_in_same_second_returns_distinct_files(tmp_path, monkeypatch, fmt):
    from _settings_test_helpers import safe_backend_for_settings

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 1, tzinfo=tz)

    with ExitStack() as cleanup:
        monkeypatch.setenv("KRAB_EAR_BRAIN_LEASE_PATH", str(tmp_path / "brain.lock"))
        store = StateStore(tmp_path / "profile")
        store.initialize_startup_plaintext_policy(new_profile=True)
        svc = safe_backend_for_settings(SimpleNamespace(addCleanup=cleanup.callback), store)

        def export():
            response = svc.handle_request({"id": 1, "method": f"export_timeline_{fmt}", "params": {}})
            assert response["ok"] is True
            assert response["result"]["blocks"] == 0
            return Path(response["result"]["path"])

        with patch("backend.service.datetime", FrozenDateTime), patch("backend.timeline_export.datetime", FrozenDateTime):
            first = export()
            first_contents = first.read_bytes()
            second = export()
        assert first != second
        assert first.read_bytes() == first_contents
        assert second.read_bytes() == first_contents
        if fmt == "json":
            assert json.loads(first_contents)["blocks"] == []
        elif fmt == "svg":
            assert b"No data" in first_contents
        else:
            assert b"BEGIN:VEVENT" not in first_contents


def test_swift_export_matches_timeline_ipc(entries, swift_harness):  # noqa: F811
    with tempfile.TemporaryDirectory(prefix="ear-tl-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        profile = root / "profile"
        store = StateStore(profile)
        store.initialize_startup_plaintext_policy(new_profile=True)
        store.save_settings({"history_encryption_enabled": False, "privacy_mode_enabled": False})
        journal = profile / "history.ndjson"
        journal.write_text("".join(json.dumps(item.to_dict()) + "\n" for item in entries), encoding="utf-8")
        original = journal.read_bytes()
        backend = IsolatedBackend(root)
        try:
            backend.start()
            view = backend.call("get_timeline_view")["result"]
            assert [(b["start_time"], b["items_count"]) for b in view["blocks"]] == [
                ("2026-10-25T00:00:00+00:00", 4), ("2026-10-26T00:00:00+00:00", 1),
            ]
            def export(fmt):
                process = subprocess.run(
                    [str(swift_harness), str(root / "ipc.sock"), "backend", f"export_timeline_{fmt}", "grant", "run"],
                    env=synthetic_subprocess_env(root), capture_output=True, text=True, timeout=30,
                )
                assert process.returncode == 0, "Swift timeline client failed"
                report = json.loads(process.stdout)
                assert (report["performed"], report["blocks"], report["denied"]) == (1, 2, False)

            for fmt, suffix in [("json", "json"), ("svg", "svg"), ("ical", "ics")]:
                export(fmt)
                path, = (profile / "exports/timeline").glob(f"*.{suffix}")
                content = path.read_bytes()
                if fmt == "json":
                    payload = json.loads(content)
                    assert payload["blocks"] == view["blocks"]
                    assert payload["total_duration_sec"] == 3.875
                elif fmt == "svg":
                    titles = ET.fromstring(content).findall(".//{*}rect/{*}title")
                    assert [title.text.split(" | ")[0] for title in titles] == [b["start_time"] for b in view["blocks"]]
                else:
                    assert b"DTSTART:20261025T000000Z\r\nDTEND:20261026T000000Z" in content
                    assert b"DTSTART:20261026T000000Z\r\nDTEND:20261027T000000Z" in content
                    export("ical")
                    retry, = set((profile / "exports/timeline").glob("*.ics")) - {path}
                    repeated = retry.read_bytes()
                    assert [line for line in repeated.split(b"\r\n") if not line.startswith(b"DTSTAMP:")] == [
                        line for line in content.split(b"\r\n") if not line.startswith(b"DTSTAMP:")
                    ]
            assert journal.read_bytes() == original
        finally:
            backend.close()
