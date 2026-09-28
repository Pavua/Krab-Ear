"""A5.2c1 — purge: целостность профиля и полнота зачистки.

Спека `docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md` §5/§6 и
карточка `docs/superpowers/plans/2026-09-26-a52c1-purge-integrity.md`.

Два дефекта, найденных РАЗБОРОМ КОДА, а не тестами:

  1. **Purge при ON ломает чтение истории.** Шаг компактирования дописывает
     ENC1-строки в `history_purged_ids.ndjson` СТАРЫМ ключом; файл allowlisted
     и purge его не чистит; шаг удаления ключа shred'ит ключ → следующее
     чтение истории получает `InvalidTag` → `HistoryEncryptionUnavailable`.
     Профиль с шифрованием после privacy-purge НЕЧИТАЕМ.
  2. **Purge не зачищает копии.** `history.ndjson.bak-*` (в прод-профиле
     владельца — 23.4 МБ ОТКРЫТОЙ истории) и `settings.json.bak*` (шесть копий
     с непустыми секретами) не попадают в зону purge: в коде нет glob'а на
     `*.bak*`.

Только synthetic tmp-профили и СЛУЧАЙНЫЙ тестовый ключ (`os.urandom(32)`).
Системный Keychain не трогается, и это доказывается ДВУМЯ независимыми
способами (как в b1/b2/b3):

  1. счётчики с РАЗДЕЛЕНИЕМ на патче `crypto_keystore._run_security`:
       * ``reads``     — чтение ключа (``find-generic-password … -w``);
       * ``creates``   — создание ключа (``add-generic-password``);
       * ``deletions`` — удаление ключа (``delete-generic-password``) —
         ОЖИДАЕМЫЙ путь внутри ``handle_purge_all_data`` (purge по контракту
         shred'ит ключ: без этого выживший AES-ключ расшифровывает pre-purge
         бэкап history.ndjson);
       * ``probes``    — read-only проба наличия ключа из ``get_diagnostics``
         (без ``-w``, без создания — это и проверяется).
     Инвариант «снаружи purge обращений нет» проверяется отдельно.
  2. OS-шим: сессионная фикстура кладёт на PATH поддельный исполняемый
     `security`, который ТОЛЬКО пишет маркер вызова. Настоящий бинарь из этой
     сессии недостижим, поэтому «нулевой счётчик» — не артефакт патча.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from backend.encrypted_snapshot import (
    STATE_COMMITTED,
    collect_ledger_union,
    create_encrypted_snapshot,
    restore_encrypted_snapshot,
)
from backend.history_crypto import HistoryCrypto
from backend.history_service import HistoryService
from backend.state_store import StateStore

# Фиксированный путь OS-шима: одинаков для всего прогона pytest-сессии, чтобы
# факт обращения к `security(1)` можно было проверить и ПОСЛЕ прогона.
SECURITY_SHIM_MARKER = Path(tempfile.gettempdir()) / "krab_a52c1_security_invocations.log"

# Счётчики обращений к Keychain — С РАЗДЕЛЕНИЕМ (см. докстринг модуля).
KEYCHAIN = {
    "reads": 0,       # find-generic-password … -w  (нужен ключ)
    "creates": 0,     # add-generic-password       (ключ создаётся)
    "deletions": 0,   # delete-generic-password    (ожидаемо ТОЛЬКО в purge)
    "probes": 0,      # find-generic-password без -w (read-only проба)
}


def _reset_counters() -> None:
    for key in KEYCHAIN:
        KEYCHAIN[key] = 0


def _snapshot_counters() -> dict[str, int]:
    return dict(KEYCHAIN)


def _delta(before: dict[str, int]) -> dict[str, int]:
    return {k: KEYCHAIN[k] - before[k] for k in KEYCHAIN}


# ---------------------------------------------------------------------------
# Поддельный Keychain: настоящая семантика чтения/создания/удаления
# ---------------------------------------------------------------------------


class _FakeKeychain:
    """In-memory Keychain поверх ``crypto_keystore._run_security``.

    Нужен для ГЛАВНОГО RED-кейса: ротация ключа обязана быть настоящей
    (удаление + новый ``get_or_create_history_key``), иначе тест проверил бы
    «забытый» crypto-инстанс вместо production-инварианта.

    Ни одна реальная команда `security` не выполняется: единственная точка
    вызова — ``_run_security``, которую патчит фикстура.
    """

    ITEM_NOT_FOUND_EXIT_CODE = 44

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], bytes] = {}
        self.calls: list[list[str]] = []

    def run(self, args) -> subprocess.CompletedProcess:
        argv = list(args)
        self.calls.append(argv)
        verb = argv[0]
        service = argv[argv.index("-s") + 1] if "-s" in argv else ""
        account = argv[argv.index("-a") + 1] if "-a" in argv else ""
        key = (service, account)

        if verb == "find-generic-password":
            if "-w" in argv:
                KEYCHAIN["reads"] += 1
            else:
                # Проба наличия: ключевой материал НЕ выводится (нет -w).
                KEYCHAIN["probes"] += 1
            if key in self.items:
                out = base64.b64encode(self.items[key]).decode() + "\n" if "-w" in argv else ""
                return subprocess.CompletedProcess(argv, 0, out, "")
            return subprocess.CompletedProcess(argv, self.ITEM_NOT_FOUND_EXIT_CODE, "", "not found")

        if verb == "add-generic-password":
            KEYCHAIN["creates"] += 1
            self.items[key] = base64.b64decode(argv[argv.index("-w") + 1])
            return subprocess.CompletedProcess(argv, 0, "", "")

        if verb == "delete-generic-password":
            KEYCHAIN["deletions"] += 1
            self.items.pop(key, None)
            return subprocess.CompletedProcess(argv, 0, "", "")

        raise AssertionError(f"неожиданный вызов security: {argv}")


@pytest.fixture
def fake_keychain(monkeypatch):
    """Фикстура с поддельным Keychain + OS-шим на PATH (два доказательства)."""
    import backend.crypto_keystore as ks

    _reset_counters()
    fake = _FakeKeychain()

    def _guarded(args, *_a, **_kw):
        return fake.run(args)

    monkeypatch.setattr(ks, "_run_security", _guarded)
    return fake


@pytest.fixture(scope="session", autouse=True)
def _forbid_security_cli(tmp_path_factory):
    """OS-шим: настоящий `security` недостижим из этой pytest-сессии.

    Доказательство вторым способом — независимое от python-патчей: любой
    subprocess, дошедший до бинаря, попадёт в подмену, которая только пишет
    маркер. Патченный `_run_security` (фикстура ``fake_keychain``) сюда не
    доходит — именно поэтому маркер обязан остаться пустым.
    """
    bin_dir = tmp_path_factory.mktemp("a52c1_shim_bin")
    script = bin_dir / "security"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{SECURITY_SHIM_MARKER}"\n'
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    SECURITY_SHIM_MARKER.unlink(missing_ok=True)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
    try:
        yield SECURITY_SHIM_MARKER
    finally:
        os.environ["PATH"] = old_path


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------


def _crypto() -> HistoryCrypto:
    return HistoryCrypto(os.urandom(32))


def _data_dir(tmp_path: Path) -> Path:
    base = tmp_path / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _settings_on(data_dir: Path) -> None:
    (data_dir / "settings.json").write_text(
        json.dumps({"history_encryption_enabled": True}), encoding="utf-8"
    )


def _encryption_enabled_now(data_dir: Path) -> bool:
    return bool(
        json.loads((data_dir / "settings.json").read_text(encoding="utf-8")).get(
            "history_encryption_enabled"
        )
    )


def _write_ledger(data_dir: Path, name: str, ids: list[str], crypto: HistoryCrypto) -> None:
    (data_dir / name).write_text(
        "".join(
            crypto.encrypt_line(json.dumps({"id": i}, ensure_ascii=False)) + "\n" for i in ids
        ),
        encoding="utf-8",
    )


def _live_ids(data_dir: Path, crypto: HistoryCrypto, name: str) -> list[str]:
    path = data_dir / name
    if not path.is_file():
        return []
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if line.startswith("ENC1:"):
            line = crypto.decrypt_line(line)
        payload = json.loads(line)
        item_id = payload.get("id")
        if item_id:
            ids.append(str(item_id))
    return ids


def _make_snapshot(data_dir: Path, crypto: HistoryCrypto, name: str = "snapshot_1") -> Path:
    snapshot_dir = data_dir / "backups" / name
    result = create_encrypted_snapshot(
        data_dir=data_dir,
        backup_dir=snapshot_dir,
        crypto=crypto,
        transaction_id="tx-a52c1-0001",
        policy_on=True,
    )
    assert result["state"] == STATE_COMMITTED
    return snapshot_dir


def _store_crypto(store: StateStore) -> HistoryCrypto:
    """HistoryCrypto ТОТ ЖЕ ключ, которым пишет живой StateStore.

    Снимок/ledger обязаны шифроваться ключом профиля, иначе тест проверял бы
    согласованность двух случайных ключей вместо поведения purge.
    """
    crypto = store._get_history_crypto()
    assert crypto is not None, "профиль ON обязан иметь crypto"
    return crypto


# ---------------------------------------------------------------------------
# Task 1 — RED: профиль ON после purge обязан остаться ЧИТАЕМЫМ
# ---------------------------------------------------------------------------


class TestProfileStaysReadableAfterPurge:
    """Главный дефект №1: purge shred'ит ключ, но оставляет ENC1-ledger."""

    def test_history_readable_after_purge_with_real_key_rotation(
        self, tmp_path, fake_keychain
    ):
        """Ключ УДАЛЯЕТСЯ purge, а следующая запись получает НОВЫЙ — как в проде.

        Именно ротация ключа делает баг production-багом: после purge у профиля
        ключа нет, а ``history_purged_ids.ndjson`` остался зашифрованным старым.
        Первое же чтение истории обязано succeed (пустая страница), а первая
        же запись — получить новый ключ, и профиль остаться читаемым.

        Проверка на «забытый crypto-инстанс» здесь невозможна: ключ реально
        удаляется из Keychain и реально создаётся заново (счётчики созданий
        это видят), а не «забывается» в кэше StateStore.
        """
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        store = StateStore(data_dir)
        store.add_history_item(text="секрет владельца")

        before = _snapshot_counters()
        key_before = fake_keychain.items[("KrabEar", "history-encryption-key")]

        svc = HistoryService(store=store)
        result = svc.handle_purge_all_data({"confirm": "PURGE_ALL"})
        assert result["ok"] is True

        # Ротация реальна: purge shred'ит ключ — следующей записи нужен новый.
        assert fake_keychain.items.get(("KrabEar", "history-encryption-key")) is None, (
            "purge обязан shred'ить ключ шифрования истории"
        )
        assert _delta(before)["deletions"] == 1

        # СЛЕДУЮЩЕЕ чтение истории не падает. Раньше — InvalidTag →
        # HistoryEncryptionUnavailable, т.е. профиль становился нечитаемым.
        page, _cursor = store.get_history_page(None, 50)
        assert page == [], "после purge история пуста — чтение обязано вернуть пустую страницу"

        # Следующая запись получает СВЕЖИЙ ключ (ротация состоялась), и профиль
        # остаётся читаемым — то есть purge не оставил за собой «яд», который
        # блокирует последующую работу.
        store.add_history_item(text="новая запись после purge")
        key_after = fake_keychain.items.get(("KrabEar", "history-encryption-key"))
        assert key_after is not None, "следующая запись обязана получить ключ"
        assert key_after != key_before, "ключ обязан быть НОВЫМ, а не восстановленным"

        reread, _cursor = store.get_history_page(None, 50)
        assert [item["text"] for item in reread] == ["новая запись после purge"]

    def test_deletion_ledger_ciphertext_is_destroyed_after_purge(self, tmp_path, fake_keychain):
        """Ledger переживает purge открытым — шифротекст обязан исчезнуть.

        B1 (adversarial-ревью): обоснование карточки «ledger можно снести, его
        роль держит ledger самой копии» было НЕВЕРНЫМ для ID, удалённых в
        текущем профиле ПОСЛЕ снимка: их знает только текущий ledger. Поэтому
        содержимое ledger'а сохраняется намеренно (в нём только ID, без PII) —
        уничтожается ТОЛЬКО шифротекст. Исходный дефект (профиль нечитаем после
        shred'а ключа) при этом остаётся починенным.
        """
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        store = StateStore(data_dir)
        removed_id = store.add_history_item(text="удалённая запись").id
        store.delete_history_item(removed_id)
        store.compact_with_stats()

        ledger = data_dir / "history_purged_ids.ndjson"
        before = ledger.read_text(encoding="utf-8")
        assert any(line.startswith("ENC1:") for line in before.splitlines()), (
            "прогресс-условие: до purge ledger зашифрован — иначе тест врал бы"
        )

        HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})

        assert ledger.exists(), "содержимое ledger'а переживает purge (B1)"
        raw = ledger.read_text(encoding="utf-8")
        encrypted = [line for line in raw.splitlines() if line.startswith("ENC1:")]
        assert encrypted == [], (
            "шифротекст обязан быть уничтожен: после shred'а ключа он нечитаем "
            "и роняет чтение истории (исходный дефект карточки)"
        )
        assert removed_id in _plain_ledger_ids(ledger), (
            "список удалённых ID обязан сохраниться — иначе resurrection открыт"
        )

    def test_ledger_recreated_by_restart_is_readable(self, tmp_path, fake_keychain):
        """Рестарт backend не ломает ledger: он остаётся читаемым и полным.

        B1: ledger теперь переживает purge открытым, поэтому после рестарта
        StateStore не должен ни потерять его содержимое, ни потребовать ключ.
        Проверяем оба свойства, а не «файл существует».
        """
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        store = StateStore(data_dir)
        crypto = _store_crypto(store)
        removed_id = store.add_history_item(text="секрет владельца").id
        HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})

        restarted = StateStore(data_dir)
        page, _cursor = restarted.get_history_page(None, 50)

        assert page == [], "история пуста — purge обязан её очистить"
        ledger = data_dir / "history_purged_ids.ndjson"
        assert ledger.exists()
        # Читается обоими ридерами БЕЗ ключа (ключ-то уже уничтожен).
        assert removed_id in _plain_ledger_ids(ledger)
        assert removed_id in restarted._load_deleted_ids_unlocked()
        assert removed_id in set(collect_ledger_union(data_dir=data_dir, crypto=crypto))


