"""YAML schema validation against PCP JSON schemas."""

import json
from pathlib import Path
from typing import Any

import jsonschema
import yaml

SCHEMA_DIR = Path(__file__).parent.parent.parent.parent / "schema"


def _load_schema(name: str) -> dict:
    path = SCHEMA_DIR / f"{name}.schema.json"
    with open(path) as f:
        return json.load(f)


def validate_file(yaml_path: Path, schema_name: str) -> list[str]:
    """Validate a YAML file against a named schema. Returns list of error messages."""
    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return [f"YAML parse error: {e}"]

    schema = _load_schema(schema_name)
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))

    return [
        f"{'.'.join(str(p) for p in err.path) or 'root'}: {err.message}"
        for err in errors
    ]


def load_yaml(path: Path) -> Any:
    with open(path) as f:
        return yaml.safe_load(f)
