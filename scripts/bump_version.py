#!/usr/bin/env python3
"""
Bump PCP's own package version according to version_rules.json.
Ported from another project's version-control discipline (2026-07-01).

Rules:
  z increments on each commit.
  When z > max_patch: z resets to 0, y increments.
  When y > max_minor: y resets to 0, x increments.

Updates: version field in pyproject.toml (single source of truth).
"""
import json
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent

rules_path = root / "version_rules.json"
pyproject  = root / "pyproject.toml"

rules     = json.loads(rules_path.read_text())
max_minor = int(rules["max_minor"])
max_patch = int(rules["max_patch"])

content = pyproject.read_text()
m = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
if not m:
    print("ERROR: version field not found in pyproject.toml", file=sys.stderr)
    sys.exit(1)

current = m.group(1)
parts = current.split(".")
if len(parts) != 3:
    print(f"ERROR: invalid version '{current}' in pyproject.toml", file=sys.stderr)
    sys.exit(1)

x, y, z = int(parts[0]), int(parts[1]), int(parts[2])

z += 1
if z > max_patch:
    z = 0
    y += 1
if y > max_minor:
    y = 0
    x += 1

new_version = f"{x}.{y}.{z}"
print(f"version: {current} → {new_version}")

pyproject.write_text(
    re.sub(
        r'^(version\s*=\s*)"[^"]+"',
        f'\\g<1>"{new_version}"',
        content,
        flags=re.MULTILINE,
    )
)
