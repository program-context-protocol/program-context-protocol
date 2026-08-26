import json

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.kb_topics import (
    build_kb_topics,
    write_kb_topics,
    _extract_scope_headings,
    _parse_pyproject_dependencies,
    _parse_requirements_txt,
    _parse_package_json,
    _collect_build_vs_buy_candidates,
    _collect_logic_tier_flags,
)


def _write_module(pcp_dir, name, spec, acceptance):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    (mod_dir / "spec.yaml").write_text(yaml.dump(spec))
    (mod_dir / "acceptance.yaml").write_text(yaml.dump(acceptance))


# --- pure helper units --------------------------------------------------

def test_extract_scope_headings_reads_markdown_headings(tmp_path):
    md = tmp_path / "objective.md"
    md.write_text("# Program Objective\n\nSome text.\n\n## Why This Exists\n\nMore.\n### Sub heading\n")
    assert _extract_scope_headings(md) == [
        "Program Objective", "Why This Exists", "Sub heading",
    ]


def test_extract_scope_headings_missing_file_returns_empty(tmp_path):
    assert _extract_scope_headings(tmp_path / "nope.md") == []


def test_parse_pyproject_dependencies_main_and_optional(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\n"
        "name = \"x\"\n"
        "dependencies = [\n"
        "    \"click>=8.1\",\n"
        "    \"pyyaml>=6.0\",\n"
        "]\n"
        "\n"
        "[project.optional-dependencies]\n"
        "graph = [\"graphifyy>=0.8\"]\n"
        "visual = [\"playwright>=1.40\"]\n"
        "\n"
        "[project.urls]\n"
        "Homepage = \"https://example.com\"\n"
    )
    result = _parse_pyproject_dependencies(pyproject)
    names = {n for n, _ in result}
    assert names == {"click", "pyyaml", "graphifyy", "playwright"}
    by_name = {n: g for n, g in result}
    assert by_name["click"] == "dependencies"
    assert by_name["graphifyy"] == "graph"
    assert by_name["playwright"] == "visual"


def test_parse_pyproject_dependencies_missing_file(tmp_path):
    assert _parse_pyproject_dependencies(tmp_path / "nope.toml") == []


def test_parse_requirements_txt_strips_versions_and_comments(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("# comment\n\nrequests==2.31.0\n-e .\nflask>=2.0\n")
    result = _parse_requirements_txt(req)
    names = [n for n, _ in result]
    assert names == ["requests", "flask"]


def test_parse_package_json_dependencies_and_dev(tmp_path):
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({
        "dependencies": {"react": "^18.0.0"},
        "devDependencies": {"vitest": "^1.0.0"},
    }))
    result = _parse_package_json(pkg)
    names = {n for n, _ in result}
    assert names == {"react", "vitest"}


def test_parse_package_json_missing_file(tmp_path):
    assert _parse_package_json(tmp_path / "nope.json") == []


