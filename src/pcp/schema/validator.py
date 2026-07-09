"""YAML schema validation against PCP JSON schemas."""

import json
from pathlib import Path
from typing import Any

import jsonschema
import yaml

SCHEMA_DIR = Path(__file__).parent
# Real packaging bug, found 2026-07-08: this used to be
# Path(__file__).parent.parent.parent.parent / "schema", assuming a repo-root
# schema/ dir sits four levels above validator.py -- only true in this exact
# dev-repo layout. A real installed package (site-packages/pcp/schema/
# validator.py, editable or not) has no such sibling four levels up, so
# every validate_file() call silently pointed at a nonexistent path for
# anyone using PCP as an actually-installed dependency (confirmed against
# ontology-foundry's own separate venv install). Schemas now live directly
# alongside validator.py, inside the package itself, so the path resolves
# correctly regardless of how or where the package is installed.


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
