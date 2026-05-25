"""Unit-тесты Wave 158: SharingManager TTL + revoke API.

Закрывает privacy gap из Wave 98: токены шаринга больше не постоянны.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.sharing_manager import SharingManager, DEFAULT_SHARE_TTL_HOURS


# ---------------------------------------------------------------------------
# Вспомогательные фейки (копируем минимальный набор из test_sharing_manager.py)
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    def __init__(self, item_id: str, text: str, ts: str = "2024-01-01T10:00:00+00:00") -> None:
        self.id = item_id
        self.text = text
        self.ts = ts
        self.translated_text = ""
        self.source_lang = "ru"
        self.target_lang = "es"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "ts": self.ts,
            "translated_text": self.translated_text,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
        }


class FakeStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}

    def add_item(self, item_id: str, text: str, **kwargs: Any) -> FakeHistoryItem:
        item = FakeHistoryItem(item_id, text, **kwargs)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str) -> FakeHistoryItem | None:
        return self._items.get(item_id)


def make_mgr(
    tmpdir: str,
    default_ttl: int = DEFAULT_SHARE_TTL_HOURS,
    no_default_ttl: bool = False,
) -> tuple[FakeStore, SharingManager]:
    store = FakeStore(data_dir=tmpdir)
    mgr = SharingManager(
        store=store,
        default_share_ttl_hours=default_ttl,
        share_no_default_ttl=no_default_ttl,
    )
    store.add_item("i1", "текст один")
    store.add_item("i2", "текст два")
    return store, mgr


# ---------------------------------------------------------------------------
# TTL tests
# ---------------------------------------------------------------------------

class TTLCreationTestCase(unittest.TestCase):
    """Тесты создания пакетов с TTL."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_create_with_ttl_sets_expires_at(self) -> None:
        """При явном ttl_hours пакет получает expires_at ~ now + ttl*3600."""
        _, mgr = make_mgr(self._tmpdir)
        before = time.time()
        pkg = mgr.prepare_share(["i1"], ttl_hours=24.0)
        after = time.time()

        self.assertIsNotNone(pkg.expires_at)
        expected_low = before + 24 * 3600
        expected_high = after + 24 * 3600
        self.assertGreaterEqual(pkg.expires_at, expected_low)
        self.assertLessEqual(pkg.expires_at, expected_high)

    def test_create_without_ttl_uses_default(self) -> None:
        """Без явного ttl_hours применяется default_share_ttl_hours (168 ч)."""
        _, mgr = make_mgr(self._tmpdir, default_ttl=168)
        before = time.time()
        pkg = mgr.prepare_share(["i1"])  # ttl_hours=None
        after = time.time()

        self.assertIsNotNone(pkg.expires_at)
        expected_low = before + 168 * 3600
        expected_high = after + 168 * 3600
        self.assertGreaterEqual(pkg.expires_at, expected_low)
        self.assertLessEqual(pkg.expires_at, expected_high)

    def test_share_no_default_ttl_setting_disables_auto_ttl(self) -> None:
        """share_no_default_ttl=True делает expires_at=None (бессрочно)."""
        _, mgr = make_mgr(self._tmpdir, no_default_ttl=True)
        pkg = mgr.prepare_share(["i1"])  # ttl_hours=None

        self.assertIsNone(pkg.expires_at)
        self.assertFalse(pkg.is_revoked)

    def test_explicit_ttl_overrides_no_default_ttl(self) -> None:
        """Явный ttl_hours перекрывает share_no_default_ttl=True."""
        _, mgr = make_mgr(self._tmpdir, no_default_ttl=True)
        pkg = mgr.prepare_share(["i1"], ttl_hours=1.0)
        self.assertIsNotNone(pkg.expires_at)

    def test_default_ttl_constant_is_7_days(self) -> None:
        """DEFAULT_SHARE_TTL_HOURS == 168 (7 дней)."""
        self.assertEqual(DEFAULT_SHARE_TTL_HOURS, 168)