# ---------------------------------------------------------------------------
# Task 1 — RED: зачистка копий
# ---------------------------------------------------------------------------


class TestStaleCopiesAreWiped:
    """Дефект №2: ``*.bak*`` — копии уничтожаемых данных, purge их оставлял."""

    def test_history_and_settings_bak_copies_are_deleted(self, tmp_path, fake_keychain):
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        store = StateStore(data_dir)
        store.add_history_item(text="открытая история владельца")

        (data_dir / "history.ndjson.bak-20260926-120000").write_text(
            (data_dir / "history.ndjson").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (data_dir / "settings.json.bak").write_text(
            json.dumps({"hf_token": "не-for-assert", "llm_api_key": "x"}), encoding="utf-8"
        )
        (data_dir / "settings.json.bak1").write_text(
            json.dumps({"sentry_dsn_agent": "https://example.invalid/1"}), encoding="utf-8"
        )

        HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})

        assert sorted(p.name for p in data_dir.glob("*.bak*")) == [], (
            "privacy-purge обязан снести .bak-копии истории и настроек"
        )

    def test_temp_copy_families_are_deleted(self, tmp_path, fake_keychain):
        """M1: `*.tmp`/`*_tmp` — тоже копии уничтожаемых данных.

        Найдено пробой ревьюера: обычный purge рапортовал `complete: true,
        errors: [], stale_copies_removed: 0`, а на диске лежали
        `history.ndjson.migration_tmp` (ПОЛНАЯ копия истории из убитой миграции
        шифрования) и `settings.json.tmp` (с секретами). То есть «успешный»
        purge — ложный all-clear.

        Семейство `*.tmp` выбрано намеренно: в data_dir `.tmp` пишут 12+
        атомарных писателей (state_store, recording_chain, settings_backup,
        sharing_manager, obsidian_sync, semantic_search через mkstemp, …), и
        перечисление «известных» имён — это ровно тот способ, которым
        `migration_tmp` и просочился. Каждый `.tmp` по построению — копия
        файла, который purge уничтожает.
        """
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        store = StateStore(data_dir)
        store.add_history_item(text="открытая история владельца")
        history_copy = (data_dir / "history.ndjson").read_text(encoding="utf-8")

        # Производители: state_store.py:2527 (миграция шифрования) и :919
        # (атомарная запись settings). Остальные — tmp-файлы delta-журналов,
        # которые остаются при сбое compact.
        (data_dir / "history.ndjson.migration_tmp").write_text(history_copy, encoding="utf-8")
        (data_dir / "settings.json.tmp").write_text(
            json.dumps({"hf_token": "СЕКРЕТ-МАРКЕР"}), encoding="utf-8"
        )
        (data_dir / "history.ndjson.tmp").write_text(history_copy, encoding="utf-8")
        (data_dir / "history_status.tmp").write_text('{"id":"x","paste_status":"done"}')
        (data_dir / "history_annotations.tmp").write_text('{"id":"x","note":"заметка"}')
        # Случайное имя от tempfile.mkstemp(suffix=".tmp") (semantic_search).
        (data_dir / "tmpab12cd34.tmp").write_text("npy-like", encoding="utf-8")

        result = HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})

        # Проверка обязана ловить ОБА окончания: `*.tmp` не матчит `migration_tmp`
        # (подчёркивание, а не точка) — именно поэтому первый вариант проверки
        # сам был слеп к главной утечке ревьюера.
        left = sorted(
            {p.name for pat in ("*.tmp", "*_tmp") for p in data_dir.glob(pat)}
        )
        assert left == [], f"производные tmp-копии пережили purge: {left}"
        # Ровно 3, а не 6: `history.ndjson.tmp`, `history_status.tmp` и
        # `history_annotations.tmp` уничтожает САМА компактирующая ветка purge
        # (шаг 1b переписывает эти tmp и переименовывает их в живой журнал).
        # А зачисткой сносятся три, которых purge сам не трогает:
        # `migration_tmp`, `settings.json.tmp` и mkstemp-имя. Счётчик фиксирует
        # именно факт исчезновения, поэтому число не «на глаз».
        assert result["stale_copies_removed"] == 3, (
            "посчитано только то, что действительно исчезло — иначе отчёт "
            "«stale_copies_removed: 0» при полной утечке"
        )
        assert result["complete"] is True, result["errors"]

    def test_irregular_stale_copies_are_reported_not_skipped(self, tmp_path, fake_keychain):
        """L3: каталог и висячий симлинк под именем копии — не «молча пройти».

        `is_file()` возвращает False и для каталога, и для битого симлинка, так
        что оба случая молча выпадали из зачистки и из отчёта: на диске оставалась
        папка с копией истории и «висящая» ссылка, а purge рапортовала успех.
        """
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        store = StateStore(data_dir)
        store.add_history_item(text="секрет владельца")

        as_dir = data_dir / "history.ndjson.bak-dir"
        as_dir.mkdir()
        (as_dir / "history.ndjson").write_text("ПОЛНАЯ КОПИЯ", encoding="utf-8")
        dangling = data_dir / "settings.json.bak-broken"
        dangling.symlink_to(data_dir / "нет-такого-файла")

        result = HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})

        assert not os.path.lexists(as_dir), (
            "каталог под именем копии содержит данные — он обязан быть снесён"
        )
        assert not os.path.lexists(dangling), (
            "висячий симлинк под именем копии обязан быть снят (сам линк, не цель)"
        )
        assert result["stale_copies_removed"] == 2, "обе нерегулярные записи посчитаны"
        assert result["complete"] is True, result["errors"]

    def test_unremovable_stale_copy_is_counted_honestly(self, tmp_path, fake_keychain, monkeypatch):
        """L4: счётчик занижает/завышает? Обязан считать ПО ФАКТУ.

        `unlink(missing_ok=True)` + безусловный `+= 1` зачитывал бы копию,
        которая осталась на диске (отказ по правам, гонка, занятый файл). Тогда
        отчёт врал бы в ту же сторону, что и раньше: «посчитано — значит
        уничтожено».
        """
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        store = StateStore(data_dir)
        store.add_history_item(text="секрет владельца")
        (data_dir / "history.ndjson.bak-stuck").write_text("копия", encoding="utf-8")
        (data_dir / "settings.json.tmp").write_text("{}", encoding="utf-8")

        real_unlink = Path.unlink

        def _deny_stuck(self, missing_ok=False):
            if self.name == "history.ndjson.bak-stuck":
                raise OSError(13, "Permission denied")
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", _deny_stuck)

        result = HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})

        assert (data_dir / "history.ndjson.bak-stuck").exists(), "прогресс-условие"
        assert result["stale_copies_removed"] == 1, (
            "посчитана только та копия, которой действительно не стало"
        )
        assert "stale_copies" in result["errors"], "уцелевшая копия — шаговая ошибка"
        assert result["complete"] is False, "purge с уцелевшими копиями неполон"

    def test_foreign_files_in_data_dir_are_not_touched(self, tmp_path, fake_keychain):
        """Зачистка — ЯВНЫМ перечислением паттернов, не широким glob по data_dir."""
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        store = StateStore(data_dir)
        store.add_history_item(text="секрет владельца")

        keep = {
            "notes.txt": "заметка владельца",
            "session.log": "log",
            "README.md": "readme",
        }
        for name, body in keep.items():
            (data_dir / name).write_text(body, encoding="utf-8")
        (data_dir / "subdir").mkdir()
        (data_dir / "subdir" / "keep.json").write_text("{}", encoding="utf-8")
        (data_dir / "history.ndjson.bak-20260926-120000").write_text("x", encoding="utf-8")

        result = HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})

        for name, body in keep.items():
            assert (data_dir / name).read_text(encoding="utf-8") == body, f"{name} не трогаем"
        assert (data_dir / "subdir" / "keep.json").exists(), "подкаталоги не трогаем"
        assert not (data_dir / "history.ndjson.bak-20260926-120000").exists()
        assert result["stale_copies_removed"] == 1, "засчитана ровно одна снесённая копия"


