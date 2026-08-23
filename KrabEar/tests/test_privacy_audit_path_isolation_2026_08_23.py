"""Изоляция пути privacy_audit.log (инцидент 2026-08-23).

Боевой compliance-журнал ~/Library/Application Support/KrabEar/privacy_audit.log
набрал 44 907 записей privacy/purge_all_data из 50 041 — тестовый мусор. Корень:
путь был захардкожен модульной константой мимо env и мимо data_dir, а
PrivacyAuditLogger — синглтон, поэтому 17 из 20 purge-тестов писали в боевой файл.
Следствие: реальный purge владельца стал неотличим от тестового.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.privacy_audit import (  # noqa: E402
    PrivacyAuditLogger,
    _default_log_path,
)

# Боевой путь, записанный ЯВНО: тест обязан ловить регрессию, даже если
# продовый дефолт в модуле кто-то переопределит.
_PROD_LOG_PATH = (
    Path.home() / "Library" / "Application Support" / "KrabEar" / "privacy_audit.log"
)


def test_env_var_redirects_log_path(tmp_path, monkeypatch):
    """Выставленная переменная уводит журнал в свой каталог."""
    monkeypatch.setenv("KRAB_EAR_PRIVACY_AUDIT_DIR", str(tmp_path))

    logger = PrivacyAuditLogger()

    assert logger._log_path == tmp_path / "privacy_audit.log"


def test_key_file_follows_log_dir(tmp_path, monkeypatch):
    """HMAC-ключ создаётся рядом с журналом, а не в боевом каталоге.

    _load_or_create_key берёт каталог как self._log_path.parent, поэтому ключ
    обязан переехать вместе с журналом без отдельной переменной.
    """
    monkeypatch.setenv("KRAB_EAR_PRIVACY_AUDIT_DIR", str(tmp_path))

    logger = PrivacyAuditLogger()
    logger.log_event("test", "isolation_probe")

    assert (tmp_path / "privacy_audit.key").exists()
    assert (tmp_path / "privacy_audit.log").exists()


def test_no_env_falls_back_to_home_default(monkeypatch):
    """Без переменной — прежний боевой путь (обратная совместимость).

    Зовём ТОЛЬКО чистую функцию: конструктор создал бы каталог и записал ключ
    в боевую директорию, чего тест делать не имеет права.
    """
    monkeypatch.delenv("KRAB_EAR_PRIVACY_AUDIT_DIR", raising=False)

    assert _default_log_path() == _PROD_LOG_PATH


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_env_falls_back_to_default(monkeypatch, blank):
    """Пустое значение трактуется как «не задано» — fail-safe от опечатки.

    Иначе Path("") / "privacy_audit.log" увёл бы compliance-журнал в CWD.
    """
    monkeypatch.setenv("KRAB_EAR_PRIVACY_AUDIT_DIR", blank)

    assert _default_log_path() == _PROD_LOG_PATH


def test_running_test_session_is_not_on_prod_path():
    """🔴 Главный гард инцидента: в ЛЮБОМ тестовом прогоне журнал не боевой.

    Проверяем путь, а не «боевой файл не изменился» по mtime: на ubuntu-CI
    боевого файла не существует, и такой тест был бы вечно-зелёным именно там,
    где прогоняется настоящий гейт.
    """
    from backend.privacy_audit import get_privacy_audit_logger

    logger = get_privacy_audit_logger()

    assert logger._log_path != _PROD_LOG_PATH
    assert _PROD_LOG_PATH.parent not in logger._log_path.parents
