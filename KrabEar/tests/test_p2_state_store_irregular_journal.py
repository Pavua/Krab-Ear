"""P2 — StateStore.__init__: защита от зависания на нерегулярных записях журналов.

Проблема (задокументирована в docs/audit/2026-09-29-acceptance.md §Находки п.4 и
docs/superpowers/plans/2026-09-26-a52c1-purge-integrity.md §17):

  StateStore.__init__ вызывает path.touch(exist_ok=True) для каждого из 11
  управляемых журналов. Если на пути журнала лежит FIFO (например, оставшийся
  после preflight_failed с нерегулярной записью), open() в touch() блокируется
  навсегда — нет второго конца трубы. Бэкенд перестаёт запускаться, а история
  становится недоступной.

  Старый claim о зависании в Path.touch() был опровергнут; уточнённый диагноз:
  первое ЧТЕНИЕ ledger'а тоже зависает (под общим flock). Оба пути опасны:
  touch() — при инициализации, _read_history_ndjson_unlocked() — при первом
  обращении к истории.

Фикс (state_store.py):
  Перед path.touch() добавлена lstat-проверка: если запись существует и не
  является обычным файлом (FIFO, сокет, каталог, симлинк) — touch() пропускается,
  в лог пишется WARNING. Нерегулярная запись не удаляется: это задача отдельного
  оператора (preflight-шаг в purge уже делает это правильно).

Тест-стратегия: создаём FIFO на пути purged_ids_path, убеждаемся, что
StateStore(data_dir) завершается в разумное время (≤ 5 с), не зависая.
Затем убеждаемся, что _read_history_ndjson_unlocked тоже не зависает на FIFO
(реализуется отдельной проверкой через threading.Event с timeout).
"""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import pytest

from backend.state_store import StateStore


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _data_dir(tmp_path: Path) -> Path:
    """Создаём пустой data_dir — без settings.json, шифрование выключено."""
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_store_with_timeout(data_dir: Path, timeout: float) -> tuple[StateStore | None, float]:
    """Создаём StateStore в отдельном потоке с ограничением по времени.

    Возвращает (store_or_None, elapsed_seconds).
    Если поток не завершился за timeout — возвращает (None, timeout).
    """
    result: dict = {}
    finished = threading.Event()

    def _run() -> None:
        try:
            result["store"] = StateStore(data_dir)
        except Exception as exc:  # noqa: BLE001
            result["exc"] = exc
        finally:
            finished.set()

    t0 = time.monotonic()
    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    finished.wait(timeout)
    elapsed = time.monotonic() - t0

    if not finished.is_set():
        # Поток завис — возвращаем None
        return None, elapsed

    if "exc" in result:
        raise result["exc"]

    return result.get("store"), elapsed


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------


