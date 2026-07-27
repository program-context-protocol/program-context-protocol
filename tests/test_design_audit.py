import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.design_audit import build_design_audit, write_design_audit, _classify_rung


def _write_module(pcp_dir, name, criteria):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({"module": name, "criteria": criteria}))


# Rewritten 2026-07-27. These asserted the declaration-based contract: no
# `design_justification` -> rung 1 "Built, Hidden". Measured on ontology-foundry
# that produced 101 at rung 1, 24 at rung 4 and ZERO at rungs 2 and 3 — a binary
# condition wearing a four-rung costume, reporting missing FIELDS as hidden
# FEATURES. Rungs 1-3 now come from measured reachability in the built UI; only
# rung 4 still consults the declaration.

def _nav(tmp_path, entries, links):
    """A real vite multi-page front end on disk, matching ontology-foundry's shape."""
    from pcp import nav_graph
    (tmp_path / "vite.config.ts").write_text(
        "export default {build:{rollupOptions:{input:{"
        + ",".join(f'{k}: resolve(__dirname, "{v}")' for k, v in entries.items())
        + "}}}}"
    )
    for page in entries.values():
        hrefs = "".join(f'<a href="./{t}">go</a>' for t in links.get(page, []))
        (tmp_path / page).write_text(f"<html><body>{hrefs}</body></html>")
    return nav_graph.analyse(tmp_path)


def _ui(cid="A001", target=None, dj=None):
    c = {"id": cid, "description": "Dashboard renders coverage"}
    if target:
        c["target"] = target
    if dj is not None:
        c["design_justification"] = dj
    return c


def test_criterion_with_no_screen_is_not_measurable_not_rung_1(tmp_path):
    """The defect being fixed: an absent measurement reported as a bad one."""
    nav = _nav(tmp_path, {"main": "index.html"}, {})
    assert _classify_rung(_ui(), nav) is None


def test_rung_1_requires_a_measurably_unreachable_screen(tmp_path):
    nav = _nav(tmp_path, {"main": "index.html", "s": "secret.html"}, {})
    assert _classify_rung(_ui(target="secret.html"), nav) == 1


def test_rung_2_is_reachable_but_buried(tmp_path):
    nav = _nav(tmp_path, {"main": "index.html", "a": "a.html", "b": "b.html"},
               {"index.html": ["a.html"], "a.html": ["b.html"]})
    assert nav["depths"]["b.html"] == 2
    assert _classify_rung(_ui(target="b.html"), nav, depth_threshold=1) == 2


def test_rung_3_is_reachable_within_threshold(tmp_path):
    nav = _nav(tmp_path, {"main": "index.html", "q": "query.html"},
               {"index.html": ["query.html"]})
    assert _classify_rung(_ui(target="query.html"), nav) == 3


def test_rung_4_adds_real_jtbd_framing_on_a_reachable_screen(tmp_path):
    nav = _nav(tmp_path, {"main": "index.html", "q": "query.html"},
               {"index.html": ["query.html"]})
    dj = {"checklist_passed": ["both-themes"],
          "jtbd_framing": "when a PM worries coverage is slipping, this screen shows the real number"}
    assert _classify_rung(_ui(target="query.html", dj=dj), nav) == 4
    # A justification alone can no longer manufacture a rung — the artifact
    # decides 1-3, so an unattributable criterion stays unmeasured.
    assert _classify_rung(_ui(dj=dj), nav) is None


def test_build_design_audit_empty_project(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    data = build_design_audit(pcp_dir)
    assert data["modules"] == []
    assert data["total_ui_criteria"] == 0


def test_build_design_audit_skips_non_ui_criteria(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(pcp_dir, "backend", [
        {"id": "A001", "description": "API returns correct percentage", "check": "manual", "status": "pending"},
    ])
    data = build_design_audit(pcp_dir)
    assert data["modules"] == []


def test_build_design_audit_aggregates_ui_criteria_by_rung(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(pcp_dir, "admin", [
        {"id": "A001", "description": "Admin dashboard renders per-app state", "check": "manual", "status": "pending"},
        {"id": "A002", "description": "Settings form displays validation errors", "check": "manual", "status": "pending",
         "design_justification": {"checklist_passed": ["grounded-in-subject"],
                                   "jtbd_framing": "when a user submits invalid input, this shows exactly what to fix"}},
    ])
    data = build_design_audit(pcp_dir)
    assert data["total_ui_criteria"] == 2
    # No front end exists in this fixture, so neither criterion can be tied to a
    # screen. Both are "not measured" — and critically NOT rung 1.
    assert data["undetermined"] == 2
    assert data["rung_counts"][1] == 0
    assert data["nav_analysis"]["available"] is False
    mod = data["modules"][0]
    assert mod["module"] == "admin"
    assert all(c["rung"] is None for c in mod["criteria"])


def test_write_design_audit_renders_markdown(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(pcp_dir, "admin", [
        {"id": "A001", "description": "Dashboard renders coverage", "check": "manual", "status": "pending"},
    ])
    out = write_design_audit(pcp_dir)
    assert out.exists()
    content = out.read_text()
    assert "Feature Exposure Ladder" in content
    assert "Module: `admin`" in content
    assert "HEART" in content


def test_design_audit_cli_json(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["design-audit", "--path", str(tmp_path), "--json"])
    assert result.exit_code == 0
    assert '"rung_counts"' in result.output


def test_design_audit_cli_writes_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["design-audit", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert (pcp_dir / "design_audit.md").exists()


def test_design_audit_cli_no_pcp_dir_exits(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["design-audit", "--path", str(tmp_path)])
    assert result.exit_code == 2
