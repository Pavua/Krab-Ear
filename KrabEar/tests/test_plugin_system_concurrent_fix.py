import threading
import time
from backend.plugin_system import PluginManager


def test_plugin_system_concurrent_call_hook():
    manager = PluginManager()

    # Mock some loaded plugins
    class DummyPlugin:
        def on_transcribe(self, payload):
            return payload + 1

    # Pre-populate
    for i in range(100):
        manager._loaded[f"plugin_{i}"] = DummyPlugin()

    errors = []

    def worker_iterate():
        try:
            for _ in range(200):
                manager.call_hook("on_transcribe", 0)
        except Exception as e:
            errors.append(e)

    def worker_mutate():
        try:
            for i in range(200):
                name = f"plugin_{i % 100}"
                # simulate unload
                with manager._lock:
                    if name in manager._loaded:
                        del manager._loaded[name]
                time.sleep(0.001)
                with manager._lock:
                    manager._loaded[name] = DummyPlugin()
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=worker_iterate)
    t2 = threading.Thread(target=worker_mutate)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"Errors occurred: {errors}"