# ---------------------------------------------------------------------------
# Task 1 — RED: resurrection-защита возвращённой извне копии (union из b2)
# ---------------------------------------------------------------------------


class TestExternalCopyResurrectionStillBlocked:
    """Решение карточки 1: ledger удаляется, но защиту держит ledger САМОЙ копии."""

    def test_restored_external_copy_cannot_resurrect_its_own_purged_ids(
        self, tmp_path, fake_keychain
    ):
        """Снимок содержит свои purged-ID; purge сносит локальный ledger.

        Возврат этого снимка не должен воскресить его собственные удалённые ID —
        запрет даёт union (текущий ledger ∪ ledger снимка) из A5.2b2.
        """
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        store = StateStore(data_dir)
        store.add_history_item(text="живая запись")
        # Владелец удалил запись ДО снимка → её ID попадает в ledger снимка.
        removed_id = store.add_history_item(text="удалённая до снимка").id
        store.delete_history_item(removed_id)
        store.compact_with_stats()
        # Ключ ПРОФИЛЯ (тот, что создал StateStore), а не новый случайный.
        crypto = _store_crypto(store)

        # Ledger профиля = ID удалённой записи (записан compact'ом) + ID,
        # которого в самой истории уже нет. Оба обязаны пережить в снимок.
        _write_ledger(
            data_dir, "history_purged_ids.ndjson", [removed_id, "ghost-from-snapshot"], crypto
        )
        snapshot_dir = _make_snapshot(data_dir, crypto)
        snapshot_ids = set(_live_ids(snapshot_dir, crypto, "history_purged_ids.ndjson"))
        assert removed_id in snapshot_ids, "снимок обязан нести свой ledger"

        # Внешняя копия: снимок унесён из профиля (purge сносит backups/).
        external = tmp_path / "external" / "snapshot_1"
        external.parent.mkdir(parents=True, exist_ok=True)
        for src in sorted(snapshot_dir.rglob("*")):
            if src.is_file():
                dst = external / src.relative_to(snapshot_dir)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())

        HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})
        # B1: ledger переживает purge ОТКРЫТЫМ (шифротекст уничтожен) — его
        # содержимое держит защиту для ID, удалённых уже ПОСЛЕ снимка.
        ledger_after = data_dir / "history_purged_ids.ndjson"
        assert ledger_after.exists(), "ledger не удаляется целиком (B1)"
        assert not any(
            line.startswith("ENC1:") for line in ledger_after.read_text(encoding="utf-8").splitlines()
        ), "шифротекст обязан быть уничтожен"

        # Владелец вернул копию снаружи и восстановился.
        restored_backups = data_dir / "backups" / "snapshot_1"
        restored_backups.parent.mkdir(parents=True, exist_ok=True)
        for src in sorted(external.rglob("*")):
            if src.is_file():
                dst = restored_backups / src.relative_to(external)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())

        restore_encrypted_snapshot(
            data_dir=data_dir,
            backups_root=data_dir / "backups",
            snapshot_dir=restored_backups,
            crypto=crypto,
            policy_read=lambda: True,
        )

        live = _live_ids(data_dir, crypto, "history.ndjson")
        assert removed_id not in live, "ledger снимка обязан блокировать resurrection"
        assert "ghost-from-snapshot" in set(
            collect_ledger_union(data_dir=data_dir, crypto=crypto)
        )


