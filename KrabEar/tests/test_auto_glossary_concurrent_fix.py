import threading
from unittest import mock
from core.auto_glossary import AutoGlossaryBuilder


def test_auto_glossary_concurrent_build_invalidate():
    store = mock.Mock()
    # Mock get_history_page to return some dummy items
    store.get_history_page.return_value = ([{"ts": "2026-01-01T00:00:00Z", "text": "test"}], None)

    extractor = mock.Mock()
    extractor.extract_terms.return_value = []

    builder = AutoGlossaryBuilder(store=store, term_extractor=extractor)

    errors = []

    def worker_build():
        try:
            for _ in range(50):
                builder.build(force=True)
        except Exception as e:
            errors.append(e)

    def worker_invalidate():
        try:
            for _ in range(50):
                builder.invalidate()
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=worker_build)
    t2 = threading.Thread(target=worker_invalidate)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"Errors occurred: {errors}"
