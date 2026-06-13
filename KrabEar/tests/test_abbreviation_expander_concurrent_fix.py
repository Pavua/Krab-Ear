import threading
import tempfile
from pathlib import Path
from core.abbreviation_expander import AbbreviationExpander


def test_abbreviation_expander_concurrent_save():
    with tempfile.TemporaryDirectory() as tmpdir:
        expander = AbbreviationExpander(data_dir=Path(tmpdir))

        errors = []

        def worker_add():
            try:
                for i in range(100):
                    expander.add_abbreviation(f"abbr{i}", f"expansion{i}")
            except Exception as e:
                errors.append(e)

        def worker_remove():
            try:
                for i in range(100):
                    expander.remove_abbreviation(f"abbr{i}")
            except Exception as e:
                errors.append(e)

        def worker_save():
            try:
                for _ in range(100):
                    expander._save_custom()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=worker_add)
        t2 = threading.Thread(target=worker_remove)
        t3 = threading.Thread(target=worker_save)

        t1.start()
        t2.start()
        t3.start()

        t1.join()
        t2.join()
        t3.join()

        assert not errors, f"Errors occurred: {errors}"