# ---------------------------------------------------------------------------
# B1 (BLOCK, adversarial-ревью) — удаление ledger'а открывало resurrection
# ---------------------------------------------------------------------------


def _plain_ledger_ids(ledger: Path) -> set[str]:
    """ID из ledger'а, читаемые БЕЗ ключа (т.е. пережившие purge открытыми)."""
    ids: set[str] = set()
    for line in ledger.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        assert not line.startswith("ENC1:"), "функция только для открытого ledger'а"
        item_id = json.loads(line).get("id")
        if item_id:
            ids.add(str(item_id))
    return ids


def _resurrection_scenario(tmp_path: Path, root: str):
    """Профиль, в котором ID удалён ПОСЛЕ снимка.

    Возвращает ``(data_dir, store, crypto, external_copy_dir, id_A, id_B)``.

    Ключевая расстановка: снимок снят, когда A и B **оба живы**, поэтому ledger
    САМОЙ копии про A ничего не знает. Единственная защита A — ledger текущего
    профиля. Именно её и проверяют обе ноги теста.
    """
    data_dir = tmp_path / root / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "settings.json").write_text(
        json.dumps({"history_encryption_enabled": True}), encoding="utf-8"
    )
    store = StateStore(data_dir)
    id_a = store.add_history_item(text="ЗАПИСЬ A").id
    id_b = store.add_history_item(text="ЗАПИСЬ B").id
    crypto = _store_crypto(store)

    snapshot_dir = _make_snapshot(data_dir, crypto)
    assert id_a in set(_live_ids(snapshot_dir, crypto, "history.ndjson")), (
        "прогресс-условие: в снимке A жива — иначе resurrection-тест был бы пустым"
    )
    assert id_a not in set(_live_ids(snapshot_dir, crypto, "history_purged_ids.ndjson")), (
        "прогресс-условие: ledger снимка про A не знает — защита держится на "
        "ledger текущего профиля, и только она"
    )

    # Внешняя копия (purge сносит backups/, поэтому выносим за пределы профиля).
    external = tmp_path / root / "external" / "snapshot_1"
    external.parent.mkdir(parents=True, exist_ok=True)
    for src in sorted(snapshot_dir.rglob("*")):
        if src.is_file():
            dst = external / src.relative_to(snapshot_dir)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())

    # ПОСЛЕ снимка владелец удаляет A → ID попадает в ledger текущего профиля.
    store.delete_history_item(id_a)
    store.compact_with_stats()
    return data_dir, store, crypto, external, id_a, id_b


def _return_external_copy(data_dir: Path, external: Path) -> Path:
    """Владелец вернул копию снаружи — как это делает restore."""
    restored = data_dir / "backups" / "snapshot_1"
    restored.parent.mkdir(parents=True, exist_ok=True)
    for src in sorted(external.rglob("*")):
        if src.is_file():
            dst = restored / src.relative_to(external)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
    return restored