def test_collect_build_vs_buy_candidates_dedupes_across_modules(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(
        pcp_dir, "auth",
        spec={"module": "auth", "description": "Auth.",
              "build_vs_buy": {"decision": "reuse_whole", "rationale": "fits",
                                "candidates_considered": ["Auth0", "Clerk"]}},
        acceptance={"module": "auth", "criteria": [
            {"id": "A001", "description": "x", "check": "manual", "status": "pending",
             "logic_tier": 1, "build_vs_buy": {"decision": "build_fresh", "rationale": "r",
                                                "candidates_considered": ["Auth0"]}},
        ]},
    )
    _write_module(
        pcp_dir, "billing",
        spec={"module": "billing", "description": "Billing.",
              "build_vs_buy": {"decision": "not_applicable", "rationale": "n/a"}},
        acceptance={"module": "billing", "criteria": [
            {"id": "B001", "description": "y", "check": "manual", "status": "pending",
             "logic_tier": 1, "build_vs_buy": {"decision": "build_fresh", "rationale": "r",
                                                "candidates_considered": ["Stripe"]}},
        ]},
    )
    modules_dir = pcp_dir / "strategy" / "modules"
    result = _collect_build_vs_buy_candidates(modules_dir)
    by_name = {c["candidate"]: c for c in result}
    assert set(by_name) == {"Auth0", "Clerk", "Stripe"}
    # Auth0 referenced from both module-level and criterion-level in auth
    assert len(by_name["Auth0"]["sources"]) == 2
    assert by_name["Stripe"]["sources"] == ["billing:B001"]


def test_collect_logic_tier_flags_only_rung_2_and_3(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(
        pcp_dir, "sched",
        spec={"module": "sched", "description": "Scheduling.", "build_vs_buy": {"decision": "not_applicable", "rationale": "n/a"}},
        acceptance={"module": "sched", "criteria": [
            {"id": "A001", "description": "solver criterion", "check": "manual", "status": "pending",
             "logic_tier": 2, "build_vs_buy": {"decision": "reuse_whole", "rationale": "OR-Tools"}},
            {"id": "A002", "description": "ml criterion", "check": "manual", "status": "pending",
             "logic_tier": 3, "build_vs_buy": {"decision": "reuse_whole", "rationale": "sklearn"}},
            {"id": "A003", "description": "deterministic criterion", "check": "manual", "status": "pending",
             "logic_tier": 1, "build_vs_buy": {"decision": "build_fresh", "rationale": "trivial"}},
            {"id": "A004", "description": "llm criterion", "check": "manual", "status": "pending",
             "logic_tier": 6, "build_vs_buy": {"decision": "build_fresh", "rationale": "n/a"}},
        ]},
    )
    modules_dir = pcp_dir / "strategy" / "modules"
    flags = _collect_logic_tier_flags(modules_dir)
    ids = {(f["module"], f["id"], f["logic_tier"]) for f in flags}
    assert ids == {("sched", "A001", 2), ("sched", "A002", 3)}


# --- build_kb_topics (full aggregation) ----------------------------------

def test_build_kb_topics_empty_project(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    result = build_kb_topics(pcp_dir, project_root=tmp_path)
    assert result["objective_scope"] == []
    assert result["build_vs_buy_candidates"] == []
    assert result["dependencies"] == []
    assert result["logic_tier_flags"] == []
    assert result["topics"] == []
    assert result["counts"]["total"] == 0


def test_build_kb_topics_aggregates_all_four_sources(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Program Objective\n\n## Why This Exists\n\nBody.\n")
    (pcp_dir / "target_state.md").write_text("# Target State\n")
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")

    _write_module(
        pcp_dir, "kb",
        spec={"module": "kb", "description": "KB module.",
              "build_vs_buy": {"decision": "reuse_partial", "rationale": "r",
                                "candidates_considered": ["graphify"]}},
        acceptance={"module": "kb", "criteria": [
            {"id": "A001", "description": "solver bit", "check": "manual", "status": "pending",
             "logic_tier": 2, "build_vs_buy": {"decision": "reuse_whole", "rationale": "OR-Tools"}},
        ]},
    )

    result = build_kb_topics(pcp_dir, project_root=tmp_path)

    assert {"file": "objective.md", "heading": "Program Objective"} in result["objective_scope"]
    assert {"file": "objective.md", "heading": "Why This Exists"} in result["objective_scope"]
    assert {"file": "target_state.md", "heading": "Target State"} in result["objective_scope"]

    candidate_names = {c["candidate"] for c in result["build_vs_buy_candidates"]}
    assert candidate_names == {"graphify"}

    dep_names = {d["name"] for d in result["dependencies"]}
    assert "requests" in dep_names

    flag_ids = {(f["module"], f["id"]) for f in result["logic_tier_flags"]}
    assert ("kb", "A001") in flag_ids

    # unified topics list includes one entry per source category
    sources = {t["source"] for t in result["topics"]}
    assert sources == {
        "objective_scope", "build_vs_buy_candidate",
        "dependency_manifest", "logic_tier_rung",
    }
    assert result["counts"]["total"] == len(result["topics"])
    assert result["counts"]["objective_scope"] == 3
    assert result["counts"]["build_vs_buy_candidate"] == 1
    assert result["counts"]["logic_tier_rung"] == 1

    # topic ids are stable slugs, not free text
    ids = {t["id"] for t in result["topics"]}
    assert "candidate:graphify" in ids
    assert "logic_tier:kb/A001" in ids


def test_build_kb_topics_is_zero_llm_pure_function(tmp_path, monkeypatch):
    """No LLM client should ever be imported/called by this module."""
    import pcp.commands.kb_topics as mod
    assert "llm" not in mod.__file__  # sanity: not literally the llm package
    import inspect
    src = inspect.getsource(mod)
    assert "llm.client" not in src
    assert "call_json" not in src


# --- write_kb_topics ------------------------------------------------------

def test_write_kb_topics_writes_yaml_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Obj\n")

    out = write_kb_topics(pcp_dir, project_root=tmp_path)
    assert out == pcp_dir / "kb" / "topics.yaml"
    assert out.exists()

    data = yaml.safe_load(out.read_text())
    assert "generated_at" in data
    assert data["objective_scope"] == [{"file": "objective.md", "heading": "Obj"}]


# --- CLI --------------------------------------------------------------

def test_kb_topics_cli_writes_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["kb-topics", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert (pcp_dir / "kb" / "topics.yaml").exists()


def test_kb_topics_cli_json(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["kb-topics", "--path", str(tmp_path), "--json"])
    assert result.exit_code == 0
    assert '"topics"' in result.output


def test_kb_topics_cli_no_pcp_dir_exits(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["kb-topics", "--path", str(tmp_path)])
    assert result.exit_code == 2