class TestStateStoreInitDoesNotHangOnIrregularJournal:
    """StateStore.__init__ не зависает, если журнал — нерегулярная запись.

    Воспроизводит дефект, задокументированный в принятом acceptance-отчёте:
    touch() на FIFO без читателя блокируется навсегда.
    """

    @pytest.mark.parametrize(
        "journal_name",
        [
            "history_purged_ids.ndjson",   # основной сценарий из acceptance-отчёта
            "history.ndjson",               # главный журнал истории
            "history_tombstones.ndjson",    # tombstone-журнал
        ],
        ids=["purged_ids", "history", "tombstones"],
    )
    def test_init_completes_when_journal_is_fifo(self, tmp_path, journal_name):
        """StateStore(data_dir) завершается ≤ 5 с, даже если журнал — FIFO."""
        data_dir = _data_dir(tmp_path)
        # Заранее создаём FIFO на пути интересующего нас журнала.
        # Именно такой сценарий возникает после preflight_failed в purge,
        # который переименовывает FIFO, но не может его удалить, оставляя
        # FIFO на месте.
        fifo_path = data_dir / journal_name
        os.mkfifo(fifo_path)
        assert stat.S_ISFIFO(os.stat(fifo_path).st_mode), "FIFO создан корректно"

        store, elapsed = _make_store_with_timeout(data_dir, timeout=5.0)

        assert store is not None, (
            f"StateStore.__init__ ЗАВИС на журнале '{journal_name}' с FIFO: "
            f"не вернулся за 5 с. Это воспроизводит acceptance-дефект п.4: "
            "'StateStore.__init__ проходит, первое чтение ledger зависает.'"
        )
        assert elapsed < 5.0, (
            f"StateStore инициализация заняла {elapsed:.2f}s > 5s — слишком долго"
        )

    def test_init_completes_when_journal_is_directory(self, tmp_path):
        """StateStore(data_dir) не зависает, если журнал — каталог."""
        data_dir = _data_dir(tmp_path)
        journal_path = data_dir / "history_purged_ids.ndjson"
        journal_path.mkdir()

        store, elapsed = _make_store_with_timeout(data_dir, timeout=5.0)

        assert store is not None, (
            "StateStore.__init__ завис при каталоге на пути purged_ids"
        )
        assert elapsed < 5.0

    def test_init_does_not_delete_irregular_entry(self, tmp_path):
        """Нерегулярная запись не удаляется при инициализации.

        StateStore.__init__ — это НЕ место для принятия решений об удалении:
        FIFO/каталог может принадлежать внешнему процессу. Мы только пропускаем
        touch() и логируем WARNING, не сносим запись.
        """
        data_dir = _data_dir(tmp_path)
        fifo_path = data_dir / "history_purged_ids.ndjson"
        os.mkfifo(fifo_path)

        store, _ = _make_store_with_timeout(data_dir, timeout=5.0)
        assert store is not None

        # FIFO должен остаться нетронутым — StateStore не удалял нерегулярные записи
        assert os.path.lexists(fifo_path), (
            "StateStore.__init__ не должен удалять нерегулярные записи: "
            "это решение владельца, а не автоматическая операция при старте"
        )
        assert stat.S_ISFIFO(os.stat(fifo_path).st_mode), (
            "Тип нерегулярной записи не должен измениться при инициализации"
        )

    def test_regular_journals_still_created_after_irregular(self, tmp_path):
        """Остальные журналы создаются нормально, даже если один — нерегулярный.

        Пропуск touch() для нерегулярного пути не должен ломать инициализацию
        прочих журналов — это частичная деградация, а не полный отказ.
        """
        data_dir = _data_dir(tmp_path)
        # FIFO на одном журнале
        fifo_path = data_dir / "history_purged_ids.ndjson"
        os.mkfifo(fifo_path)

        store, _ = _make_store_with_timeout(data_dir, timeout=5.0)
        assert store is not None

        # Все остальные журналы должны быть созданы
        for name in (
            "history.ndjson",
            "history_tombstones.ndjson",
            "history_status.ndjson",
            "history_tags.ndjson",
        ):
            path = data_dir / name
            assert path.exists() and path.is_file(), (
                f"{name} не создан при инициализации — touch() для нерегулярного "
                "соседа не должен блокировать создание остальных журналов"
            )

    def test_history_readable_after_init_with_irregular_ledger(self, tmp_path):
        """После инициализации с нерегулярным ledger'ом история читаема.

        Профиль должен оставаться читаемым: история пустая, но get_history_page
        не должна зависать или бросать исключение.
        """
        data_dir = _data_dir(tmp_path)
        fifo_path = data_dir / "history_purged_ids.ndjson"
        os.mkfifo(fifo_path)

        store, _ = _make_store_with_timeout(data_dir, timeout=5.0)
        assert store is not None

        # get_history_page не должен зависать (читает history.ndjson, не purged_ids)
        # При нерегулярном purged_ids — _load_deleted_ids_unlocked тоже не должен зависать
        result_holder: dict = {}
        event = threading.Event()

        def _read():
            try:
                result_holder["page"] = store.get_history_page(None, 10)
            except Exception as exc:  # noqa: BLE001
                result_holder["exc"] = exc
            finally:
                event.set()

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        assert event.wait(5.0), (
            "get_history_page завис при нерегулярном ledger'е — "
            "_read_history_ndjson_unlocked не должен открывать FIFO"
        )
        assert "exc" not in result_holder, f"get_history_page упал: {result_holder.get('exc')}"