class TestPurgeDoesNotOpenResurrection:
    """B1: purge не имеет права ОТКРЫВАТЬ resurrection, который был закрыт."""

    def test_control_leg_blocks_id_before_purge(self, tmp_path, fake_keychain):
        """КОНТРОЛЬНАЯ нога: ДО purge защита работает.

        Без этой ноги тест ниже нельзя интерпретировать: «A не воскресла» после
        фикса может означать и «защита есть», и «тест вообще не проверяет
        resurrection». Контроль доказывает, что сценарий воспроизводит
        resurrection, а фильтр действительно срабатывает.
        """
        data_dir, _store, crypto, external, id_a, id_b = _resurrection_scenario(
            tmp_path, "control"
        )

        restored = _return_external_copy(data_dir, external)
        result = restore_encrypted_snapshot(
            data_dir=data_dir,
            backups_root=data_dir / "backups",
            snapshot_dir=restored,
            crypto=crypto,
            policy_read=lambda: True,
        )

        assert result["ledger_blocked"] >= 1, (
            "контрольная нога: текущий ledger обязан блокировать A ДО purge"
        )
        live = _live_ids(data_dir, crypto, "history.ndjson")
        assert id_a not in live, "A не воскресает, пока ledger цел"
        assert id_b in live, "B возвращается — сценарий не вырожден в пустоту"

    def test_id_deleted_after_snapshot_does_not_resurrect_after_purge(
        self, tmp_path, fake_keychain
    ):
        """B1, главный кейс: purge + возврат внешней копии — A не воскресает.

        Дифференциальная проба ревьюера, воспроизведённая тестом:
          CONTROL (ledger сохранён):  ledger_blocked: 1 → live = [B]
          A5.2c1 (ledger удалён):     ledger_blocked: 0 → live = [B, A]

        То есть раньше purge ВОЗВРАЩАЛ ПОЛНЫЙ ТЕКСТ записи — и рапортовал об
        этом молча (`ledger_blocked: 0`), без единого предупреждения в ответе,
        audit-логе и доках.
        """
        data_dir, store, crypto, external, id_a, id_b = _resurrection_scenario(
            tmp_path, "hole"
        )

        HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})

        restored = _return_external_copy(data_dir, external)
        result = restore_encrypted_snapshot(
            data_dir=data_dir,
            backups_root=data_dir / "backups",
            snapshot_dir=restored,
            crypto=crypto,
            policy_read=lambda: True,
        )

        assert result["ledger_blocked"] >= 1, (
            "ledger текущего профиля обязан пережить purge и блокировать A"
        )
        live = _live_ids(data_dir, crypto, "history.ndjson")
        assert id_a not in live, "resurrection: A воскресла после purge"
        # Полный wipe: purge tombstone'ит ВСЁ, поэтому ledger содержит id и B, и
        # возвращённый снимок не имеет права принести обратно ничего. Это строже
        # требования ревьюера (только «A не воскресла»), и это правильно: после
        # privacy-purge профиль пуст, а не «почти пуст».
        assert id_b not in live, (
            "после полного wipe возврат внешней копии не должен воскрешать НИЧЕГО"
        )
        assert live == [], "после purge + возврата внешней копии профиль обязан быть пуст"

    def test_compaction_invariant_still_holds_and_survives_purge(
        self, tmp_path, fake_keychain
    ):
        """(в) Инвариант «ledger ДО очистки tombstones» и его выживание.

        Порядок compact'а (append+fsync permanent ledger → только потом чистка
        tombstones) — то, что делает ledger'ом вообще. Purge обязан его не
        сломать: перематериализация в открытый вид идёт по уже записанному
        ledger'у, а не по tombstones.
        """
        data_dir, store, crypto, _external, id_a, _id_b = _resurrection_scenario(
            tmp_path, "invariant"
        )
        tombstones = data_dir / "history_tombstones.ndjson"

        # До purge: ledger заполнен, tombstones уже очищены (инвариант W1756).
        raw = (data_dir / "history_purged_ids.ndjson").read_text(encoding="utf-8")
        assert any(line.startswith("ENC1:") for line in raw.splitlines())
        if tombstones.exists():
            assert tombstones.read_text(encoding="utf-8").strip() == "", (
                "tombstones обязаны быть очищены ПОСЛЕ записи в permanent ledger"
            )

        HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})

        ledger = data_dir / "history_purged_ids.ndjson"
        assert id_a in _plain_ledger_ids(ledger), (
            "перематериализация обязана сохранить ID, а не начать с нуля"
        )
        # Оба ридера принимают открытый ledger — проверяем на живых вызовах.
        assert id_a in set(collect_ledger_union(data_dir=data_dir, crypto=crypto))
        assert id_a in store._load_deleted_ids_unlocked()

    def test_purge_reports_preserved_ledger_ids(self, tmp_path, fake_keychain):
        """B1: владелец видит по ОТВЕТУ, сколько ID пережило purge.

        Ноль при `complete: true` означал бы «реестр resurrection потерян, но всё
        прошло» — ровно тот молчаливый all-clear, которого карточка не хочет.
        """
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        store = StateStore(data_dir)
        first = store.add_history_item(text="первая").id
        second = store.add_history_item(text="вторая").id
        store.delete_history_item(first)
        store.delete_history_item(second)
        store.compact_with_stats()

        result = HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})

        # purge tombstone'ит ВСЁ, поэтому к двум удалённым добавляется ещё и
        # запись, удалённая самим purge (шаг 1).
        assert result["deletion_ledger_ids_preserved"] >= 2, (
            "пережившие ID обязаны быть видны в ответе purge"
        )
        assert result["complete"] is True, "нормальный путь — purge полон"
        assert result["errors"] == []

    def test_ledger_carries_no_pii(self, tmp_path, fake_keychain):
        """Инвариант allowlist'а: в ledger'е только ID, без текста.

        Решение B1 держит ledger живым — значит, в нём не должно быть ничего,
        кроме идентификаторов. Иначе мы бы сохранили PII.
        """
        data_dir, store, _crypto, _external, removed_id, _b = _resurrection_scenario(
            tmp_path, "pii"
        )
        store.add_history_item(text="ТАЙНОЕ СОДЕРЖИМОЕ ВЛАДЕЛЬЦА")
        HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})

        blob = (data_dir / "history_purged_ids.ndjson").read_text(encoding="utf-8")
        assert "ТАЙНОЕ СОДЕРЖИМОЕ" not in blob
        for line in blob.splitlines():
            if line.strip():
                assert set(json.loads(line)) == {"id"}, "ledger хранит ТОЛЬКО id"


