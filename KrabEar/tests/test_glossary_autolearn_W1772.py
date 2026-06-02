"""W1772 — Тесты защиты глоссария auto-learn от unbounded growth и TOCTOU.

Покрывает два исправления в backend/glossary_auto_learn.py:

Fix 1 — MED unbounded glossary (строки ~372-409):
  apply_glossary_suggestions теперь проверяет MAX_GLOSSARY_ENTRIES (500)
  и MAX_TERM_BYTES (200) перед добавлением пары в глоссарий.
  - Попытка добавить 501-ю запись → запись skipped, словарь не растёт.
  - Слишком длинный source/target → пара skipped.

Fix 2 — MED TOCTOU lost-update (строки ~351-355):
  Весь read-modify-write выполняется под модульным _apply_lock.
  - Два конкурентных вызова apply_glossary_suggestions сохраняют оба ключа.

Fail-before / pass-after:
  - До фикса: 501-я запись и over-long термин добавлялись без ограничений
    (unbounded glossary).
  - До фикса: два конкурентных apply могли потерять одно обновление (TOCTOU).
  - После фикса: оба сценария корректно отклоняются / сериализуются.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.glossary_auto_learn import (  # noqa: E402
    GlossaryAutoLearnService,
    MAX_GLOSSARY_ENTRIES,
    MAX_TERM_BYTES,
)


# ──────────────────────────────────────────────────────────────
# Вспомогательные фикстуры
# ──────────────────────────────────────────────────────────────


class _FakeStore:
    """Минимальный stub для StateStore с поддержкой инспекции вызовов."""

    def __init__(self, initial_glossary: Dict[str, str] | None = None) -> None:
        self._settings: Dict[str, Any] = {
            "translation_glossary": dict(initial_glossary or {})
        }
        self.save_count = 0

    def get_history_page(self, cursor=None, limit=500):
        return [], None

    def save_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        # Эмулируем персистенцию: обновляем внутреннее состояние
        self._settings = dict(settings)
        self.save_count += 1
        return dict(settings)


def _make_svc(
    initial_glossary: Dict[str, str] | None = None,
) -> tuple[GlossaryAutoLearnService, _FakeStore]:
    store = _FakeStore(initial_glossary=initial_glossary)
    svc = GlossaryAutoLearnService(
        store=store,
        cached_settings=lambda: dict(store._settings),
        invalidate_settings_cache=lambda: None,
    )
    return svc, store


def _make_suggestions(ids_and_targets: Dict[str, str]) -> list:
    """Преобразует {source_lo: target} в список dicts для параметра suggestions."""
    return [
        {"source_term": k, "target_term": v}
        for k, v in ids_and_targets.items()
    ]


# ──────────────────────────────────────────────────────────────
# Fix 1 — Unbounded glossary (лимит числа записей)
# ──────────────────────────────────────────────────────────────


class TestApplyGlossaryEntryCapW1772(unittest.TestCase):
    """MED Fix 1: apply_glossary_suggestions не превышает MAX_GLOSSARY_ENTRIES."""

    def test_overflow_entry_skipped_not_added(self) -> None:
        """501-я запись при уже полном глоссарии пропускается (skipped=1, applied=0).

        До фикса: запись добавлялась → словарь рос без ограничений.
        После фикса: запись отклоняется, save_settings НЕ вызывается.
        """
        full_glossary = {f"src{i}": f"tgt{i}" for i in range(MAX_GLOSSARY_ENTRIES)}
        self.assertEqual(len(full_glossary), 500)
        svc, store = _make_svc(initial_glossary=full_glossary)

        result = svc.handle_apply_glossary_suggestions({
            "selected_ids": ["overflow_term"],
            "suggestions": [{"source_term": "overflow_term", "target_term": "target"}],
        })

        self.assertEqual(result["applied"], 0, "Не должно быть применений при полном глоссарии")
        self.assertEqual(result["skipped"], 1, "Один термин должен быть пропущен")
        self.assertLessEqual(result["total_glossary"], MAX_GLOSSARY_ENTRIES,
                             "Размер глоссария не должен превышать лимит")
        self.assertEqual(store.save_count, 0, "save_settings не должен вызываться")

        # Убеждаемся, что переполнение реально не попало в словарь
        final = store._settings.get("translation_glossary", {})
        self.assertNotIn("overflow_term", final)
        self.assertEqual(len(final), MAX_GLOSSARY_ENTRIES)

    def test_multiple_overflow_all_skipped(self) -> None:
        """Несколько избыточных записей — все пропускаются."""
        full_glossary = {f"src{i}": f"tgt{i}" for i in range(MAX_GLOSSARY_ENTRIES)}
        svc, store = _make_svc(initial_glossary=full_glossary)

        result = svc.handle_apply_glossary_suggestions({
            "selected_ids": ["extra1", "extra2", "extra3"],
            "suggestions": [
                {"source_term": "extra1", "target_term": "e1"},
                {"source_term": "extra2", "target_term": "e2"},
                {"source_term": "extra3", "target_term": "e3"},
            ],
        })

        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped"], 3)
        self.assertEqual(store.save_count, 0)

    def test_partial_overflow_stops_at_cap(self) -> None:
        """При частично заполненном глоссарии добавляются только те, что вмещаются."""
        # Глоссарий на 498 записей — ещё 2 места
        near_full = {f"src{i}": f"tgt{i}" for i in range(MAX_GLOSSARY_ENTRIES - 2)}
        svc, store = _make_svc(initial_glossary=near_full)

        suggestions = [{"source_term": f"new{i}", "target_term": f"nv{i}"}
                       for i in range(5)]
        result = svc.handle_apply_glossary_suggestions({
            "selected_ids": [f"new{i}" for i in range(5)],
            "suggestions": suggestions,
        })

        # Ровно 2 места — должно добавиться 2, остальные 3 пропущены
        self.assertEqual(result["applied"], 2)
        self.assertEqual(result["skipped"], 3)
        final = store._settings.get("translation_glossary", {})
        self.assertEqual(len(final), MAX_GLOSSARY_ENTRIES)

    def test_within_cap_all_applied(self) -> None:
        """В пределах лимита все записи добавляются (регрессия нормального поведения)."""
        svc, store = _make_svc()

        suggestions = [{"source_term": f"term{i}", "target_term": f"перевод{i}"}
                       for i in range(10)]
        result = svc.handle_apply_glossary_suggestions({
            "selected_ids": [f"term{i}" for i in range(10)],
            "suggestions": suggestions,
        })

        self.assertEqual(result["applied"], 10)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(store.save_count, 1)


# ──────────────────────────────────────────────────────────────
# Fix 1 — Unbounded glossary (лимит длины терминов)
# ──────────────────────────────────────────────────────────────


class TestApplyGlossaryTermLengthCapW1772(unittest.TestCase):
    """MED Fix 1: apply_glossary_suggestions не принимает слишком длинные термины."""

    def test_overlong_source_skipped(self) -> None:
        """Source длиннее MAX_TERM_BYTES → пара пропускается, save не вызывается.

        До фикса: длинный термин добавлялся без проверки.
        """
        long_source = "x" * (MAX_TERM_BYTES + 1)
        svc, store = _make_svc()

        result = svc.handle_apply_glossary_suggestions({
            "selected_ids": [long_source],
            "suggestions": [{"source_term": long_source, "target_term": "нормальный"}],
        })

        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(store.save_count, 0)
        self.assertNotIn(long_source, store._settings.get("translation_glossary", {}))

    def test_overlong_target_skipped(self) -> None:
        """Target длиннее MAX_TERM_BYTES → пара пропускается."""
        long_target = "y" * (MAX_TERM_BYTES + 1)
        svc, store = _make_svc()

        result = svc.handle_apply_glossary_suggestions({
            "selected_ids": ["good_source"],
            "suggestions": [{"source_term": "good_source", "target_term": long_target}],
        })

        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(store.save_count, 0)

    def test_cyrillic_counted_by_bytes(self) -> None:
        """Кириллица 2 байта/символ: 100 символов = 200 байт = ровно лимит → принимается.
        101 символ = 202 байта → отклоняется.
        """
        # Ровно на границе (200 байт) → допустимо
        on_edge = "а" * (MAX_TERM_BYTES // 2)
        self.assertEqual(len(on_edge.encode("utf-8")), MAX_TERM_BYTES)
        svc_ok, store_ok = _make_svc()
        result_ok = svc_ok.handle_apply_glossary_suggestions({
            "selected_ids": [on_edge],
            "suggestions": [{"source_term": on_edge, "target_term": "ok"}],
        })
        self.assertEqual(result_ok["applied"], 1)

        # Один лишний символ (202 байта) → отклоняется
        over = "а" * (MAX_TERM_BYTES // 2 + 1)
        self.assertGreater(len(over.encode("utf-8")), MAX_TERM_BYTES)
        svc_over, store_over = _make_svc()
        result_over = svc_over.handle_apply_glossary_suggestions({
            "selected_ids": [over],
            "suggestions": [{"source_term": over, "target_term": "ok"}],
        })
        self.assertEqual(result_over["applied"], 0)
        self.assertEqual(result_over["skipped"], 1)
        self.assertEqual(store_over.save_count, 0)

    def test_mixed_batch_good_and_bad(self) -> None:
        """В пакете: хорошие термины добавляются, плохие пропускаются."""
        long_src = "z" * (MAX_TERM_BYTES + 1)
        svc, store = _make_svc()

        result = svc.handle_apply_glossary_suggestions({
            "selected_ids": ["good1", long_src, "good2"],
            "suggestions": [
                {"source_term": "good1", "target_term": "g1"},
                {"source_term": long_src, "target_term": "g2"},
                {"source_term": "good2", "target_term": "g3"},
            ],
        })

        self.assertEqual(result["applied"], 2)
        self.assertEqual(result["skipped"], 1)
        final = store._settings.get("translation_glossary", {})
        self.assertIn("good1", final)
        self.assertIn("good2", final)
        self.assertNotIn(long_src, final)


# ──────────────────────────────────────────────────────────────
# Fix 2 — TOCTOU: сериализация concurrent apply_glossary_suggestions
# ──────────────────────────────────────────────────────────────


class TestApplyGlossaryTOCTOUW1772(unittest.TestCase):
    """MED Fix 2: два конкурентных apply_glossary_suggestions сохраняют оба ключа.

    Сценарий до фикса:
      Поток A читает glossary={}, добавляет "диагноз".
      Поток B читает glossary={} (тот же snapshot), добавляет "лечение".
      Поток A пишет {диагноз: x} → B перезаписывает {лечение: y}.
      Итог: "диагноз" потерян.

    После фикса (_apply_lock):
      Один из потоков ждёт, пока другой завершит read-modify-write.
      Итог: {диагноз: x, лечение: y}.
    """

    def test_two_concurrent_apply_no_lost_update(self) -> None:
        """Два конкурентных apply сохраняют оба ключа (нет потери обновления).

        До фикса: один из ключей мог быть потерян (TOCTOU lost-update).
        После фикса: _apply_lock сериализует оба обращения.
        """
        svc, store = _make_svc()
        errors: list = []

        def apply_first() -> None:
            try:
                svc.handle_apply_glossary_suggestions({
                    "selected_ids": ["диагноз"],
                    "suggestions": [{"source_term": "диагноз", "target_term": "diagnóstico"}],
                })
            except Exception as exc:
                errors.append(exc)

        def apply_second() -> None:
            try:
                svc.handle_apply_glossary_suggestions({
                    "selected_ids": ["лечение"],
                    "suggestions": [{"source_term": "лечение", "target_term": "tratamiento"}],
                })
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=apply_first)
        t2 = threading.Thread(target=apply_second)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(errors, [], f"Ошибки в потоках: {errors}")

        # Ключи сохраняются в нижнем регистре (sid_lo = sid.lower())
        final = store._settings.get("translation_glossary", {})
        self.assertIn("диагноз", final,
                      "W1772: 'диагноз' потерян при конкурентном apply (TOCTOU)")
        self.assertIn("лечение", final,
                      "W1772: 'лечение' потерян при конкурентном apply (TOCTOU)")
        self.assertEqual(final["диагноз"], "diagnóstico")
        self.assertEqual(final["лечение"], "tratamiento")

    def test_many_concurrent_apply_all_persist(self) -> None:
        """20 конкурентных потоков добавляют уникальные ключи — все сохраняются."""
        svc, store = _make_svc()
        n = 20
        errors: list = []

        def add(idx: int) -> None:
            try:
                svc.handle_apply_glossary_suggestions({
                    "selected_ids": [f"слово_{idx}"],
                    "suggestions": [
                        {"source_term": f"слово_{idx}", "target_term": f"word_{idx}"}
                    ],
                })
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=15)

        self.assertEqual(errors, [], f"Ошибки в потоках: {errors}")

        final = store._settings.get("translation_glossary", {})
        missing = [f"слово_{i}" for i in range(n) if f"слово_{i}" not in final]
        self.assertEqual(
            missing, [],
            f"W1772 TOCTOU: потеряно {len(missing)} ключей: {missing[:5]}",
        )

    def test_sequential_apply_no_lost_update(self) -> None:
        """Два последовательных apply сохраняют оба ключа (базовый регрессионный тест)."""
        svc, store = _make_svc()

        svc.handle_apply_glossary_suggestions({
            "selected_ids": ["Краб"],
            "suggestions": [{"source_term": "Краб", "target_term": "Krab"}],
        })
        svc.handle_apply_glossary_suggestions({
            "selected_ids": ["Ухо"],
            "suggestions": [{"source_term": "Ухо", "target_term": "Ear"}],
        })

        # handle_apply_glossary_suggestions сохраняет ключи в нижнем регистре
        final = store._settings.get("translation_glossary", {})
        self.assertIn("краб", final, "Первый ключ потерян")
        self.assertIn("ухо", final, "Второй ключ потерян")
        self.assertEqual(final["краб"], "Krab")
        self.assertEqual(final["ухо"], "Ear")


# ──────────────────────────────────────────────────────────────
# Регрессия: нормальное поведение не нарушено
# ──────────────────────────────────────────────────────────────


class TestApplyGlossaryNormalBehaviourW1772(unittest.TestCase):
    """Нормальный путь работает без изменений после фиксов (регрессия)."""

    def test_normal_apply_persists(self) -> None:
        """Короткие пары в пустой глоссарий успешно добавляются."""
        svc, store = _make_svc()

        result = svc.handle_apply_glossary_suggestions({
            "selected_ids": ["diagnóstico"],
            "suggestions": [{"source_term": "diagnóstico", "target_term": "диагноз"}],
        })

        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["total_glossary"], 1)
        self.assertEqual(store.save_count, 1)
        final = store._settings.get("translation_glossary", {})
        self.assertEqual(final["diagnóstico"], "диагноз")

    def test_duplicate_skipped_as_before(self) -> None:
        """Уже существующий ключ пропускается (поведение не изменилось)."""
        svc, store = _make_svc(initial_glossary={"diagnóstico": "диагноз"})

        result = svc.handle_apply_glossary_suggestions({
            "selected_ids": ["diagnóstico"],
            "suggestions": [{"source_term": "diagnóstico", "target_term": "иное"}],
        })

        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(store.save_count, 0)

    def test_empty_selected_ids_returns_zero_counts(self) -> None:
        """Пустой selected_ids возвращает нули (поведение не изменилось)."""
        svc, store = _make_svc()
        result = svc.handle_apply_glossary_suggestions({"selected_ids": []})
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(store.save_count, 0)


if __name__ == "__main__":
    unittest.main()
