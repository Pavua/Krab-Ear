import threading
import json
from core.normalization_profiles import NormalizationProfileRegistry


def test_normalization_profiles_concurrent(tmp_path):
    registry = NormalizationProfileRegistry(data_dir=tmp_path)

    def add_profiles():
        for i in range(20):
            registry.add_profile(f"prof_add_{i}", [f"rule_{i}"])

    def remove_profiles():
        # First add them so we can remove
        for i in range(20):
            registry.add_profile(f"prof_rem_{i}", [f"rule_{i}"])
        for i in range(20):
            registry.remove_profile(f"prof_rem_{i}")

    t1 = threading.Thread(target=add_profiles)
    t2 = threading.Thread(target=remove_profiles)

    t1.start()
    t2.start()

    t1.join()
    t2.join()

    # Verify file is not corrupted
    custom_path = tmp_path / "normalization_profiles.json"
    if custom_path.exists():
        data = json.loads(custom_path.read_text())
        assert isinstance(data, list)

    for i in range(20):
        assert registry.get_profile(f"prof_add_{i}") is not None
        assert registry.get_profile(f"prof_rem_{i}") is None
