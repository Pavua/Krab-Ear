"""Дрейф списка SENSITIVE_FIELDS: учётные данные не должны попадать в бэкап/экспорт.

Находка ревью R-Krab-Ear-fix-08 (RKE-F01-01), сверенная с кодом 27.11.2026: ``sip_password``
хранится в settings.json открытым текстом, но не входил в ``SENSITIVE_FIELDS`` — поэтому
уезжал в settings_backups/*.json, auto-backup и экспорт настроек. Список был ручным, и новое
поле с паролем молча выпадало из него. Этот тест делает дрейф громким: любой строковый
дефолт с «секретным» именем обязан быть либо в списке, либо в явном allowlist ниже.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.settings_backup import SENSITIVE_FIELDS, SettingsBackup  # noqa: E402
from core.config import DEFAULT_SETTINGS  # noqa: E402

_SECRET_NAME = re.compile(
    r"(secret|token|password|passwd|api_?key|credential|bearer|dsn|private_key|access_key)",
    re.IGNORECASE,
)

# Строковые настройки с «секретным» именем, которые секретом НЕ являются (осознанно).
_NOT_SECRET_ALLOWLIST: frozenset[str] = frozenset()


class SensitiveFieldsDriftTest(unittest.TestCase):
    def test_every_secret_looking_string_default_is_redacted(self):
        missing = sorted(
            key
            for key, value in DEFAULT_SETTINGS.items()
            if isinstance(value, str)
            and _SECRET_NAME.search(key)
            and key not in SENSITIVE_FIELDS
            and key not in _NOT_SECRET_ALLOWLIST
        )
        self.assertEqual(
            missing,
            [],
            "строковые настройки с секретным именем не в SENSITIVE_FIELDS — "
            "добавь в список или осознанно в allowlist этого теста",
        )

    def test_sip_password_is_in_sensitive_fields(self):
        self.assertIn("sip_password", SENSITIVE_FIELDS)

    def test_rolling_backup_does_not_contain_sip_password(self):
        marker = "sip-secret-marker-7f3a"
        with tempfile.TemporaryDirectory(prefix="krab_ear_sip_bk_") as td:
            backup = SettingsBackup(Path(td))
            backup_id = backup.create_backup(
                {"quality_profile": "balanced", "sip_password": marker, "sip_user": "alice"},
                reason="test",
            )
            raw = (Path(td) / f"{backup_id}.json").read_text(encoding="utf-8")
        self.assertNotIn(marker, raw)
        self.assertNotIn("sip_password", json.loads(raw))
        # Несекретное поле соседнего блока не пропало.
        self.assertEqual(json.loads(raw).get("sip_user"), "alice")


if __name__ == "__main__":
    unittest.main()