class TestPurgeResultIsMachineReadable:
    def test_purge_reports_every_new_field(self, tmp_path, fake_keychain):
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        store = StateStore(data_dir)
        store.add_history_item(text="секрет владельца")
        (data_dir / "history.ndjson.bak-20260926-120000").write_text("x", encoding="utf-8")
        (data_dir / "settings.json.bak").write_text("{}", encoding="utf-8")
        (data_dir / "backups" / "migration_backup_20260926").mkdir(parents=True)

        result = HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})

        assert result["encryption_key_shredded"] is True
        assert result["deletion_ledger_purged"] is True
        assert result["stale_copies_removed"] == 2, "history.bak + settings.bak"
        assert result["backups_deleted"] == 1, "backups/migration_backup_20260926"
        assert result["history_encryption_enabled_after"] is True, (
            "purge НЕ переключает политику — это решение владельца"
        )
        assert _encryption_enabled_now(data_dir) is True, "флаг в settings не тронут"

    def test_previous_contract_fields_survive(self, tmp_path, fake_keychain):
        """Регрессия контракта: прежние поля на месте, confirm-гейт работает."""
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        store = StateStore(data_dir)
        store.add_history_item(text="секрет владельца")
        svc = HistoryService(store=store)

        refused = svc.handle_purge_all_data({})
        assert refused["ok"] is False
        assert refused["error"] == "confirmation_required"
        assert (data_dir / "history.ndjson").read_text(encoding="utf-8").strip() != ""

        result = svc.handle_purge_all_data({"confirm": True})
        for field in (
            "ok",
            "history_deleted",
            "chains_deleted",
            "archive_deleted",
            "bookmarks_deleted",
            "call_sessions_deleted",
            "transcripts_deleted",
            "rescue_deleted",
            "obsidian_deleted",
            "semantic_purged",
            "complete",
            "errors",
        ):
            assert field in result, f"прежнее поле {field} пропало из ответа purge"

    def test_purge_result_carries_no_secret_values(self, tmp_path, fake_keychain):
        """В ответе/логе не должно быть ни ключа, ни содержимого .bak-копий."""
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        store = StateStore(data_dir)
        store.add_history_item(text="секрет владельца")
        (data_dir / "settings.json.bak").write_text(
            json.dumps({"hf_token": "СЕКРЕТ-МАРКЕР", "llm_api_key": "ЕЩЁ-СЕКРЕТ"}),
            encoding="utf-8",
        )

        result = HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})
        blob = json.dumps(result, ensure_ascii=False, default=str)

        assert "СЕКРЕТ-МАРКЕР" not in blob
        assert "ЕЩЁ-СЕКРЕТ" not in blob
        assert "секрет владельца" not in blob

    def test_failed_key_deletion_is_not_reported_as_shredded(
        self, tmp_path, fake_keychain, monkeypatch
    ):
        """Fail-closed: `security delete` не сработал → «shredded» быть НЕ может.

        Найдено пробой OS-уровня: ``delete_history_key()`` глотал неудачный
        exit code (только лог), поэтому поле рапортовало бы «ключ уничтожен»,
        хотя живой ключ + pre-purge бэкап = вся история (именно тот аргумент,
        ради которого шаг shred'а вообще существует). Это recurring-класс
        «fail-open в safety-проверке» — здесь он закрывается.

        Патчится ``_run_security`` (НЕ сама delete_history_key): так реальная
        функция keystore отрабатывает и возвращает неуспех, как в проде.
        """
        import backend.crypto_keystore as ks

        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        store = StateStore(data_dir)
        store.add_history_item(text="секрет владельца")

        def _delete_denied(args, *_a, **_kw):
            if args[0] == "delete-generic-password":
                return subprocess.CompletedProcess(
                    list(args), 51, "", "User interaction is not allowed"
                )
            return fake_keychain.run(args)

        monkeypatch.setattr(ks, "_run_security", _delete_denied)

        result = HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})

        assert ("KrabEar", "history-encryption-key") in fake_keychain.items, (
            "проба должна быть построена так, чтобы ключ реально выжил"
        )
        assert result["encryption_key_shredded"] is False, (
            "не удалось shred'ить ключ — рапортовать об успехе нельзя"
        )

    def test_failed_key_deletion_marks_purge_incomplete(
        self, tmp_path, fake_keychain, monkeypatch
    ):
        """Выживший ключ делает purge ЧАСТИЧНЫМ — `complete` обязан это сказать.

        Найдено разбором кода рядом с fail-closed shred'ом: у поля
        `encryption_key_shredded` есть честное значение, но сам purge при этом
        рапортовал `complete: true, errors: []` — то есть ШАГОВЫЙ механизм
        W1749 («loud error when purge is only partial») обходил именно тот
        случай, ради которого он написан: живой ключ + pre-purge бэкап = вся
        история. Владелец читает `complete` как «зачистил всё».
        """
        import backend.crypto_keystore as ks

        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        store = StateStore(data_dir)
        store.add_history_item(text="секрет владельца")

        def _delete_denied(args, *_a, **_kw):
            if args[0] == "delete-generic-password":
                return subprocess.CompletedProcess(
                    list(args), 51, "", "User interaction is not allowed"
                )
            return fake_keychain.run(args)

        monkeypatch.setattr(ks, "_run_security", _delete_denied)

        result = HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})

        assert "encryption_key" in result["errors"], (
            "не shred'ённый ключ — шаговая ошибка, а не «всё прошло»"
        )
        assert result["complete"] is False, "purge неполон ⇒ complete обязан быть False"

    def test_missing_keystill_counts_as_shredded(self, tmp_path, fake_keychain, monkeypatch):
        """Обратная сторона: НЕТ Keychain (Linux/CI) — не ошибка purge.

        Асимметрия, которую легко сломать «на всякий случай»: если шумно
        ругаться на отсутствие Keychain, то на ubuntu-CI purge станет
        «частичным» всегда. KeystoreUnavailable = «ключа на этой платформе
        не существует» ⇒ shred истинен, шаг не в errors.
        """
        import backend.crypto_keystore as ks

        def _no_cli(args, *_a, **_kw):
            raise ks.KeystoreUnavailable("security CLI недоступен")

        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        store = StateStore(data_dir)
        store.add_history_item(text="секрет владельца")

        monkeypatch.setattr(ks, "_run_security", _no_cli)

        result = HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})

        assert result["encryption_key_shredded"] is True
        assert "encryption_key" not in result["errors"], (
            "отсутствие Keychain — не ошибка purge (иначе CI всегда «частичный»)"
        )

    def test_delete_history_key_reports_outcome_to_caller(self, tmp_path, monkeypatch):
        """`delete_history_key` обязан различать «удалил» и «не смог».

        Без возвращаемого значения вызывающий (purge) не может отличить
        успешный shred от молча проглоченной ошибки.
        """
        import backend.crypto_keystore as ks

        monkeypatch.setattr(
            ks, "_run_security", lambda args, *a, **k: subprocess.CompletedProcess(list(args), 0, "", "")
        )
        assert ks.delete_history_key() is True, "rc=0 — ключ удалён"

        monkeypatch.setattr(
            ks,
            "_run_security",
            lambda args, *a, **k: subprocess.CompletedProcess(
                list(args), 44, "", "The specified item could not be found in the keychain."
            ),
        )
        assert ks.delete_history_key() is True, "ключа не было — «уничтожен» истинно"

        monkeypatch.setattr(
            ks,
            "_run_security",
            lambda args, *a, **k: subprocess.CompletedProcess(list(args), 51, "", "not allowed"),
        )
        assert ks.delete_history_key() is False, "отказ Keychain — НЕ «уничтожен»"


# ---------------------------------------------------------------------------
# Task 2 — RED: признак ключа в get_diagnostics, строго read-only
# ---------------------------------------------------------------------------


class _DiagStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def count_active_items(self, lock_timeout_sec=None, nowait: bool = False) -> int:
        return 3


class _DiagSettings:
    _cache_ttl = 5
    _cache: dict = {}

    def cached_settings(self, nowait: bool = False) -> dict:
        return {}


def _diag_service(data_dir: Path):
    from backend.health_check_service import HealthCheckService

    return HealthCheckService(
        store=_DiagStore(data_dir),
        health_checker=None,
        startup_diagnostics=None,
        integrity_checker=None,
        llm_probe=None,
        metrics_collector=None,
        transcriber=None,
        llm_rewriter=None,
        settings_svc=_DiagSettings(),
        start_time=0.0,
        app_version="a52c1-test",
        recorder=None,
        last_stt_engine_ref=["mlx-whisper"],
    )


