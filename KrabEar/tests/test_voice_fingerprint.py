"""Тесты voice fingerprint matching — cross-recording идентификация спикеров.

Все тесты используют mock-эмбеддинги (numpy arrays). pyannote.audio не требуется.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

# Настройка путей для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend.speaker_manager import SpeakerManager, _cosine_similarity  # noqa: E402


def _make_emb(dim: int = 512, seed: int = 0) -> np.ndarray:
    """Создаёт нормализованный вектор с фиксированным seed."""
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    v /= np.linalg.norm(v)
    return v


def _close_emb(base: np.ndarray, noise: float = 0.05) -> np.ndarray:
    """Возвращает вектор, похожий на base (малый шум)."""
    rng = np.random.default_rng(42)
    perturbed = base + rng.standard_normal(len(base)).astype(np.float32) * noise
    perturbed /= np.linalg.norm(perturbed)
    return perturbed


def _far_emb(base: np.ndarray) -> np.ndarray:
    """Возвращает вектор, антипараллельный base (максимально непохожий)."""
    v = -base.copy()
    v /= np.linalg.norm(v)
    return v


class TestCosineSimilarity(unittest.TestCase):
    """Юнит-тесты для вспомогательной функции _cosine_similarity."""

    def test_identical_vectors(self) -> None:
        v = _make_emb(seed=1)
        self.assertAlmostEqual(_cosine_similarity(v, v), 1.0, places=5)

    def test_opposite_vectors(self) -> None:
        v = _make_emb(seed=2)
        self.assertAlmostEqual(_cosine_similarity(v, -v), -1.0, places=5)

    def test_zero_vector_returns_zero(self) -> None:
        v = _make_emb(seed=3)
        z = np.zeros_like(v)
        self.assertEqual(_cosine_similarity(v, z), 0.0)
        self.assertEqual(_cosine_similarity(z, v), 0.0)

    def test_orthogonal_vectors_close_to_zero(self) -> None:
        v1 = np.zeros(4, dtype=np.float32)
        v2 = np.zeros(4, dtype=np.float32)
        v1[0] = 1.0
        v2[1] = 1.0
        self.assertAlmostEqual(_cosine_similarity(v1, v2), 0.0, places=5)


class TestFindMatchingSpeaker(unittest.TestCase):
    """Тест 1: same speaker -> match / different -> no match / threshold respected."""

    def setUp(self) -> None:
        self.mgr = SpeakerManager()

    def _register(self, emb: np.ndarray, name: str = "TestUser") -> str:
        return self.mgr.register_speaker(name, emb)

    def test_same_speaker_matches(self) -> None:
        """Эмбеддинг того же спикера (малый шум) -> match выше threshold."""
        base = _make_emb(seed=10)
        spk_id = self._register(base, "Паша")
        query = _close_emb(base, noise=0.02)
        result = self.mgr.find_matching_speaker(query, threshold=0.75)
        self.assertEqual(result, spk_id)

    def test_different_speaker_no_match(self) -> None:
        """Противоположный вектор -> score << threshold -> None."""
        base = _make_emb(seed=11)
        self._register(base, "Паша")
        far = _far_emb(base)
        result = self.mgr.find_matching_speaker(far, threshold=0.75)
        self.assertIsNone(result)

    def test_threshold_respected_high(self) -> None:
        """При threshold=0.99 умеренно похожий вектор не матчится."""
        base = _make_emb(seed=12)
        self._register(base, "Маша")
        query = _close_emb(base, noise=0.30)
        score = _cosine_similarity(base, query)
        result = self.mgr.find_matching_speaker(query, threshold=0.99)
        if score < 0.99:
            self.assertIsNone(result)

    def test_threshold_zero_always_matches(self) -> None:
        """При threshold=0.0 близкий вектор матчится."""
        base = _make_emb(seed=13)
        spk_id = self._register(base, "Кто-то")
        query = _close_emb(base, noise=0.1)
        result = self.mgr.find_matching_speaker(query, threshold=0.0)
        self.assertEqual(result, spk_id)

    def test_empty_fingerprints_returns_none(self) -> None:
        """Нет зарегистрированных спикеров -> None."""
        query = _make_emb(seed=20)
        result = self.mgr.find_matching_speaker(query)
        self.assertIsNone(result)

    def test_zero_embedding_returns_none(self) -> None:
        """Нулевой эмбеддинг -> None (невалидный вектор)."""
        base = _make_emb(seed=21)
        self._register(base, "Тест")
        zero = np.zeros(512, dtype=np.float32)
        result = self.mgr.find_matching_speaker(zero)
        self.assertIsNone(result)


class TestAutoRegister(unittest.TestCase):
    """Тест 3: авто-регистрация нового спикера."""

    def setUp(self) -> None:
        self.mgr = SpeakerManager()

    def test_auto_register_creates_speaker(self) -> None:
        """Первый неизвестный эмбеддинг -> авто-создаётся Speaker_0."""
        emb = _make_emb(seed=30)
        spk_id = self.mgr.resolve_speaker_for_segment(
            "SPEAKER_00", emb, threshold=0.75, auto_register=True
        )
        self.assertEqual(spk_id, "Speaker_0")
        fps = self.mgr.get_all_fingerprints()
        self.assertIn("Speaker_0", fps)

    def test_auto_register_increments_counter(self) -> None:
        """Каждый новый неизвестный спикер получает новый ID."""
        emb0 = _make_emb(seed=31)
        emb1 = _make_emb(seed=32)
        id0 = self.mgr.resolve_speaker_for_segment(
            "SPEAKER_00", emb0, threshold=0.75, auto_register=True
        )
        id1 = self.mgr.resolve_speaker_for_segment(
            "SPEAKER_01", emb1, threshold=0.75, auto_register=True
        )
        self.assertEqual(id0, "Speaker_0")
        self.assertEqual(id1, "Speaker_1")

    def test_auto_register_disabled_returns_local_id(self) -> None:
        """auto_register=False -> возвращает исходный local_speaker_id."""
        emb = _make_emb(seed=33)
        result = self.mgr.resolve_speaker_for_segment(
            "SPEAKER_00", emb, threshold=0.75, auto_register=False
        )
        self.assertEqual(result, "SPEAKER_00")
        self.assertEqual(self.mgr.get_all_fingerprints(), {})

    def test_known_speaker_matched_not_duplicated(self) -> None:
        """Если спикер уже зарегистрирован — match, не создаётся дубль."""
        base = _make_emb(seed=34)
        first_id = self.mgr.register_speaker("Паша", base)
        query = _close_emb(base, noise=0.02)
        matched_id = self.mgr.resolve_speaker_for_segment(
            "SPEAKER_00", query, threshold=0.75, auto_register=True
        )
        self.assertEqual(matched_id, first_id)
        self.assertEqual(len(self.mgr.get_all_fingerprints()), 1)


class TestRenameAndDelete(unittest.TestCase):
    """Тест 4: rename / delete fingerprint."""

    def setUp(self) -> None:
        self.mgr = SpeakerManager()

    def test_rename_speaker(self) -> None:
        """Переименование: set_alias меняет отображаемое имя."""
        emb = _make_emb(seed=40)
        spk_id = self.mgr.register_speaker("OldName", emb)
        self.mgr.set_alias(spk_id, "NewName")
        self.assertEqual(self.mgr.get_alias(spk_id), "NewName")

    def test_delete_fingerprint_removes_it(self) -> None:
        """delete_fingerprint удаляет вектор и делает спикера ненаходимым."""
        emb = _make_emb(seed=41)
        spk_id = self.mgr.register_speaker("Маша", emb)
        deleted = self.mgr.delete_fingerprint(spk_id)
        self.assertTrue(deleted)
        query = _close_emb(emb, noise=0.01)
        result = self.mgr.find_matching_speaker(query, threshold=0.5)
        self.assertIsNone(result)

    def test_delete_nonexistent_fingerprint_returns_false(self) -> None:
        result = self.mgr.delete_fingerprint("Speaker_999")
        self.assertFalse(result)

    def test_delete_alias_does_not_remove_fingerprint(self) -> None:
        """remove_alias не удаляет фингерпринт."""
        emb = _make_emb(seed=42)
        spk_id = self.mgr.register_speaker("Паша", emb)
        self.mgr.remove_alias(spk_id)
        self.assertIsNone(self.mgr.get_alias(spk_id))
        self.assertIn(spk_id, self.mgr.get_all_fingerprints())


class TestPersistence(unittest.TestCase):
    """Тест 5: persistence — сохранение и загрузка фингерпринтов."""

    def test_fingerprints_saved_and_reloaded(self) -> None:
        """Фингерпринты сохраняются в файл и корректно загружаются."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr1 = SpeakerManager(data_dir=tmpdir)
            emb = _make_emb(seed=50)
            spk_id = mgr1.register_speaker("Паша", emb)
            mgr2 = SpeakerManager(data_dir=tmpdir)
            fps = mgr2.get_all_fingerprints()
            self.assertIn(spk_id, fps)
            restored = np.array(fps[spk_id], dtype=np.float32)
            sim = _cosine_similarity(emb, restored)
            self.assertGreater(sim, 0.9999)

    def test_fingerprints_file_created(self) -> None:
        """Файл speaker_fingerprints.json создаётся при register_speaker."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SpeakerManager(data_dir=tmpdir)
            emb = _make_emb(seed=51)
            mgr.register_speaker("Тест", emb)
            fp_path = Path(tmpdir) / "speaker_fingerprints.json"
            self.assertTrue(fp_path.exists())
            data = json.loads(fp_path.read_text(encoding="utf-8"))
            self.assertIn("Speaker_0", data)

    def test_counter_restored_from_file(self) -> None:
        """После reload счётчик авто-именования продолжает с правильного места."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr1 = SpeakerManager(data_dir=tmpdir)
            mgr1.register_speaker("А", _make_emb(seed=52))
            mgr1.register_speaker("Б", _make_emb(seed=53))
            mgr2 = SpeakerManager(data_dir=tmpdir)
            new_id = mgr2.register_speaker("В", _make_emb(seed=54))
            self.assertEqual(new_id, "Speaker_2")

    def test_match_works_after_reload(self) -> None:
        """find_matching_speaker работает корректно после reload."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = _make_emb(seed=55)
            mgr1 = SpeakerManager(data_dir=tmpdir)
            spk_id = mgr1.register_speaker("Паша", base)
            mgr2 = SpeakerManager(data_dir=tmpdir)
            query = _close_emb(base, noise=0.02)
            matched = mgr2.find_matching_speaker(query, threshold=0.75)
            self.assertEqual(matched, spk_id)


class TestDisabledFlag(unittest.TestCase):
    """Тест 6: VOICE_FINGERPRINT_ENABLED=False — фича отключена."""

    def test_disabled_flag_in_settings(self) -> None:
        """VOICE_FINGERPRINT_ENABLED по умолчанию False в Settings."""
        from core.config import settings  # noqa: PLC0415

        self.assertFalse(settings.VOICE_FINGERPRINT_ENABLED)

    def test_disabled_flag_in_default_settings(self) -> None:
        """voice_fingerprint_enabled по умолчанию False в DEFAULT_SETTINGS."""
        from core.config import DEFAULT_SETTINGS  # noqa: PLC0415

        self.assertFalse(DEFAULT_SETTINGS["voice_fingerprint_enabled"])

    def test_disabled_means_resolve_returns_local_id(self) -> None:
        """Когда auto_register=False (имитирует отключённую фичу) — без регистрации."""
        from core.config import settings  # noqa: PLC0415

        self.assertFalse(settings.VOICE_FINGERPRINT_ENABLED)
        mgr = SpeakerManager()
        emb = _make_emb(seed=60)
        result = mgr.resolve_speaker_for_segment(
            "SPEAKER_00", emb, threshold=0.75, auto_register=False
        )
        self.assertEqual(result, "SPEAKER_00")

    def test_default_threshold_in_settings(self) -> None:
        """VOICE_FINGERPRINT_MATCH_THRESHOLD по умолчанию 0.75."""
        from core.config import settings  # noqa: PLC0415

        self.assertAlmostEqual(settings.VOICE_FINGERPRINT_MATCH_THRESHOLD, 0.75)


class TestRegisterSpeakerAPI(unittest.TestCase):
    """Тест 7: register_speaker — сохраняет name + embedding вместе."""

    def setUp(self) -> None:
        self.mgr = SpeakerManager()

    def test_register_stores_alias(self) -> None:
        emb = _make_emb(seed=70)
        spk_id = self.mgr.register_speaker("Паша", emb)
        self.assertEqual(self.mgr.get_alias(spk_id), "Паша")

    def test_register_no_name_no_alias(self) -> None:
        emb = _make_emb(seed=71)
        spk_id = self.mgr.register_speaker("", emb)
        self.assertIsNone(self.mgr.get_alias(spk_id))
        self.assertIn(spk_id, self.mgr.get_all_fingerprints())

    def test_register_then_find(self) -> None:
        emb = _make_emb(seed=72)
        spk_id = self.mgr.register_speaker("Маша", emb)
        found = self.mgr.find_matching_speaker(emb, threshold=0.99)
        self.assertEqual(found, spk_id)

    def test_register_none_embedding_raises(self) -> None:
        with self.assertRaises((ValueError, AttributeError)):
            self.mgr.register_speaker("Паша", None)  # type: ignore[arg-type]

    def test_ipc_register_speaker(self) -> None:
        """IPC handle_register_speaker корректно регистрирует спикера."""
        emb = _make_emb(seed=73)
        result = self.mgr.handle_register_speaker({
            "name": "Паша",
            "embedding": emb.tolist(),
        })
        self.assertIn("speaker_id", result)
        self.assertEqual(result["name"], "Паша")


class TestSelectBestMatch(unittest.TestCase):
    """Тест 8: выбирается лучший совпадающий спикер среди нескольких."""

    def setUp(self) -> None:
        self.mgr = SpeakerManager()

    def test_best_match_selected_from_multiple(self) -> None:
        """Из нескольких зарегистрированных — выбирается наиближайший."""
        emb_a = _make_emb(seed=80)
        emb_b = _make_emb(seed=81)
        id_a = self.mgr.register_speaker("А", emb_a)
        id_b = self.mgr.register_speaker("Б", emb_b)
        query = _close_emb(emb_a, noise=0.02)
        result = self.mgr.find_matching_speaker(query, threshold=0.5)
        self.assertEqual(result, id_a)
        query_b = _close_emb(emb_b, noise=0.02)
        result_b = self.mgr.find_matching_speaker(query_b, threshold=0.5)
        self.assertEqual(result_b, id_b)

    def test_update_fingerprint_ema(self) -> None:
        """update_fingerprint адаптирует вектор через EMA."""
        base = _make_emb(seed=82)
        spk_id = self.mgr.register_speaker("Паша", base)
        new_emb = _close_emb(base, noise=0.1)
        updated = self.mgr.update_fingerprint(spk_id, new_emb, alpha=0.1)
        self.assertTrue(updated)
        query = _close_emb(base, noise=0.02)
        result = self.mgr.find_matching_speaker(query, threshold=0.75)
        self.assertEqual(result, spk_id)

    def test_update_nonexistent_fingerprint_returns_false(self) -> None:
        new_emb = _make_emb(seed=83)
        result = self.mgr.update_fingerprint("Speaker_999", new_emb)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
