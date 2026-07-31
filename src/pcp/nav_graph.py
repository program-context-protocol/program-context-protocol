"""Measured navigation reachability — is a screen actually reachable, and how deep.

The Feature Exposure Ladder previously classified a criterion by whether it
carried a `design_justification` field. Measured 2026-07-27 on Project O,
that produced 101 "Built, Hidden" and 24 "Exposed, Enriched" with **zero** at
rungs 2 and 3 -- a binary condition wearing a four-rung costume. "101 hidden
features" was really "101 criteria lack an optional field": a statement about
PCP's own paperwork presented as a statement about the product, and the exact
Goodhart shape `coverage_audit.py` exists to guard against elsewhere.

This module measures the artifact instead. It builds a page/route graph from the
UI source actually on disk and computes shortest-path depth from the app's entry
point. Deterministic, rung 1, no LLM.

Two navigation styles are recognised, both by reading what is there:

* multi-page (Vite `rollupOptions.input`, or bare `*.html`) with `href` edges --
  Project O's canvas-next is this: `index.html` and `query.html` linking
  to each other.
* single-page router (`<Route path=...>`) with `<Link to=...>` / `navigate(...)`
  edges.

The honest part is the fallback. When the graph cannot be determined -- no UI
source, no pages found, or a criterion that cannot be tied to a screen -- the
answer is `None`, meaning "not determinable", NOT "hidden". Reporting an absent
measurement as a bad measurement is how the previous version produced a
discoverability crisis out of an unpopulated field.
"""

from __future__ import annotations

import json
import re
from collections import deque
from pathlib import Path

_SKIP_DIRS = {"node_modules", ".venv", "venv", "dist", "build", ".git", "__pycache__"}

# Multi-page: Vite's own registry of entry documents.
_VITE_INPUT_BLOCK = re.compile(r"input\s*:\s*\{(.*?)\}", re.DOTALL)
_VITE_INPUT_ENTRY = re.compile(r"""["']?(\w+)["']?\s*:\s*resolve\([^,]+,\s*["']([^"']+)["']\)""")

# Edges.
_HREF = re.compile(r"""href\s*=\s*["']([^"'#?]+)""")
_LINK_TO = re.compile(r"""<Link[^>]*\bto\s*=\s*["']([^"']+)["']""")
_NAVIGATE = re.compile(r"""navigate\(\s*["']([^"']+)["']""")
_LOCATION = re.compile(r"""location\.(?:href|assign|replace)\s*=?\s*\(?\s*["']([^"']+)["']""")

# Single-page routes.
_ROUTE = re.compile(r"""<Route[^>]*\bpath\s*=\s*["']([^"']+)["']""")


def _source_files(root: Path) -> list[Path]:
    out: list[Path] = []
    if not root.is_dir():
        return out
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in {".tsx", ".ts", ".jsx", ".js", ".html", ".vue", ".svelte"}:
            continue
        if _SKIP_DIRS & set(p.parts):
            continue
        out.append(p)
    return out


def _ui_roots(project_root: Path) -> list[Path]:
    """Directories that look like a front end, without hardcoding a layout."""
    roots: list[Path] = []
    for cand in project_root.rglob("vite.config.*"):
        if not (_SKIP_DIRS & set(cand.parts)):
            roots.append(cand.parent)
    if roots:
        return roots
    for name in ("web", "frontend", "ui", "client"):
        d = project_root / name
        if d.is_dir():
            roots.append(d)
    return roots


def discover_pages(ui_root: Path) -> tuple[list[str], str | None]:
    """(page ids, entry page) for one UI root. Entry is None if undecidable."""
    pages: list[str] = []
    entry: str | None = None

    for cfg in ui_root.glob("vite.config.*"):
        block = _VITE_INPUT_BLOCK.search(cfg.read_text(errors="ignore"))
        if not block:
            continue
        for name, path in _VITE_INPUT_ENTRY.findall(block.group(1)):
            page = Path(path).name
            pages.append(page)
            # Vite convention: `main` (or the only entry) is the app's front door.
            if name in {"main", "index"} or entry is None:
                entry = page

    if not pages:
        for html in ui_root.glob("*.html"):
            pages.append(html.name)
        if "index.html" in pages:
            entry = "index.html"
        elif len(pages) == 1:
            entry = pages[0]

    if not pages:
        routes: set[str] = set()
        for f in _source_files(ui_root):
            routes.update(_ROUTE.findall(f.read_text(errors="ignore")))
        if routes:
            pages = sorted(routes)
            entry = "/" if "/" in routes else None

    return sorted(set(pages)), entry


