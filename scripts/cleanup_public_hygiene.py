#!/usr/bin/env python3
"""Auto-fix routine for scripts/check_public_hygiene.py failures.

Removes disallowed .md files (git rm) and substitutes known denylist terms
with their anonymized label. Only fixes what's already in
public_hygiene_denylist.py -- a brand-new leak (a project name not yet
listed) will still fail the check after this runs. That's a real limit, not
papered over: add the name to the denylist by hand, then re-run the check.
"""
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from public_hygiene_denylist import (
    NAME_DENYLIST, MD_ALLOWLIST, SELF_EXEMPT_FILES, BINARY_EXTENSIONS,
)
from check_public_hygiene import tracked_files, check_md_files


def remove_disallowed_md(files: list[str]) -> list[str]:
    violations = check_md_files(files)
    for f in violations:
        subprocess.run(["git", "rm", "-f", "-q", f], check=True)
        print(f"removed: {f}")
    return violations


def substitute_leaks(files: list[str]) -> list[str]:
    fixed = []
    for f in files:
        if f in SELF_EXEMPT_FILES or Path(f).suffix in BINARY_EXTENSIONS:
            continue
        if f.endswith(".md") and f not in MD_ALLOWLIST:
            continue  # already removed above
        path = Path(f)
        if not path.exists():
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        original = text
        for pattern, label in NAME_DENYLIST.items():
            # See check_public_hygiene.py: (?<!...)/(?!...) instead of \b,
            # since \b misses names embedded in snake_case identifiers.
            text = re.sub(
                rf"(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])", label, text,
                flags=re.IGNORECASE,
            )
        if text != original:
            path.write_text(text)
            fixed.append(f)
    return fixed


def main() -> int:
    files = tracked_files()
    removed = remove_disallowed_md(files)
    remaining = [f for f in files if f not in removed]
    fixed = substitute_leaks(remaining)
    for f in fixed:
        print(f"scrubbed: {f}")
    if not removed and not fixed:
        print("cleanup: nothing to fix -- check is likely failing on a term "
              "not yet in public_hygiene_denylist.py. Add it by hand.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
