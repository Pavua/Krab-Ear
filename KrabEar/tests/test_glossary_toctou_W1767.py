"""W1767 — Тест атомарности glossary TOCTOU через SettingsService._save_lock.

Сценарий:
  До исправления handle_set_translation_glossary_item делал read→mutate→store.save_settings
  без удержания SettingsService._save_lock.  Параллельный set_settings мог перезаписать
  translation_glossary и потерять одно из двух добавленных слов (lost-update).

После исправления весь read-modify-write выполняется под _save_lock SettingsService —
оба обновления сохраняются атомарно.

Тесты:
  1. no_lost_update_sequential — два последовательных вызова set_glossary_item сохраняют оба ключа.
  2. no_lost_update_locked — симулируем конкурентный set_settings, оба ключа сохраняются.
  3. fallback_without_settings_svc — без инъекции settings_svc поведение graceful (store.save_settings вызывается).
  4. locked_remove_persists — _locked_remove_glossary_item атомарно удаляет ключ.
  5. locked_set_returns_correct_count — _locked_set_glossary_item возвращает актуальный count.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings_service import SettingsService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from backend.translation_service import TranslationService  # noqa: E402
from backend.translator import TranslationResult  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────────────────────

def _make_svc_with_settings_svc(
    data_dir: Path,
    initial_glossary: dict | None = None,
) -> tuple[TranslationService, SettingsService]:
    """Создаёт TranslationService с реальным SettingsService, разделяя _save_lock."""
    store = StateStore(data_dir)
    # Пишем начальный глоссарий в settings.json
    if initial_glossary is not None:
        store.save_settings({"translation_glossary": initial_glossary})

    settings_svc = SettingsService(store=store)

    translator_mock = MagicMock()
    translator_mock.translate.return_value = TranslationResult(
        text="translated", status="ok",
        source_lang="ru", target_lang="es",
        mode="ru_to_es", engine="fake",
    )

    svc = TranslationService(
        translator=translator_mock,
        store=store,
        cached_settings=settings_svc.cached_settings,
        invalidate_settings_cache=settings_svc.invalidate_cache,
        settings_svc=settings_svc,
    )
    return svc, settings_svc


# ─────────────────────────────────────────────────────────────────────────────
# Тесты
# ─────────────────────────────────────────────────────────────────────────────

class GlossaryTOCTOWTest(unittest.TestCase):
    """W1767: Два concurrent или sequential glossary-update'а — оба сохраняются."""

    def setUp(self) -> None:
        self._tmpdir = TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.data_dir = Path(self._tmpdir.name) / "data"

    def test_no_lost_update_sequential(self) -> None:
        """Два последовательных set_glossary_item — оба ключа сохраняются."""
        svc, settings_svc = _make_svc_with_settings_svc(self.data_dir)

        r1 = svc.handle_set_translation_glossary_item({"source": "Краб", "target": "Krab"})
        r2 = svc.handle_set_translation_glossary_item({"source": "Ухо", "target": "Ear"})

        self.assertTrue(r1["updated"])
        self.assertTrue(r2["updated"])
        self.assertEqual(r2["count"], 2)

        # Финальный глоссарий содержит оба ключа
        final = settings_svc.cached_settings().get("translation_glossary", {})
        self.assertIn("Краб", final, "Первый ключ потерян (lost-update)")
        self.assertIn("Ухо", final, "Второй ключ потерян (lost-update)")
        self.assertEqual(final["Краб"], "Krab")
        self.assertEqual(final["Ухо"], "Ear")

    def test_no_lost_update_locked(self) -> None:
        """Конкурентный set_settings не теряет glossary-запись.

        Сценарий TOCTOU (до фикса):
          Поток A читает glossary={}, добавляет "Краб".
          Поток B читает glossary={} (тот же snapshot), добавляет "Ухо".
          Поток A пишет glossary={"Краб": "Krab"} → B перезаписывает → {"Ухо": "Ear"}.
          Итог: "Краб" потерян.

        После фикса оба потока сериализуются через _save_lock:
          Итог: {"Краб": "Krab", "Ухо": "Ear"}.
        """
        svc, settings_svc = _make_svc_with_settings_svc(self.data_dir)

        errors: list[Exception] = []

        def add_krab() -> None:
            try:
                svc.handle_set_translation_glossary_item({"source": "Краб", "target": "Krab"})
            except Exception as exc:
                errors.append(exc)

        def add_uho() -> None:
            try:
                svc.handle_set_translation_glossary_item({"source": "Ухо", "target": "Ear"})
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=add_krab)
        t2 = threading.Thread(target=add_uho)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(errors, [], f"Exceptions in threads: {errors}")

        settings_svc.invalidate_cache()
        final = settings_svc.cached_settings().get("translation_glossary", {})
        self.assertIn("Краб", final, "W1767: lost-update — 'Краб' потерян при concurrent записи")
        self.assertIn("Ухо", final, "W1767: lost-update — 'Ухо' потерян при concurrent записи")

    def test_fallback_without_settings_svc(self) -> None:
        """Без инъекции settings_svc — сервис работает через store.save_settings (graceful fallback)."""
        store_mock = MagicMock()
        settings_cell: list[dict] = [{"translation_glossary": {}}]

        def save_settings(s: dict) -> dict:
            settings_cell[0] = dict(s)
            return dict(s)

        store_mock.save_settings.side_effect = save_settings

        svc = TranslationService(
            translator=MagicMock(),
            store=store_mock,
            cached_settings=lambda: dict(settings_cell[0]),
            invalidate_settings_cache=lambda: None,
            settings_svc=None,  # без инъекции
        )

        result = svc.handle_set_translation_glossary_item({"source": "test", "target": "тест"})
        self.assertTrue(result["updated"])
        # Fallback вызывает store.save_settings напрямую
        store_mock.save_settings.assert_called_once()
        saved = store_mock.save_settings.call_args[0][0]
        self.assertEqual(saved["translation_glossary"]["test"], "тест")

    def test_locked_remove_persists(self) -> None:
        """_locked_remove_glossary_item атомарно удаляет ключ, не затрагивая остальные."""
        svc, settings_svc = _make_svc_with_settings_svc(
            self.data_dir,
            initial_glossary={"Краб": "Krab", "Ухо": "Ear", "Голос": "Voice"},
        )

        result = svc.handle_remove_translation_glossary_item({"source": "Ухо"})
        self.assertTrue(result["removed"])
        self.assertEqual(result["count"], 2)

        settings_svc.invalidate_cache()
        final = settings_svc.cached_settings().get("translation_glossary", {})
        self.assertNotIn("Ухо", final, "Ключ должен быть удалён")
        self.assertIn("Краб", final, "Соседний ключ должен остаться")
        self.assertIn("Голос", final, "Второй соседний ключ должен остаться")

    def test_locked_set_returns_correct_count(self) -> None:
        """_locked_set_glossary_item возвращает актуальный размер глоссария."""
        svc, settings_svc = _make_svc_with_settings_svc(
            self.data_dir,
            initial_glossary={"Краб": "Krab"},
        )

        r = svc.handle_set_translation_glossary_item({"source": "Ухо", "target": "Ear"})
        self.assertEqual(r["count"], 2, "count должен отражать размер после добавления")
        self.assertTrue(r["updated"])

    def test_many_concurrent_entries_all_persist(self) -> None:
        """20 конкурентных потоков добавляют уникальные ключи — все сохраняются.

        Расширенная версия no_lost_update_locked для стресс-проверки сериализации.
        """
        svc, settings_svc = _make_svc_with_settings_svc(self.data_dir)
        n = 20
        errors: list[Exception] = []

        def add(idx: int) -> None:
            try:
                svc.handle_set_translation_glossary_item({
                    "source": f"слово_{idx}",
                    "target": f"word_{idx}",
                })
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        settings_svc.invalidate_cache()
        final = settings_svc.cached_settings().get("translation_glossary", {})
        missing = [f"слово_{i}" for i in range(n) if f"слово_{i}" not in final]
        self.assertEqual(
            missing, [],
            f"W1767: lost-update — {len(missing)} ключей потеряно: {missing[:5]}...",
        )


if __name__ == "__main__":
    unittest.main()