def _normalise(target: str, pages: list[str]) -> str | None:
    """Map a raw href/route to a known page id, or None if it leaves the app."""
    t = target.strip()
    if not t or t.startswith(("http://", "https://", "mailto:", "tel:", "javascript:")):
        return None
    base = Path(t).name or t
    if base in pages:
        return base
    if t in pages:
        return t
    if not t.startswith("/"):
        cand = "/" + t.lstrip("./")
        if cand in pages:
            return cand
    return None


def build_link_graph(ui_root: Path, pages: list[str]) -> dict[str, set[str]]:
    """page -> pages it links to, from hrefs / <Link to> / navigate() / location.

    A file's edges are attributed to every page whose name it matches, and to
    every page when the file is shared (a component imported by several pages).
    Erring toward MORE edges makes the depth estimate optimistic, so an
    "unreachable" verdict is conservative -- the direction worth erring in when
    the output is "this feature is hidden".
    """
    graph: dict[str, set[str]] = {p: set() for p in pages}
    for f in _source_files(ui_root):
        text = f.read_text(errors="ignore")
        targets = set()
        for pat in (_HREF, _LINK_TO, _NAVIGATE, _LOCATION):
            targets.update(pat.findall(text))
        resolved = {r for t in targets if (r := _normalise(t, pages))}
        if not resolved:
            continue
        owner = f.name if f.name in pages else None
        for src in ([owner] if owner else pages):
            graph.setdefault(src, set()).update(resolved - {src})
    return graph


def reachability(graph: dict[str, set[str]], entry: str) -> dict[str, int]:
    """Shortest-path depth from `entry`. Absent key == not reachable."""
    depths = {entry: 0}
    q = deque([entry])
    while q:
        cur = q.popleft()
        for nxt in graph.get(cur, ()):  # noqa: SIM118
            if nxt not in depths:
                depths[nxt] = depths[cur] + 1
                q.append(nxt)
    return depths


def analyse(project_root: Path) -> dict:
    """{"available": bool, "pages": [...], "entry": str, "depths": {page: int}}.

    `available: False` means the front end could not be located or its entry
    could not be determined -- callers MUST treat that as "not measured", never
    as "nothing is reachable".
    """
    # A project can hold several front ends (Project O has `web/` and
    # `web/canvas-next/`, both with a vite config). First-match-wins picked the
    # thinner one and reported a single unreachable-from-nothing page, so pick
    # the most substantive graph and record that others exist rather than
    # silently analysing a stub.
    candidates = []
    for ui_root in _ui_roots(project_root):
        pages, entry = discover_pages(ui_root)
        if not pages or not entry:
            continue
        graph = build_link_graph(ui_root, pages)
        depths = reachability(graph, entry)
        candidates.append({
            "available": True,
            "ui_root": str(ui_root.relative_to(project_root)) if ui_root != project_root else ".",
            "pages": pages,
            "entry": entry,
            "depths": depths,
            "unreachable": sorted(set(pages) - set(depths)),
        })
    if not candidates:
        return {"available": False, "reason": "no front end with a determinable entry page found"}
    candidates.sort(key=lambda c: (len(c["pages"]), len(c["depths"])), reverse=True)
    best = candidates[0]
    others = [c["ui_root"] for c in candidates[1:]]
    if others:
        best["other_ui_roots"] = others
    return best


def screen_for_target(target: str, analysis: dict) -> str | None:
    """Which page a criterion's declared `target` file belongs to, if knowable.

    A target that IS a page resolves directly. A component file resolves only
    when its name matches a page stem; otherwise None -- import-graph tracing is
    deliberately not attempted here, because guessing which screen a shared
    component belongs to would reintroduce exactly the fabricated precision this
    module exists to remove.
    """
    if not target or not analysis.get("available"):
        return None
    pages = analysis.get("pages", [])
    direct = _normalise(target, pages)
    if direct:
        return direct
    stem = Path(target).stem.lower()
    for page in pages:
        if Path(page).stem.lower() == stem:
            return page
    return None