class TTLExpiredTestCase(unittest.TestCase):
    """Тесты поведения истёкших пакетов."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_expired_package_not_returned(self) -> None:
        """get_shared возвращает None для истёкшего пакета."""
        _, mgr = make_mgr(self._tmpdir)
        # TTL = -1 час => уже истёк в прошлом
        pkg = mgr.prepare_share(["i1"], ttl_hours=-1.0)

        found = mgr.get_shared(pkg.share_id)
        self.assertIsNone(found)

    def test_expired_package_excluded_from_list(self) -> None:
        """list_shared не возвращает истёкшие пакеты по умолчанию."""
        _, mgr = make_mgr(self._tmpdir)
        mgr.prepare_share(["i1"], ttl_hours=-1.0)  # expired
        mgr.prepare_share(["i2"], ttl_hours=24.0)  # valid

        shares = mgr.list_shared()
        self.assertEqual(len(shares), 1)  # только валидный

    def test_expired_package_visible_with_include_expired(self) -> None:
        """list_shared(include_expired=True) возвращает истёкшие пакеты."""
        _, mgr = make_mgr(self._tmpdir)
        mgr.prepare_share(["i1"], ttl_hours=-1.0)
        mgr.prepare_share(["i2"], ttl_hours=24.0)

        shares = mgr.list_shared(include_expired=True)
        self.assertEqual(len(shares), 2)

    def test_no_expiry_package_always_returned(self) -> None:
        """Пакет без TTL (expires_at=None) всегда возвращается."""
        _, mgr = make_mgr(self._tmpdir, no_default_ttl=True)
        pkg = mgr.prepare_share(["i1"])

        found = mgr.get_shared(pkg.share_id)
        self.assertIsNotNone(found)
        self.assertEqual(found.share_id, pkg.share_id)


# ---------------------------------------------------------------------------
# Revoke tests
# ---------------------------------------------------------------------------

class RevokeTestCase(unittest.TestCase):
    """Тесты API отзыва пакетов."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        _, self._mgr = make_mgr(self._tmpdir)

    def test_revoke_package(self) -> None:
        """revoke_share возвращает True для существующего пакета."""
        pkg = self._mgr.prepare_share(["i1"], ttl_hours=24.0)
        result = self._mgr.revoke_share(pkg.share_id)
        self.assertTrue(result)

    def test_revoked_not_returned(self) -> None:
        """get_shared возвращает None для отозванного пакета."""
        pkg = self._mgr.prepare_share(["i1"], ttl_hours=24.0)
        self._mgr.revoke_share(pkg.share_id)
        found = self._mgr.get_shared(pkg.share_id)
        self.assertIsNone(found)

    def test_revoke_nonexistent_returns_false(self) -> None:
        """revoke_share возвращает False для несуществующего токена."""
        result = self._mgr.revoke_share("nonexistent_token_xyz")
        self.assertFalse(result)

    def test_revoke_idempotent(self) -> None:
        """Повторный revoke_share возвращает True (пакет уже помечен)."""
        pkg = self._mgr.prepare_share(["i1"], ttl_hours=24.0)
        first = self._mgr.revoke_share(pkg.share_id)
        second = self._mgr.revoke_share(pkg.share_id)
        self.assertTrue(first)
        self.assertTrue(second)  # idempotent: пакет всё ещё есть в индексе

    def test_revoked_excluded_from_list(self) -> None:
        """list_shared не возвращает отозванные пакеты по умолчанию."""
        pkg1 = self._mgr.prepare_share(["i1"], ttl_hours=24.0)
        self._mgr.prepare_share(["i2"], ttl_hours=24.0)
        self._mgr.revoke_share(pkg1.share_id)

        shares = self._mgr.list_shared()
        share_ids = [s["share_id"] for s in shares]
        self.assertNotIn(pkg1.share_id, share_ids)
        self.assertEqual(len(shares), 1)

    def test_revoked_visible_with_include_revoked(self) -> None:
        """list_shared(include_revoked=True) возвращает отозванные пакеты."""
        pkg = self._mgr.prepare_share(["i1"], ttl_hours=24.0)
        self._mgr.revoke_share(pkg.share_id)

        shares = self._mgr.list_shared(include_revoked=True)
        self.assertEqual(len(shares), 1)
        self.assertEqual(shares[0]["share_id"], pkg.share_id)

    def test_revoke_persisted_across_reload(self) -> None:
        """Отзыв сохраняется после перезагрузки менеджера."""
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("i1", "текст")
        mgr = SharingManager(store=store, default_share_ttl_hours=168)
        pkg = mgr.prepare_share(["i1"], ttl_hours=24.0)
        mgr.revoke_share(pkg.share_id)

        mgr2 = SharingManager(store=store, default_share_ttl_hours=168)
        found = mgr2.get_shared(pkg.share_id)
        self.assertIsNone(found)


