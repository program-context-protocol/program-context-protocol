"""Denylist/allowlist for scripts/check_public_hygiene.py and cleanup_public_hygiene.py.

Hand-maintained -- a name that isn't listed here can't be caught. Extend
this file whenever a new private project or personal reference needs
scrubbing from this public repo.
"""

# Pattern (word-boundary, case-insensitive) -> anonymized label the cleanup
# script substitutes in. One entry per project is enough; the check/cleanup
# scripts apply IGNORECASE, so "Postcar" and "postcar" both match the same key.
NAME_DENYLIST = {
    r"ontology-foundry": "Project O",
    r"postcar": "Project P",
    r"win2mac": "Project W",
    r"atacamaMDM": "Project M",
    r"agentberg": "Project A",
    r"geek-squad": "Project G",
    r"signtool": "Project S",
    r"LinkBox": "Project L",
    r"Event-Manager": "Project E",
    r"ganeshnallasivam": "the maintainer",
}

# .md files allowed to exist in the public tree. Anything else tracked as
# *.md fails the check -- this repo ships code, not narrative docs.
MD_ALLOWLIST = {
    "README.md",
    "src/pcp/skill_data/pcp/SKILL.md",  # functional -- read by `pcp install-skill`, not a doc
    "SKILL.md",  # functional -- served/read for `pcp takeover`'s remote self-install flow
                 # (github.com/program-context-protocol org URL, not a personal reference)
}

# Files that legitimately contain denylist terms as literal data (this file,
# the check/cleanup scripts themselves, and the regression test that guards
# the bundled SKILL.md) -- scanning them would just flag their own definitions.
SELF_EXEMPT_FILES = {
    "scripts/public_hygiene_denylist.py",
    "scripts/check_public_hygiene.py",
    "scripts/cleanup_public_hygiene.py",
    "tests/test_install_skill.py",
}

# Tracked-but-binary extensions the text scan should never try to decode.
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".pptx", ".woff", ".woff2",
    ".ttf", ".zip", ".whl",
}
