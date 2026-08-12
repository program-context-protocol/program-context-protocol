#!/usr/bin/env python3
"""Pre-push / CI gate for this repo's public release: no unrelated .md files,
no personal or other-project references. See public_hygiene_denylist.py for
what's actually checked -- a name not listed there cannot be caught.

Usage: python3 scripts/check_public_hygiene.py
Exit 0 = clean. Exit 1 = violations found, printed to stdout.
"""
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from public_hygiene_denylist import (
    NAME_DENYLIST, MD_ALLOWLIST, SELF_EXEMPT_FILES, BINARY_EXTENSIONS,
)

_LEAK_PATTERN = re.compile(
    # (?<![A-Za-z0-9]) / (?![A-Za-z0-9]) instead of \b -- \b treats '_' as a
    # word char, so it misses names embedded in snake_case identifiers (e.g.
    # a memory-key string like `..._from_win2mac_debug_...`).
    "|".join(rf"(?<![A-Za-z0-9]){p}(?![A-Za-z0-9])" for p in NAME_DENYLIST),
    re.IGNORECASE,
)


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


def check_md_files(files: list[str]) -> list[str]:
    return [f for f in files if f.endswith(".md") and f not in MD_ALLOWLIST]


def check_name_leaks(files: list[str]) -> list[str]:
    hits = []
    for f in files:
        if f in SELF_EXEMPT_FILES or Path(f).suffix in BINARY_EXTENSIONS:
            continue
        path = Path(f)
        if not path.exists():
            continue
        try:
            text = path.read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _LEAK_PATTERN.search(line):
                hits.append(f"{f}:{i}: {line.strip()[:100]}")
    return hits


def main() -> int:
    files = tracked_files()
    md_violations = check_md_files(files)
    name_hits = check_name_leaks(files)

    if not md_violations and not name_hits:
        print("public-hygiene check: clean.")
        return 0

    if md_violations:
        print(f"public-hygiene check FAILED -- {len(md_violations)} unrelated .md file(s):")
        for v in md_violations:
            print(f"  {v}")
    if name_hits:
        print(f"public-hygiene check FAILED -- {len(name_hits)} personal/project reference(s):")
        for h in name_hits[:30]:
            print(f"  {h}")
        if len(name_hits) > 30:
            print(f"  ... and {len(name_hits) - 30} more")
    return 1


if __name__ == "__main__":
    sys.exit(main())
