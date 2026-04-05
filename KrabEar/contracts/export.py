"""Экспорт JSON Schema для событий Krab Ear.

Использование:
    python -m contracts.export --output schemas/
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from contracts.registry import EVENT_SCHEMA_MAP


def export_schemas(output_dir: Path) -> None:
    """Экспортирует JSON Schema для всех зарегистрированных событий."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for event_type, model_cls in EVENT_SCHEMA_MAP.items():
        schema = model_cls.model_json_schema()
        path = output_dir / f"{event_type.value}.schema.json"
        path.write_text(json.dumps(schema, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Krab Ear event JSON schemas")
    parser.add_argument("--output", type=Path, default=Path("contracts/schemas"))
    args = parser.parse_args()
    export_schemas(args.output)
    print(f"Exported {len(EVENT_SCHEMA_MAP)} schemas to {args.output}")


if __name__ == "__main__":
    main()