# ---------------------------------------------------------------------------
# IPC handler tests
# ---------------------------------------------------------------------------

class IPCRevokeLinkTestCase(unittest.TestCase):
    """Тесты IPC-обработчика handle_revoke_share_link."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        _, self._mgr = make_mgr(self._tmpdir)

    def test_handle_revoke_share_link_returns_revoked_true(self) -> None:
        pkg = self._mgr.prepare_share(["i1"], ttl_hours=24.0)
        result = self._mgr.handle_revoke_share_link({"token": pkg.share_id})
        self.assertTrue(result["revoked"])
        self.assertEqual(result["token"], pkg.share_id)

    def test_handle_revoke_share_link_accepts_share_id_param(self) -> None:
        """Обработчик принимает 'share_id' как алиас 'token'."""
        pkg = self._mgr.prepare_share(["i1"], ttl_hours=24.0)
        result = self._mgr.handle_revoke_share_link({"share_id": pkg.share_id})
        self.assertTrue(result["revoked"])

    def test_handle_revoke_share_link_nonexistent_returns_false(self) -> None:
        result = self._mgr.handle_revoke_share_link({"token": "ghost_token_000"})
        self.assertFalse(result["revoked"])

    def test_handle_revoke_share_link_missing_token_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_revoke_share_link({})

    def test_handle_prepare_share_respects_ttl_hours_param(self) -> None:
        """IPC prepare_share передаёт ttl_hours в prepare_share."""
        before = time.time()
        result = self._mgr.handle_prepare_share(
            {"item_ids": ["i1"], "ttl_hours": 48.0}
        )
        after = time.time()
        expires_at = result.get("expires_at")
        self.assertIsNotNone(expires_at)
        self.assertGreaterEqual(expires_at, before + 48 * 3600)
        self.assertLessEqual(expires_at, after + 48 * 3600)

    def test_handle_get_shared_raises_for_revoked(self) -> None:
        """handle_get_shared бросает RuntimeError для отозванного пакета."""
        pkg = self._mgr.prepare_share(["i1"], ttl_hours=24.0)
        self._mgr.revoke_share(pkg.share_id)
        with self.assertRaises(RuntimeError):
            self._mgr.handle_get_shared({"share_id": pkg.share_id})


# ---------------------------------------------------------------------------
# Concurrency tests
# ---------------------------------------------------------------------------

class ConcurrentRevokeTestCase(unittest.TestCase):
    """Тесты потокобезопасности revoke_share."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        _, self._mgr = make_mgr(self._tmpdir)

    def test_concurrent_revoke(self) -> None:
        """Параллельные revoke_share одного пакета безопасны."""
        pkg = self._mgr.prepare_share(["i1"], ttl_hours=24.0)

        results = []
        lock = threading.Lock()

        def do_revoke() -> None:
            r = self._mgr.revoke_share(pkg.share_id)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=do_revoke) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Все вызовы завершились без исключения
        self.assertEqual(len(results), 10)
        # После всех revoke пакет недоступен
        self.assertIsNone(self._mgr.get_shared(pkg.share_id))

    def test_concurrent_revoke_and_get(self) -> None:
        """Параллельные revoke и get не вызывают гонок данных."""
        pkg = self._mgr.prepare_share(["i1"], ttl_hours=24.0)
        errors: list[Exception] = []
        lock = threading.Lock()

        def do_revoke() -> None:
            try:
                self._mgr.revoke_share(pkg.share_id)
            except Exception as e:
                with lock:
                    errors.append(e)

        def do_get() -> None:
            try:
                self._mgr.get_shared(pkg.share_id)  # может вернуть None — ОК
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = (
            [threading.Thread(target=do_revoke) for _ in range(5)]
            + [threading.Thread(target=do_get) for _ in range(5)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Exceptions during concurrent access: {errors}")


if __name__ == "__main__":
    unittest.main()