class TestDiagnosticsKeyPresenceProbe:
    def test_diagnostics_reports_key_presence_without_creating_it(self, tmp_path, fake_keychain):
        """Проба «есть ли ключ» не создаёт ключ — это и есть её смысл."""
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        assert ("KrabEar", "history-encryption-key") not in fake_keychain.items

        before = _snapshot_counters()
        diag = _diag_service(data_dir).handle_get_diagnostics({})
        after = _delta(before)

        probe = diag["history_encryption"]
        assert probe["key_present"] is False, "на профиле без ключа — false"
        assert ("KrabEar", "history-encryption-key") not in fake_keychain.items, (
            "диагностика не должна восстанавливать ключ побочным эффектом"
        )
        assert after["creates"] == 0, "проба не создаёт ключ"
        assert after["reads"] == 0, "проба не читает ключевой материал"
        assert after["probes"] >= 1, "проба обязана быть read-only find без -w"
        assert after["deletions"] == 0

    def test_diagnostics_reports_present_key(self, tmp_path, fake_keychain):
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        fake_keychain.items[("KrabEar", "history-encryption-key")] = os.urandom(32)

        diag = _diag_service(data_dir).handle_get_diagnostics({})

        assert diag["history_encryption"]["key_present"] is True

    def test_diagnostics_probe_never_leaks_key_material(self, tmp_path, fake_keychain):
        """Ни ключ, ни его base64 не попадают в диагностику."""
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        raw = os.urandom(32)
        fake_keychain.items[("KrabEar", "history-encryption-key")] = raw

        diag = _diag_service(data_dir).handle_get_diagnostics({})
        blob = json.dumps(diag, ensure_ascii=False, default=str)

        assert base64.b64encode(raw).decode() not in blob
        assert raw.hex() not in blob

    def test_diagnostics_survives_unavailable_keystore(self, tmp_path, monkeypatch):
        """Keychain недоступен → проба честно сообщает, а не роняет диагностику."""
        import backend.crypto_keystore as ks

        def _boom(args, *_a, **_kw):
            raise ks.KeystoreUnavailable("Keychain недоступен")

        monkeypatch.setattr(ks, "_run_security", _boom)
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)

        diag = _diag_service(data_dir).handle_get_diagnostics({})

        assert diag["history_encryption"]["key_present"] is None, (
            "неопределённость должна отличаться от «ключа нет»"
        )


# ---------------------------------------------------------------------------
# Гейт полноты: audit_purge_coverage не должен быть слепым к .bak-семействам
# ---------------------------------------------------------------------------


class TestPurgeCoverageGateSeesBakFamilies:
    """`audit_purge_coverage` — единственный гейт полноты purge.

    Дыра, найденная в этой волне: сканер не видел `*.bak*` (PERSIST_EXTENSIONS
    их не содержит, а `_record_glob` отбрасывает шаблон без persist-расширения)
    ⇒ удаление `.bak`-копий не было обязательным для гейта, и его откат не был
    бы замечен. Проба ниже требует, чтобы семейство было ВИДИМО.
    """

    def _guard(self):
        import importlib.util
        import sys as _sys

        path = Path(__file__).resolve().parents[2] / "scripts" / "audit_purge_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_purge_coverage_c1", path)
        mod = importlib.util.module_from_spec(spec)
        _sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_guard_records_bak_family_as_a_store(self):
        guard = self._guard()

        found: dict = {}
        guard._record_glob(found, "history_service", "x.py", "history.ndjson.bak*", 1)

        assert found, "audit_purge_coverage обязан видеть .bak-семейство как хранилище"

    def test_real_repo_has_no_uncovered_gaps(self, tmp_path):
        guard = self._guard()
        result = guard.run_audit()
        assert result.gaps == [], f"пробелы полноты purge: {[g.store_id for g in result.gaps]}"

    def test_ledger_is_no_longer_allowlisted(self):
        guard = self._guard()
        allowlisted = guard.load_allowlist()
        assert "history_purged_ids.ndjson" not in allowlisted, (
            "ledger больше не allowlisted-исключение — purge его чистит"
        )

    def test_allowlist_keeps_its_justification_comment_for_ledger(self):
        raw = (
            Path(__file__).resolve().parents[2] / "scripts" / "purge_coverage_allowlist.txt"
        ).read_text(encoding="utf-8")
        assert "history_purged_ids.ndjson" in raw, (
            "решение о снятии ledger'а с allowlist обязано быть задокументировано "
            "в самом файле allowlist — иначе следующий волнёц снова его вернёт"
        )


# ---------------------------------------------------------------------------
# M1/M2/L1 — гейт полноты не должен быть слепым и не должен «проверять сам себя»
# ---------------------------------------------------------------------------


def _guard_repo_copy(tmp_root: Path) -> Path:
    """Копия репозитория, достаточная гейту (он чистый AST — импортов не тянет).

    Нужна для negative control: мутация в tmp-копии обязана давать `gaps > 0`.
    Проверять мутацию на рабочем дереве нельзя — это единственный гейт, который
    защищает purge от отката, и «проверить его, сломав его» на живом коде
    означало бы оставлять репозиторий сломанным.
    """
    import shutil

    repo = Path(__file__).resolve().parents[2]
    for rel in ("scripts", "KrabEar/backend", "KrabEar/core"):
        src = repo / rel
        dst = tmp_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
    return tmp_root


def _audit_mutated(tmp_root: Path, rel: str, old: str, new: str = "") -> set:
    """Прогнать гейт по мутированной копии; вернуть множество gap-идов.

    Мутируется файл ``rel`` (обычно purge), а гейт загружается из той же копии —
    иначе проверялось бы не то. Мутация обязана НАЙТИ свой кусок (``assert``
    вместо тихого no-op): иначе negative control проверяет не то, что думает.
    """
    import importlib.util
    import sys as _sys

    path = tmp_root / rel
    raw = path.read_text(encoding="utf-8")
    assert old in raw, f"negative control: мутация не нашла свой кусок в {rel}"
    path.write_text(raw.replace(old, new, 1), encoding="utf-8")

    guard_path = tmp_root / "scripts" / "audit_purge_coverage.py"
    spec = importlib.util.spec_from_file_location(
        f"apc_mut_{abs(hash((rel, old)))}", guard_path
    )
    mod = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return {gap.store_id for gap in mod.run_audit().gaps}


class TestPurgeCoverageGateTempFamilies:
    """M1: гейт обязан видеть `.tmp`/`_tmp`-семейства (он был слеп к ним)."""

    def _guard(self):
        import importlib.util
        import sys as _sys

        path = Path(__file__).resolve().parents[2] / "scripts" / "audit_purge_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_purge_coverage_tmp", path)
        mod = importlib.util.module_from_spec(spec)
        _sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_temp_families_are_discovered_and_covered(self):
        guard = self._guard()
        result = guard.run_audit()
        # `discovered` — dict, `covered` — set: приводим оба к множеству имён.
        discovered = set(result.discovered)
        covered = set(result.covered)

        for family in ("*.tmp", "*_tmp"):
            assert family in discovered, f"{family} не обнаружен гейтом"
            assert family in covered, f"sweep {family} не засчитан как покрытие"

    def test_temp_family_ends_are_distinguished(self):
        guard = self._guard()
        assert guard._temp_family("settings.json.tmp") == "*.tmp"
        assert guard._temp_family("history.ndjson.migration_tmp") == "*_tmp", (
            "`*.tmp` не матчит `migration_tmp` — в этом весь смысл разных семейств"
        )
        assert guard._temp_family("history.ndjson.bak") is None

    def test_negative_control_dropping_star_tmp_sweep_is_a_gap(self, tmp_path):
        gaps = _audit_mutated(
            _guard_repo_copy(tmp_path / "nc"),
            "KrabEar/backend/history_service.py",
            '+ list(_data_dir.glob("*.tmp"))',
        )
        assert "*.tmp" in gaps, (
            f"снятие sweep '*.tmp' обязано открыть пробел; получили: {sorted(gaps)}"
        )

    def test_negative_control_dropping_underscore_tmp_sweep_is_a_gap(self, tmp_path):
        gaps = _audit_mutated(
            _guard_repo_copy(tmp_path / "nc"),
            "KrabEar/backend/history_service.py",
            '+ list(_data_dir.glob("*_tmp"))',
        )
        assert "*_tmp" in gaps, (
            f"снятие sweep '*_tmp' обязано открыть пробел; получили: {sorted(gaps)}"
        )


