"""DataMigrator не переписывает журнал, где остались ENC1-строки, даже при флаге OFF.

`_read_ndjson` пропускает не-JSON строки, поэтому ENC1-остаток исчезал бы при
raw-rewrite (migrate v1→v2 и rollback). Политика блокировала только флаг ON/unknown;
явный `false` с ENC1-остатком проходил.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.data_migrator import DataMigrator, MigrationResult  # noqa: E402
from backend.history_encryption_policy import OPERATION_UNAVAILABLE_REASON as REASON  # noqa: E402
from backend.plaintext_export_authorization import POLICY_REVISION_KEY  # noqa: E402


def _known_off(data_dir: Path) -> None:
    (data_dir / "settings.json").write_text(
        json.dumps({
            "history_encryption_enabled": False,
            "privacy_mode_enabled": False,
            POLICY_REVISION_KEY: "0123456789abcdef0123456789abcdef",
        }),
        encoding="utf-8",
    )


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


def _mixed_history(data_dir: Path) -> Path:
    history = data_dir / "history.ndjson"
    history.write_text(
        json.dumps({"id": "1", "text": "v1"}) + "\n" + "ENC1:deadbeef\n",
        encoding="utf-8",
    )
    return history


def test_migrate_refuses_when_flag_off_but_enc1_residue_exists(data_dir):
    _known_off(data_dir)
    history = _mixed_history(data_dir)
    before = history.read_bytes()
    result = DataMigrator(data_dir=data_dir).migrate(data_dir)
    assert isinstance(result, MigrationResult)
    assert result.reason == REASON
    assert history.read_bytes() == before
    assert not (data_dir / "backups").exists()


def test_rollback_refuses_when_flag_off_but_enc1_residue_exists(data_dir):
    _known_off(data_dir)
    history = _mixed_history(data_dir)
    backup = data_dir / "backups" / "migration_backup_x"
    backup.mkdir(parents=True)
    (backup / "history.ndjson").write_text('{"id":"restored"}\n', encoding="utf-8")
    before = history.read_bytes()
    result = DataMigrator(data_dir=data_dir).rollback_migration(data_dir, str(backup))
    assert result["ok"] is False and result["reason"] == REASON
    assert history.read_bytes() == before


def test_plain_v1_history_without_residue_still_migrates(data_dir):
    _known_off(data_dir)
    (data_dir / "history.ndjson").write_text(
        json.dumps({"id": "1", "text": "v1"}) + "\n", encoding="utf-8"
    )
    result = DataMigrator(data_dir=data_dir).migrate(data_dir)
    assert result.items_migrated == 1
