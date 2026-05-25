"""CI drift guard: проверяет соответствие contracts/schemas/ и EventType реестра.

Wave 163: Wave 162 обнаружил, что 4 из 9 EventType не имели schema-файлов.
Этот suite предотвращает регрессию.

Запуск:
    PYTHONPATH=KrabEar python -m pytest KrabEar/tests/test_contracts_schema_drift.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from contracts.export import export_schemas
from contracts.registry import EventType

SCHEMAS_DIR = PROJECT_ROOT / "contracts" / "schemas"


def _schema_filename(event_type: EventType) -> str:
    """Возвращает имя файла схемы для данного EventType (например 'stt.final.schema.json')."""
    return f"{event_type.value}.schema.json"


class TestEveryEventTypeHasSchemaFile(unittest.TestCase):
    """Каждый EventType должен иметь соответствующий .schema.json на диске."""

    def test_every_event_type_has_schema_file(self):
        missing = []
        for event_type in EventType:
            schema_path = SCHEMAS_DIR / _schema_filename(event_type)
            if not schema_path.exists():
                missing.append(f"{event_type.value} -> {schema_path.name}")
        self.assertEqual(
            missing,
            [],
            f"Missing schema files for {len(missing)} EventType(s):\n"
            + "\n".join(f"  {m}" for m in missing)
            + "\nRun: python -m contracts.export --output KrabEar/contracts/schemas",
        )

    def test_schema_file_is_valid_json(self):
        """Каждый файл схемы должен быть валидным JSON."""
        errors = []
        for event_type in EventType:
            schema_path = SCHEMAS_DIR / _schema_filename(event_type)
            if not schema_path.exists():
                continue  # covered by test_every_event_type_has_schema_file
            try:
                data = json.loads(schema_path.read_text())
                if not isinstance(data, dict):
                    errors.append(f"{schema_path.name}: root is not a JSON object")
            except json.JSONDecodeError as exc:
                errors.append(f"{schema_path.name}: {exc}")
        self.assertEqual(errors, [], "\n".join(errors))


class TestSchemaFileMatchesPydanticModel(unittest.TestCase):
    """Файлы схем на диске должны совпадать с тем, что генерирует Pydantic сейчас.

    Если Pydantic-модель изменилась (новое поле, renamed field и т.д.),
    но schemas/ не были перегенерированы, этот тест упадёт.
    Fix: python -m contracts.export --output KrabEar/contracts/schemas
    """

    def test_schema_files_match_current_pydantic_models(self):
        drifted = []
        with tempfile.TemporaryDirectory() as tmpdir:
            fresh_dir = Path(tmpdir)
            export_schemas(fresh_dir)

            for event_type in EventType:
                fname = _schema_filename(event_type)
                committed = SCHEMAS_DIR / fname
                fresh = fresh_dir / fname

                if not committed.exists():
                    # Already caught by TestEveryEventTypeHasSchemaFile
                    continue

                committed_schema = json.loads(committed.read_text())
                fresh_schema = json.loads(fresh.read_text())

                if committed_schema != fresh_schema:
                    drifted.append(event_type.value)

        self.assertEqual(
            drifted,
            [],
            f"Schema drift detected for {len(drifted)} event type(s): {drifted}\n"
            "Run: python -m contracts.export --output KrabEar/contracts/schemas",
        )


class TestNoOrphanSchemaFile(unittest.TestCase):
    """Все .schema.json файлы в schemas/ должны соответствовать существующему EventType.

    Если EventType был удалён, но файл схемы остался — тест упадёт.
    Fix: удали лишний .schema.json вручную.
    """

    def test_no_orphan_schema_files(self):
        known_filenames = {_schema_filename(et) for et in EventType}
        orphans = []
        for path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
            if path.name not in known_filenames:
                orphans.append(path.name)
        self.assertEqual(
            orphans,
            [],
            f"Orphan schema files (no matching EventType): {orphans}\n"
            "Remove them or add the EventType back to contracts/registry.py",
        )


if __name__ == "__main__":
    unittest.main()
