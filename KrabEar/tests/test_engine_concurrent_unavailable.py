import threading
import time
from core.engine import AudioEngine


def test_engine_concurrent_unavailable_models():
    engine = AudioEngine(skip_gigaam_warmup=True)
    engine._unavailable_models["test_model"] = time.monotonic() - 1000  # expired

    errors = []

    def worker_check():
        try:
            for _ in range(100):
                engine._is_model_unavailable("test_model")
        except Exception as e:
            errors.append(e)

    def worker_expire():
        try:
            for _ in range(100):
                engine._is_model_unavailable("test_model")
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=worker_check)
    t2 = threading.Thread(target=worker_expire)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"Errors occurred: {errors}"
