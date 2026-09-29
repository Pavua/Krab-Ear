"""Тесты покрытия privacy-purge для .secrets.bak* и auto_glossary.json.bak*.

Данный модуль проверяет устранение дефекта P1:
Ранее handle_purge_all_data удалял history.ndjson.bak* и settings.json.bak*,
но пропускал производные копии .secrets.bak* и auto_glossary.json.bak*,
рапортуя при этом complete: True (ложный all-clear).

Инварианты безопасности:
1. Все производные копии .secrets.bak* и auto_glossary.json.bak* должны удаляться.
2. Активный файл .secrets НЕ должен затрагиваться при зачистке бэкапов.
3. Симлинки .secrets.bak* должны удаляться сами без разыменования целевого файла.
4. При невозможности удаления копии purge обязан вернуть честный partial failure
   (complete=False, "stale_copies" в errors).
5. audit_purge_coverage обязан видеть оба семейства и подтверждать нулевой зазор.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
import pytest

from backend.history_service import HistoryService
from backend.state_store import StateStore
import backend.crypto_keystore as ks


@pytest.fixture
def fake_keychain(monkeypatch: pytest.MonkeyPatch):
    """Поддельный Keychain для изоляции крипто-шагов purge в тестах."""
    items = {}

    def _guarded(args, *_a, **_kw):
        argv = list(args)
        if "find-generic-password" in argv and "-w" not in argv:
            # probe
            key = argv[argv.index("-s") + 1] if "-s" in argv else ""
            if key in items:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 44, "", "item not found")
        if "find-generic-password" in argv and "-w" in argv:
            key = argv[argv.index("-s") + 1] if "-s" in argv else ""
            if key in items:
                return subprocess.CompletedProcess(argv, 0, items[key], "")
            return subprocess.CompletedProcess(argv, 44, "", "item not found")
        if "add-generic-password" in argv:
            key = argv[argv.index("-s") + 1] if "-s" in argv else ""
            val = argv[argv.index("-w") + 1] if "-w" in argv else ""
            items[key] = val
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "delete-generic-password" in argv:
            key = argv[argv.index("-s") + 1] if "-s" in argv else ""
            items.pop(key, None)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(ks, "_run_security", _guarded)
    monkeypatch.setattr(ks, "keychain_available", lambda: True)
    return items


def _data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "KrabEar"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestSecretsAndGlossaryBakPurge:
    """Проверка очистки производных копий секретов и глоссария."""

    def test_secrets_bak_and_glossary_bak_are_deleted_while_active_secrets_preserved(
        self, tmp_path: Path, fake_keychain
    ) -> None:
        """Бэкапы секретов и глоссария удаляются, активный .secrets сохраняется."""
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        store.add_history_item(text="запись для инициализации")

        # Активный файл секретов (должен пережить зачистку)
        active_secrets = data_dir / ".secrets"
        active_secrets.write_text("ACTIVE_SECRET_KEY=keep_me_safe\n", encoding="utf-8")

        # Производные копии секретов (должны быть удалены)
        bak1 = data_dir / ".secrets.bak"
        bak1.write_text("OLD_SECRET=1\n", encoding="utf-8")
        bak2 = data_dir / ".secrets.bak-20260929-010000"
        bak2.write_text("OLD_SECRET=2\n", encoding="utf-8")

        # Производные копии авто-глоссария (должны быть удалены)
        g_bak1 = data_dir / "auto_glossary.json.bak"
        g_bak1.write_text('{"terms": ["термин1"]}', encoding="utf-8")
        g_bak2 = data_dir / "auto_glossary.json.bak-20260929"
        g_bak2.write_text('{"terms": ["термин2"]}', encoding="utf-8")

        svc = HistoryService(store=store)
        res = svc.handle_purge_all_data({"confirm": "PURGE_ALL"})

        # Проверка диска: бэкапы удалены
        assert not bak1.exists(), ".secrets.bak должен быть удалён"
        assert not bak2.exists(), ".secrets.bak-* должен быть удалён"
        assert not g_bak1.exists(), "auto_glossary.json.bak должен быть удалён"
        assert not g_bak2.exists(), "auto_glossary.json.bak-* должен быть удалён"

        # Проверка отчёта
        assert res.get("complete") is True, f"Ожидался complete=True, получено {res}"
        assert "stale_copies" not in res.get("errors", [])
        assert res.get("stale_copies_removed", 0) >= 4

        # Активный .secrets обязан остаться нетронутым
        assert active_secrets.exists(), "Активный .secrets обязан остаться на диске"
        assert active_secrets.read_text(encoding="utf-8") == "ACTIVE_SECRET_KEY=keep_me_safe\n"

    def test_symlink_secrets_bak_does_not_dereference_target(
        self, tmp_path: Path, fake_keychain
    ) -> None:
        """Симлинк .secrets.bak удаляется сам, не трогая целевой файл."""
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)

        external_target = tmp_path / "external_secret.txt"
        external_target.write_text("EXTERNAL_DATA", encoding="utf-8")

        symlink_bak = data_dir / ".secrets.bak"
        symlink_bak.symlink_to(external_target)

        svc = HistoryService(store=store)
        res = svc.handle_purge_all_data({"confirm": "PURGE_ALL"})

        assert not symlink_bak.exists(follow_symlinks=False), "Симлинк должен быть удалён"
        assert external_target.exists(), "Целевой файл вне data_dir НЕ должен разыменовываться"
        assert external_target.read_text(encoding="utf-8") == "EXTERNAL_DATA"
        assert res.get("complete") is True

    def test_directory_bak_is_deleted_recursively(self, tmp_path: Path, fake_keychain) -> None:
        """Каталог, названный как .bak-копия, удаляется рекурсивно."""
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)

        bak_dir = data_dir / ".secrets.bak-folder"
        bak_dir.mkdir()
        (bak_dir / "nested_secret.txt").write_text("NESTED", encoding="utf-8")

        svc = HistoryService(store=store)
        res = svc.handle_purge_all_data({"confirm": "PURGE_ALL"})

        assert not bak_dir.exists(), "Каталог .secrets.bak-* должен быть удалён рекурсивно"
        assert res.get("complete") is True

    def test_unremovable_bak_causes_honest_partial_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keychain
    ) -> None:
        """Если копию невозможно удалить, purge возвращает честный partial failure."""
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)

        stuck_bak = data_dir / ".secrets.bak"
        stuck_bak.write_text("STUCK", encoding="utf-8")

        orig_unlink = Path.unlink

        def fake_unlink(self_path: Path, *args, **kwargs):
            if self_path.name == ".secrets.bak":
                raise OSError("Permission denied (simulation)")
            return orig_unlink(self_path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fake_unlink)

        svc = HistoryService(store=store)
        res = svc.handle_purge_all_data({"confirm": "PURGE_ALL"})

        assert res.get("complete") is False, "При неудаче удаления complete обязан быть False"
        assert "stale_copies" in res.get("errors", []), "stale_copies обязан быть в errors"
        assert stuck_bak.exists(), "Файл остался на диске"

    def test_overlapping_bak_and_tmp_pattern_does_not_fail(
        self, tmp_path: Path, fake_keychain
    ) -> None:
        """Файлы, совпадающие одновременно с *.bak* и *.tmp (например .secrets.bak.tmp),
        не должны вызывать повторную попытку удаления или ложный partial failure.
        """
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)

        overlap1 = data_dir / ".secrets.bak.tmp"
        overlap1.write_text("OVERLAP1", encoding="utf-8")
        overlap2 = data_dir / "settings.json.bak.tmp"
        overlap2.write_text("OVERLAP2", encoding="utf-8")

        svc = HistoryService(store=store)
        res = svc.handle_purge_all_data({"confirm": "PURGE_ALL"})

        assert res.get("complete") is True
        assert "stale_copies" not in res.get("errors", [])
        assert not overlap1.exists()
        assert not overlap2.exists()


class TestPurgeCoverageGateForP1Bak:
    """Проверка интеграции новых семейств бэкапов со статическим гейтом audit_purge_coverage."""

    def _guard(self):
        import importlib.util
        import sys as _sys

        path = Path(__file__).resolve().parents[2] / "scripts" / "audit_purge_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_purge_coverage_p1", path)
        mod = importlib.util.module_from_spec(spec)
        _sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_guard_recognizes_secrets_and_glossary_bak_families(self) -> None:
        guard = self._guard()

        # .secrets.bak
        assert guard._looks_like_backup_copy(".secrets.bak") is True
        assert guard._looks_like_backup_copy(".secrets.bak-20260929") is True
        assert guard._backup_family(".secrets.bak*") == ".secrets.bak*"
        assert guard._backup_family(".secrets.bak-20260929") == ".secrets.bak*"

        # auto_glossary.json.bak
        assert guard._looks_like_backup_copy("auto_glossary.json.bak") is True
        assert guard._looks_like_backup_copy("auto_glossary.json.bak-20260929") is True
        assert guard._backup_family("auto_glossary.json.bak*") == "auto_glossary.json.bak*"
        assert guard._backup_family("auto_glossary.json.bak-20260929") == "auto_glossary.json.bak*"

    def test_guard_records_globs_correctly(self) -> None:
        guard = self._guard()
        found: dict = {}
        guard._record_glob(found, "history_service", "x.py", ".secrets.bak*", 1)
        guard._record_glob(found, "history_service", "x.py", "auto_glossary.json.bak*", 2)
        assert ".secrets.bak*" in found
        assert "auto_glossary.json.bak*" in found

    def test_full_repo_audit_has_no_gaps(self) -> None:
        guard = self._guard()
        result = guard.run_audit()
        assert result.gaps == [], f"Обнаружены пробелы audit_purge_coverage: {[g.store_id for g in result.gaps]}"