class TestStateStoreIrregularJournalIsLogged:
    """Пропуск touch() при нерегулярной записи фиксируется в логах.

    Это не критическая проверка, но без логирования владелец не увидит,
    почему purged_ids не был инициализирован при старте.
    """

    def test_warning_logged_for_fifo_journal(self, tmp_path, caplog):
        """WARNING должен быть записан при пропуске touch() из-за FIFO."""
        import logging

        data_dir = _data_dir(tmp_path)
        fifo_path = data_dir / "history_purged_ids.ndjson"
        os.mkfifo(fifo_path)

        with caplog.at_level(logging.WARNING, logger="KrabEar.Backend.Store"):
            store, _ = _make_store_with_timeout(data_dir, timeout=5.0)

        assert store is not None
        # Проверяем что в логах есть предупреждение о нерегулярной записи
        journal_warnings = [
            r for r in caplog.records
            if "нерегул" in r.message.lower()
            or "irregular" in r.message.lower()
            or "purged_ids" in r.message.lower()
        ]
        assert journal_warnings, (
            "При пропуске touch() для нерегулярной записи обязан быть WARNING: "
            "без него администратор/владелец не узнает о деградированном состоянии"
        )


class TestAppendNdjsonRawRejectsIrregular:
    """_append_ndjson_raw не зависает при попытке записи в нерегулярный файл."""

    def test_append_raises_oserror_on_fifo_without_hanging(self, tmp_path):
        """Запись в FIFO поднимает OSError и не зависает."""
        data_dir = _data_dir(tmp_path)
        fifo_path = data_dir / "history_purged_ids.ndjson"
        os.mkfifo(fifo_path)

        event = threading.Event()
        outcome: dict = {}

        def _do_append():
            try:
                StateStore._append_ndjson_raw(fifo_path, '{"id": "test-1"}')
            except BaseException as exc:  # noqa: BLE001
                outcome["exc"] = exc
            finally:
                event.set()

        t = threading.Thread(target=_do_append, daemon=True)
        t.start()
        assert event.wait(5.0), "append завис на FIFO вместо немедленного отказа"
        assert "exc" in outcome, "append в FIFO обязан был упасть с ошибкой"
        assert isinstance(outcome["exc"], OSError)

    def test_append_raises_oserror_on_directory(self, tmp_path):
        """Запись в каталог поднимает OSError."""
        data_dir = _data_dir(tmp_path)
        dir_path = data_dir / "history_purged_ids.ndjson"
        dir_path.mkdir()

        with pytest.raises(OSError):
            StateStore._append_ndjson_raw(dir_path, '{"id": "test-1"}')

    def test_append_succeeds_on_regular_file(self, tmp_path):
        """Запись в обычный файл проходит штатно."""
        data_dir = _data_dir(tmp_path)
        regular_path = data_dir / "history.ndjson"
        StateStore._append_ndjson_raw(regular_path, '{"id": "test-1"}')
        assert '{"id": "test-1"}\n' == regular_path.read_text(encoding="utf-8")


class TestReadHistoryNdjsonDescriptorSafety:
    """Дескрипторная безопасность _read_history_ndjson_unlocked."""

    def test_symlink_to_fifo_does_not_hang(self, tmp_path):
        """Симлинк, указывающий на FIFO, не блокирует чтение."""
        data_dir = _data_dir(tmp_path)
        fifo_path = tmp_path / "external.fifo"
        os.mkfifo(fifo_path)
        symlink_path = data_dir / "history_purged_ids.ndjson"
        symlink_path.symlink_to(fifo_path)

        store = StateStore(data_dir)
        event = threading.Event()
        results: list = []

        def _read():
            try:
                results.extend(list(store._read_history_ndjson_unlocked(symlink_path)))
            finally:
                event.set()

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        assert event.wait(5.0), "чтение симлинка на FIFO зависло"
        assert results == []

    def test_encryption_unavailable_is_not_swallowed(self, tmp_path, monkeypatch):
        """Ошибки расшифровки (HistoryEncryptionUnavailable) НЕ маскируются.

        Требование: без ослабления ошибок расшифровки.
        Если строка зашифрована, а шифрование недоступно / ключ отсутствует,
        ошибка обязана всплывать, а не молча подавляться дескрипторным гейтом.
        """
        from backend.state_store import HistoryEncryptionUnavailable

        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        # Записываем зашифрованную строку в regular файл
        history_file = data_dir / "history.ndjson"
        history_file.write_text("ENC1:some-fake-ciphertext\n", encoding="utf-8")

        # Симулируем недоступность криптомодуля
        monkeypatch.setattr(store, "_get_history_crypto", lambda: None)

        with pytest.raises(HistoryEncryptionUnavailable):
            list(store._read_history_ndjson_unlocked(history_file))

    def test_warning_logged_during_read_of_fifo(self, tmp_path, caplog):
        """_read_history_ndjson_unlocked пишет WARNING при обнаружении FIFO."""
        import logging

        data_dir = _data_dir(tmp_path)
        fifo_path = data_dir / "history_purged_ids.ndjson"
        os.mkfifo(fifo_path)
        store = StateStore(data_dir)

        with caplog.at_level(logging.WARNING, logger="KrabEar.Backend.Store"):
            items = list(store._read_history_ndjson_unlocked(fifo_path))

        assert items == []
        read_warnings = [
            r for r in caplog.records
            if "history_purged_ids.ndjson" in r.message
            and ("нерегулярн" in r.message.lower() or "irregular" in r.message.lower())
        ]
        assert read_warnings, (
            "_read_history_ndjson_unlocked обязан логировать WARNING при пропуске FIFO"
        )