class TestPurgeCoverageGateChecksItsOwnMainChange:
    """M2: `_state_store_compaction_coverage` не должен засчитывать append-only.

    Найдено ревьюером (NC2): правило считало ЛЮБОЕ упоминание атрибута внутри
    `_compact_unlocked` признаком «журнал очищен» — включая `purged_ids_path`,
    куда идёт ТОЛЬКО append+fsync. Поэтому удаление шага 37a из purge проходило
    как «0 gaps»: гейт проверял не своё главное изменение.
    """

    def _guard(self):
        import importlib.util
        import sys as _sys

        path = Path(__file__).resolve().parents[2] / "scripts" / "audit_purge_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_purge_coverage_m2", path)
        mod = importlib.util.module_from_spec(spec)
        _sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_append_only_ledger_is_not_credited_as_compacted(self):
        guard = self._guard()
        cleared = guard._state_store_compaction_coverage()

        assert "history_purged_ids.ndjson" not in cleared, (
            "permanent ledger в _compact_unlocked ТОЛЬКО дописывается (append+fsync) — "
            "засчитывать его как «очищенный» значит скрывать отсутствие шага purge"
        )
        for genuinely_truncated in (
            "history.ndjson",
            "history_tombstones.ndjson",
            "history_status.ndjson",
            "history_annotations.ndjson",
        ):
            assert genuinely_truncated in cleared, (
                f"{genuinely_truncated} реально усекается compact'ом — должен считаться"
            )

    def test_negative_control_removing_purge_ledger_step_is_a_gap(self, tmp_path):
        """NC2 ревьюера: снять шаг 37a из purge (оставив новый allowlist)."""
        gaps = _audit_mutated(
            _guard_repo_copy(tmp_path / "nc"),
            "KrabEar/backend/history_service.py",
            '_ledger_path = _data_dir / "history_purged_ids.ndjson"',
            '_ledger_path = _data_dir / "history_purged_ids_DISABLED.ndjson"',
        )
        assert "history_purged_ids.ndjson" in gaps, (
            "без шага 37a ledger нечем покрыть — гейт обязан сказать; "
            f"получили gaps: {sorted(gaps)}"
        )


class TestPurgeCoverageFamilyRuleNeedsWildcard:
    """L1: семейное покрытие нельзя выдавать по записи БЕЗ wildcard.

    NC3 ревьюера: `history.ndjson.bak*` → `history.ndjson.bak` оставлял гейт
    зелёным, хотя все таймстемп-копии (реальная утечка) выживали. Правило (5)
    сравнивало только каноническое семейство, не требуя, чтобы запись пула сама
    была семейной.
    """

    def _guard(self):
        import importlib.util
        import sys as _sys

        path = Path(__file__).resolve().parents[2] / "scripts" / "audit_purge_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_purge_coverage_l1", path)
        mod = importlib.util.module_from_spec(spec)
        _sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_non_wildcard_entry_does_not_cover_the_family(self):
        guard = self._guard()
        member = "history.ndjson.bak-20260926"  # таймстемп-вариант семейства

        assert guard._is_covered(member, {"history.ndjson.bak"}, set()) is False, (
            "запись пула без wildcard не имеет права закрыть семейство"
        )
        assert guard._is_covered(member, {"history.ndjson.bak*"}, set()) is True, (
            "wildcard-запись закрывает семейство"
        )

    def test_exact_match_still_covers_itself(self):
        """Правило (1) не сломан: точное имя покрывает само себя."""
        guard = self._guard()
        assert guard._is_covered(
            "history.ndjson.bak", {"history.ndjson.bak"}, set()
        ) is True

    def test_temp_family_is_not_closed_by_a_single_member(self):
        guard = self._guard()
        assert guard._is_covered("*.tmp", {"settings.json.tmp"}, set()) is False, (
            "одна tmp-копия не означает sweep семейства"
        )
        assert guard._is_covered("*.tmp", {"*.tmp"}, set()) is True


# ---------------------------------------------------------------------------
# KEYCHAIN: два доказательства + инвариант «вне purge обращений нет»
# ---------------------------------------------------------------------------


def test_keychain_never_reached_at_os_level(_forbid_security_cli):
    """Доказательство 1: настоящий `security(1)` не вызван ни разу за сессию."""
    assert _forbid_security_cli == SECURITY_SHIM_MARKER
    assert not SECURITY_SHIM_MARKER.exists(), SECURITY_SHIM_MARKER.read_text(encoding="utf-8")


def test_shim_actually_shadows_the_real_binary():
    """Сам шим работает: иначе «нулевой маркер» ничего не доказывает."""
    import shutil

    shim = Path(os.environ["PATH"].split(os.pathsep)[0]) / "security"
    assert shutil.which("security") == str(shim), "PATH-тень не перекрывает настоящий бинарь"
    assert os.access(shim, os.X_OK)


class TestNoKeychainAccessOutsidePurge:
    """Инвариант карточки: вне шага purge обращений к Keychain нет."""

    def test_reading_and_building_stores_never_touch_keystore(self, tmp_path, fake_keychain):
        """Обычная работа профиля не удаляет ключ; удаляет его ТОЛЬКО purge.

        Ключ кладётся в поддельный Keychain заранее (как в живом профиле владельца),
        поэтому ленивая инициализация StateStore его ЧИТАЕТ, а не создаёт —
        счётчик ``creates`` обязан остаться нулём, иначе тест врал бы и подменял
        крипто-инстанс вместо настоящей ротации.
        """
        data_dir = _data_dir(tmp_path)
        _settings_on(data_dir)
        key = os.urandom(32)
        fake_keychain.items[("KrabEar", "history-encryption-key")] = key
        before = _snapshot_counters()

        store = StateStore(data_dir)
        store.add_history_item(text="секрет владельца")
        store.get_history_page(None, 50)
        assert _delta(before)["creates"] == 0, "ключ уже был — созданий быть не должно"
        assert _delta(before)["deletions"] == 0, "до purge ключ не трогают"

        HistoryService(store=store).handle_purge_all_data({"confirm": "PURGE_ALL"})

        delta = _delta(before)
        assert delta["deletions"] == 1, "удаление ключа — ожидаемый и единственный путь"
        assert delta["creates"] == 0, (
            "purge обязан удалить ключ, а не создать новый на его месте"
        )

    def test_audit_purge_coverage_audit_itself_touches_no_keystore(self):
        """Прогон гейта полноты не должен дёргать Keychain (он чистый AST)."""
        _reset_counters()
        assert KEYCHAIN == {"reads": 0, "creates": 0, "deletions": 0, "probes": 0}
