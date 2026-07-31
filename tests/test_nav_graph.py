"""Discoverability measured from the built UI, not from a declared field.

The ladder used to classify by whether `design_justification` existed. On
Project O that gave 101 "Built, Hidden" and 24 "Exposed, Enriched" with
ZERO at rungs 2 and 3 — a binary condition wearing a four-rung costume. "101
hidden features" was really "101 criteria lack an optional field".
"""

from pathlib import Path

from pcp import nav_graph as ng


def _vite_project(tmp_path, entries: dict, links: dict):
    (tmp_path / "vite.config.ts").write_text(
        "export default {build:{rollupOptions:{input:{"
        + ",".join(f'{k}: resolve(__dirname, "{v}")' for k, v in entries.items())
        + "}}}}"
    )
    for page, targets in links.items():
        body = "".join(f'<a href="./{t}">go</a>' for t in targets)
        (tmp_path / page).write_text(f"<html><body>{body}</body></html>")
    for page in entries.values():
        if page not in links:
            (tmp_path / page).write_text("<html></html>")
    return tmp_path


def test_multipage_depths_are_measured(tmp_path):
    """Project O's real shape: vite multi-page, pages linked by href."""
    root = _vite_project(
        tmp_path,
        {"main": "index.html", "query": "query.html"},
        {"index.html": ["query.html"], "query.html": ["index.html"]},
    )
    r = ng.analyse(root)
    assert r["available"] is True
    assert r["entry"] == "index.html"
    assert r["depths"] == {"index.html": 0, "query.html": 1}
    assert r["unreachable"] == []


def test_a_page_with_no_inbound_link_is_measurably_unreachable(tmp_path):
    """This is the only honest way to say "Built, Hidden" — it is a fact about
    the artifact, not about a missing field."""
    root = _vite_project(
        tmp_path,
        {"main": "index.html", "secret": "secret.html"},
        {"index.html": []},
    )
    r = ng.analyse(root)
    assert r["depths"] == {"index.html": 0}
    assert r["unreachable"] == ["secret.html"]


def test_external_links_are_not_pages(tmp_path):
    root = tmp_path
    (root / "vite.config.ts").write_text(
        'export default {build:{rollupOptions:{input:{main: resolve(__dirname, "index.html")}}}}'
    )
    (root / "index.html").write_text('<a href="https://example.com">out</a>')
    r = ng.analyse(root)
    assert r["pages"] == ["index.html"]
    assert r["depths"] == {"index.html": 0}


def test_no_front_end_reports_unavailable_not_hidden(tmp_path):
    """`available: False` must never be read as "nothing is reachable"."""
    (tmp_path / "src").mkdir()
    r = ng.analyse(tmp_path)
    assert r["available"] is False
    assert "reason" in r


def test_richest_ui_root_wins(tmp_path):
    """A project can hold several front ends; first-match-wins picked a stub."""
    thin = tmp_path / "web"
    thin.mkdir()
    _vite_project(thin, {"main": "index.html"}, {"index.html": []})
    rich = tmp_path / "web" / "canvas-next"
    rich.mkdir(parents=True)
    _vite_project(rich, {"main": "index.html", "query": "query.html"},
                  {"index.html": ["query.html"]})
    r = ng.analyse(tmp_path)
    assert len(r["pages"]) == 2, "must analyse the substantive front end"
    assert r["other_ui_roots"], "and record that others exist"


def test_screen_for_target_refuses_to_guess(tmp_path):
    """A shared component cannot be attributed to one screen. Guessing would
    reintroduce exactly the fabricated precision this replaces."""
    root = _vite_project(tmp_path, {"main": "index.html"}, {"index.html": []})
    a = ng.analyse(root)
    assert ng.screen_for_target("index.html", a) == "index.html"
    assert ng.screen_for_target("src/components/Button.tsx", a) is None
    assert ng.screen_for_target("", a) is None


# ── the ladder itself ──

def _crit(cid, target=None, dj=None):
    c = {"id": cid, "description": "renders a dashboard view"}
    if target:
        c["target"] = target
    if dj is not None:
        c["design_justification"] = dj
    return c


def test_unmeasurable_criterion_is_not_rung_1(tmp_path):
    """The whole defect: absent measurement reported as bad measurement."""
    from pcp.commands.design_audit import _classify_rung
    root = _vite_project(tmp_path, {"main": "index.html"}, {"index.html": []})
    nav = ng.analyse(root)
    assert _classify_rung(_crit("A1"), nav) is None, "no target -> not measurable"
    assert _classify_rung(_crit("A2"), None) is None, "no nav analysis -> not measurable"


def test_unreachable_screen_is_rung_1(tmp_path):
    from pcp.commands.design_audit import _classify_rung
    root = _vite_project(tmp_path, {"main": "index.html", "s": "secret.html"},
                         {"index.html": []})
    nav = ng.analyse(root)
    assert _classify_rung(_crit("A1", target="secret.html"), nav) == 1


def test_buried_screen_is_rung_2(tmp_path):
    from pcp.commands.design_audit import _classify_rung
    root = _vite_project(
        tmp_path,
        {"main": "index.html", "a": "a.html", "b": "b.html", "c": "c.html"},
        {"index.html": ["a.html"], "a.html": ["b.html"], "b.html": ["c.html"]},
    )
    nav = ng.analyse(root)
    assert nav["depths"]["c.html"] == 3
    assert _classify_rung(_crit("A1", target="c.html"), nav, depth_threshold=2) == 2


def test_reachable_screen_is_rung_3_and_jtbd_lifts_to_4(tmp_path):
    from pcp.commands.design_audit import _classify_rung
    root = _vite_project(tmp_path, {"main": "index.html", "q": "query.html"},
                         {"index.html": ["query.html"]})
    nav = ng.analyse(root)
    assert _classify_rung(_crit("A1", target="query.html"), nav) == 3
    good = {"checklist_passed": ["both-themes"],
            "jtbd_framing": "when a user needs to query, this lets them search"}
    assert _classify_rung(_crit("A2", target="query.html", dj=good), nav) == 4
    # A justification alone can no longer manufacture a rung on an unreachable
    # screen — the artifact decides rungs 1-3.
    assert _classify_rung(_crit("A3", dj=good), nav) is None
