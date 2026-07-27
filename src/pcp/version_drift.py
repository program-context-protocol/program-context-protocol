"""Is the PCP running here the PCP that was built from its source?

Every fix PCP ships reaches a frozen-wheel install only when a human remembers
to roll it. Nothing enumerated those installs, so nothing noticed when they
fell behind. On 2026-07-27 that gap surfaced four times in one session, the
worst being a Railway-served wheel that kept distributing a version containing
a daily unverified `curl` overwrite of an agent instruction file for two days
after the fix landed, and two abandoned build worktrees carrying their own
`.venv` at 0.8.6 with the same vulnerability -- both missed by a rollout that
worked from a remembered project list instead of the disk.

The check is deliberately narrow and deterministic (rung 1): compare the
installed distribution's version against the version declared in the source
tree it was built from, when that source tree is locatable. It answers exactly
one question -- "is this install behind its own source?" -- and says so. It
does not fetch anything, contact any network, or attempt an upgrade.

Advisory, never fatal: a project can legitimately pin an older PCP, and a tool
that hard-blocks on its own version would be worse than the drift it reports.
"""

import importlib.metadata as md
import os
import re
from pathlib import Path

import pcp

_VERSION_RE = re.compile(r'^version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)

DIST_NAME = "program-context-protocol"


def installed_version() -> str | None:
    try:
        return md.version(DIST_NAME)
    except Exception:
        return None


def source_root() -> Path | None:
    """The source tree to compare this install against.

    `PCP_SOURCE_ROOT` wins when set. That override is the whole reason a WHEEL
    install can be checked at all: a wheel lives in site-packages with no
    pyproject.toml above it, so without it there is nothing to compare against
    and the check goes quiet — which would have missed the exact case it was
    written for (two abandoned worktree `.venv`s frozen at 0.8.6 while the
    source tree was thirteen releases ahead). Stated plainly rather than
    guessing at sibling paths: an unset override means "cannot verify", not
    "verified fine".
    """
    override = os.environ.get("PCP_SOURCE_ROOT")
    if override:
        root = Path(override).expanduser()
        return root if (root / "pyproject.toml").exists() else None
    try:
        pkg = Path(pcp.__file__).resolve().parent      # .../src/pcp
    except Exception:
        return None
    for candidate in (pkg.parent.parent, pkg.parent):   # repo root, or flat layout
        if (candidate / "pyproject.toml").exists():
            return candidate
    return None


def is_editable() -> bool:
    """Does this install execute the source tree directly?

    The single most repeated mistake in this project's own history: treating a
    stale `pcp --version` as proof of stale BEHAVIOUR. An editable install runs
    source live, so its recorded version goes stale the moment source is bumped
    while the code is already current. A wheel is the opposite — the recorded
    version IS the code. Conflating them either cries wolf on editable installs
    or, far worse, silently downgrades a genuinely outdated wheel to a cosmetic
    warning. Read from packaging metadata (PEP 610 direct_url.json), not guessed
    from paths.
    """
    try:
        dist = md.distribution(DIST_NAME)
        raw = dist.read_text("direct_url.json")
    except Exception:
        return False
    if not raw:
        return False
    try:
        import json
        return bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except Exception:
        return False


def source_version(root: Path) -> str | None:
    try:
        text = (root / "pyproject.toml").read_text()
    except OSError:
        return None
    m = _VERSION_RE.search(text)
    return m.group(1) if m else None


def _as_tuple(v: str) -> tuple:
    parts = []
    for chunk in v.split("."):
        num = "".join(c for c in chunk if c.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def check() -> dict:
    """{"status": ok|behind|stale_metadata|unknown, "message": str, ...}.

    `stale_metadata` is its own outcome and not a problem: an editable install
    executes source directly, so its recorded version string goes stale the
    moment the source is bumped without a reinstall, while the CODE is already
    current. Conflating that with a genuinely behind wheel is exactly the
    mistake this project has made repeatedly by reading `pcp --version`.
    """
    inst = installed_version()
    if inst is None:
        return {"status": "unknown", "message": "PCP distribution metadata not found."}

    root = source_root()
    if root is None:
        return {
            "status": "unknown", "installed": inst,
            "message": (
                f"PCP {inst} installed from a wheel — cannot verify it is current. "
                f"Set PCP_SOURCE_ROOT to your PCP checkout to enable this check."
            ),
        }

    src = source_version(root)
    if src is None:
        return {"status": "unknown", "installed": inst,
                "message": f"Could not read a version from {root / 'pyproject.toml'}."}

    if inst == src:
        return {"status": "ok", "installed": inst, "source": src, "source_root": str(root),
                "message": f"PCP {inst} matches its source tree."}

    if _as_tuple(inst) < _as_tuple(src):
        if is_editable():
            return {
                "status": "stale_metadata", "installed": inst, "source": src,
                "source_root": str(root), "editable": True,
                "message": (
                    f"PCP metadata says {inst}, source tree at {root} is {src}. This is an "
                    f"EDITABLE install, so the CODE is already {src} — only the recorded "
                    f"version string is stale. Refresh with `pip install -e {root}` if you "
                    f"want `pcp --version` to be honest."
                ),
            }
        return {
            "status": "behind", "installed": inst, "source": src,
            "source_root": str(root), "editable": False,
            "message": (
                f"PCP {inst} is installed as a WHEEL while its source tree at {root} is "
                f"{src}. This install is genuinely running old code and is missing every "
                f"fix made since {inst}."
            ),
        }
    return {
        "status": "ahead", "installed": inst, "source": src, "source_root": str(root),
        "message": f"PCP {inst} is NEWER than the source tree at {root} ({src}).",
    }
