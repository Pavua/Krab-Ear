import threading
from backend.recap_scheduler import RecapScheduler
from unittest.mock import MagicMock


def test_recap_scheduler_atomic(tmp_path):
    store_mock = MagicMock()
    digest_mock = MagicMock()
    email_mock = MagicMock()
    scheduler = RecapScheduler(email_mock, digest_mock, store_mock, data_dir=tmp_path)

    scheduler._save_state({"last_sent_date": "2026-06-13"})
    state = scheduler._load_state()
    assert state.get("last_sent_date") == "2026-06-13"

    # Read under lock test
    def reader():
        for _ in range(50):
            scheduler.get_status()

    def writer():
        for i in range(50):
            with scheduler._lock:
                scheduler._save_state({"send_count": i})

    t1 = threading.Thread(target=reader)
    t2 = threading.Thread(target=writer)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # If no exception, it's successful
