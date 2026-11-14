"""Параллельный экспорт и purge через изолированный IPC и synthetic keychain."""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path

import pytest
from test_plaintext_export_integration import IsolatedBackend


@pytest.mark.parametrize("fmt,ext", [("ical", "ics"), ("json", "json"), ("svg", "svg")])
@pytest.mark.parametrize("encrypted", [False, True])
def test_timeline_export_cannot_publish_history_after_purge(fmt, ext, encrypted):
    with tempfile.TemporaryDirectory(prefix="ear-tpurge-", dir="/tmp") as directory:
        backend = IsolatedBackend(Path(directory).resolve(), crypto_key=os.urandom(32))
        try:
            backend.start()
            if not encrypted:
                assert backend.control("timeline_plaintext_profile")
            assert backend.call("add_history_item", text="syntheticpurgecheck")["ok"] is True
            params = {"plaintext_export": backend.grant()} if encrypted else {}
            assert backend.control("timeline_arm")
            with ThreadPoolExecutor(max_workers=1) as executor:
                export = executor.submit(backend.call, f"export_timeline_{fmt}", **params)
                try:
                    assert backend.control("timeline_barrier")
                    purge = backend.call("purge_all_data", confirm=True)
                    assert purge["ok"] is True and purge["result"]["complete"] is True
                finally:
                    backend.control("timeline_release")
                response = export.result(timeout=30)
            assert response["result"]["reason"] == "timeline_purge_in_progress"
            assert response["result"]["path"] is None
            assert not list((backend.root / "profile/exports/timeline").glob(f"*.{ext}"))
            assert not (backend.root / "profile/timeline/ical-namespace").exists()
            params = {"plaintext_export": backend.grant()} if encrypted else {}
            new_export = backend.call(f"export_timeline_{fmt}", **params)
            assert new_export["ok"] is True and new_export["result"]["blocks"] == 0
        finally:
            backend.close()


def test_purge_waits_for_inflight_publication_and_removes_output():
    with tempfile.TemporaryDirectory(prefix="ear-twrite-", dir="/tmp") as directory:
        backend = IsolatedBackend(Path(directory).resolve(), crypto_key=os.urandom(32))
        try:
            backend.start()
            assert backend.call("add_history_item", text="syntheticpurgecheck")["ok"]
            context = backend.grant()
            assert backend.control("timeline_write_arm")
            with ThreadPoolExecutor(max_workers=2) as executor:
                export = executor.submit(backend.call, "export_timeline_ical", plaintext_export=context)
                try:
                    assert backend.control("timeline_write_barrier")
                    purge = executor.submit(backend.call, "purge_all_data", confirm=True)
                    assert backend.control("purge_requested")
                    with pytest.raises(TimeoutError):
                        purge.result(timeout=0.3)
                finally:
                    backend.control("timeline_write_release")
                assert export.result(timeout=30)["result"]["blocks"] == 1
                assert purge.result(timeout=30)["result"]["complete"] is True
            assert not list((backend.root / "profile/exports/timeline").glob("*.ics"))
        finally:
            backend.close()