class TestHasEncryptedHistoryDoesNotHangOnFIFO:
    """has_encrypted_history_in и связанные пути не зависают при наличии FIFO."""

    def test_has_encrypted_history_unlocked_with_fifo_purged_ids(self, tmp_path):
        """_has_encrypted_history_unlocked() не зависает при FIFO на purged_ids."""
        data_dir = _data_dir(tmp_path)
        fifo_path = data_dir / "history_purged_ids.ndjson"
        os.mkfifo(fifo_path)
        store = StateStore(data_dir)

        event = threading.Event()
        outcome: dict = {}

        def _check():
            try:
                with store._lock():
                    outcome["res"] = store._has_encrypted_history_unlocked()
            except Exception as exc:  # noqa: BLE001
                outcome["exc"] = exc
            finally:
                event.set()

        t = threading.Thread(target=_check, daemon=True)
        t.start()
        assert event.wait(5.0), "_has_encrypted_history_unlocked завис на FIFO под flock"
        assert "exc" not in outcome, f"ошибка при проверке шифрования: {outcome.get('exc')}"
        assert outcome["res"] is False

    def test_add_history_item_with_fifo_purged_ids_and_no_settings(self, tmp_path):
        """add_history_item() в свежем профиле без settings.json не зависает при FIFO."""
        data_dir = _data_dir(tmp_path)
        fifo_path = data_dir / "history_purged_ids.ndjson"
        os.mkfifo(fifo_path)
        store = StateStore(data_dir)

        event = threading.Event()
        outcome: dict = {}

        def _add():
            try:
                outcome["item"] = store.add_history_item(text="тестовая запись")
            except Exception as exc:  # noqa: BLE001
                outcome["exc"] = exc
            finally:
                event.set()

        t = threading.Thread(target=_add, daemon=True)
        t.start()
        assert event.wait(5.0), (
            "add_history_item() завис на FIFO в purged_ids под эксклюзивным flock: "
            "read_history_encryption_flag → has_encrypted_history_in заблокировался"
        )
        assert "exc" not in outcome, f"add_history_item упал: {outcome.get('exc')}"
        assert outcome["item"] is not None

    def test_get_history_encryption_status_with_fifo_history(self, tmp_path):
        """get_history_encryption_status() не зависает при FIFO на history.ndjson."""
        data_dir = _data_dir(tmp_path)
        fifo_path = data_dir / "history.ndjson"
        os.mkfifo(fifo_path)
        store = StateStore(data_dir)

        event = threading.Event()
        outcome: dict = {}

        def _status():
            try:
                outcome["status"] = store.get_history_encryption_status()
            except Exception as exc:  # noqa: BLE001
                outcome["exc"] = exc
            finally:
                event.set()

        t = threading.Thread(target=_status, daemon=True)
        t.start()
        assert event.wait(5.0), "get_history_encryption_status() завис на FIFO history.ndjson"
        assert "exc" not in outcome, f"status упал: {outcome.get('exc')}"
        assert outcome["status"]["total"] == 0
