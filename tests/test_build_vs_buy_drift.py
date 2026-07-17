import yaml

from pcp.commands.build import _run_wave_build_vs_buy_drift_check, _external_python_imports
from pcp import telemetry


def _write_module(pcp_dir, project_root, name, criteria):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    (mod_dir / "spec.yaml").write_text(yaml.dump({"dependencies": []}))
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({"criteria": criteria}))
    return mod_dir


def _qa_records(pcp_dir):
    return [r for r in telemetry.load(pcp_dir) if r.get("cycle") == "qa"]


def _bvb(decision):
    return {"decision": decision, "rationale": "r", "candidates_considered": []}


# ── _external_python_imports ──

def test_external_imports_excludes_stdlib_and_local_package(tmp_path):
    (tmp_path / "src" / "myapp").mkdir(parents=True)
    (tmp_path / "src" / "myapp" / "__init__.py").write_text("")
    target = tmp_path / "src" / "myapp" / "thing.py"
    target.write_text("import os\nimport json\nfrom myapp import util\nimport requests\n")
    externals = _external_python_imports(target, tmp_path)
    assert externals == {"requests"}


def test_external_imports_empty_for_pure_stdlib_file(tmp_path):
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "thing.py"
    target.write_text("import os\nimport sys\n")
    assert _external_python_imports(target, tmp_path) == set()


def test_external_imports_non_python_file_returns_empty(tmp_path):
    target = tmp_path / "thing.rs"
    target.write_text("use serde::Serialize;\n")
    assert _external_python_imports(target, tmp_path) == set()


# ── _run_wave_build_vs_buy_drift_check ──

def test_reuse_whole_with_no_external_import_flags_drift(tmp_path):
    project_root = tmp_path
    pcp_dir = project_root / ".pcp"
    pcp_dir.mkdir()
    (project_root / "src").mkdir()
    (project_root / "src" / "parser.py").write_text("import os\n\ndef f():\n    pass\n")
    _write_module(pcp_dir, project_root, "parsing", [
        {"id": "A001", "description": "x", "status": "complete",
         "target": "src/parser.py", "build_vs_buy": _bvb("reuse_whole")},
    ])

    findings = _run_wave_build_vs_buy_drift_check(pcp_dir, [{"name": "parsing"}], wave_number=0)

    assert len(findings) == 1
    assert "A001" in findings[0]
    assert "reuse_whole" in findings[0]
    record = [r for r in _qa_records(pcp_dir) if r["check"] == "wave-build-vs-buy-drift"][0]
    assert record["control_id"] == "CTRL-016"
    assert record["result"] == "block"


def test_reuse_whole_with_external_import_is_not_flagged(tmp_path):
    project_root = tmp_path
    pcp_dir = project_root / ".pcp"
    pcp_dir.mkdir()
    (project_root / "src").mkdir()
    (project_root / "src" / "parser.py").write_text("import lxml\n")
    _write_module(pcp_dir, project_root, "parsing", [
        {"id": "A001", "description": "x", "status": "complete",
         "target": "src/parser.py", "build_vs_buy": _bvb("reuse_whole")},
    ])

    findings = _run_wave_build_vs_buy_drift_check(pcp_dir, [{"name": "parsing"}], wave_number=0)
    assert findings == []


def test_fork_adapt_with_no_external_import_flags_drift(tmp_path):
    project_root = tmp_path
    pcp_dir = project_root / ".pcp"
    pcp_dir.mkdir()
    (project_root / "src").mkdir()
    (project_root / "src" / "engine.py").write_text("import os\n")
    _write_module(pcp_dir, project_root, "engine_mod", [
        {"id": "A001", "description": "x", "status": "complete",
         "target": "src/engine.py", "build_vs_buy": _bvb("fork_adapt")},
    ])
    findings = _run_wave_build_vs_buy_drift_check(pcp_dir, [{"name": "engine_mod"}], wave_number=0)
    assert len(findings) == 1
    assert "fork_adapt" in findings[0]


def test_build_fresh_never_flagged_regardless_of_imports(tmp_path):
    """build_fresh deliberately gets no reverse check -- package/import name
    mismatches (pyyaml/yaml) would make it too noisy to trust."""
    project_root = tmp_path
    pcp_dir = project_root / ".pcp"
    pcp_dir.mkdir()
    (project_root / "src").mkdir()
    (project_root / "src" / "thing.py").write_text("import somebrandnewlib\n")
    _write_module(pcp_dir, project_root, "widgets", [
        {"id": "A001", "description": "x", "status": "complete",
         "target": "src/thing.py", "build_vs_buy": _bvb("build_fresh")},
    ])
    findings = _run_wave_build_vs_buy_drift_check(pcp_dir, [{"name": "widgets"}], wave_number=0)
    assert findings == []


def test_reuse_partial_and_reimplement_never_checked(tmp_path):
    project_root = tmp_path
    pcp_dir = project_root / ".pcp"
    pcp_dir.mkdir()
    (project_root / "src").mkdir()
    (project_root / "src" / "thing.py").write_text("import os\n")
    _write_module(pcp_dir, project_root, "widgets", [
        {"id": "A001", "description": "x", "status": "complete",
         "target": "src/thing.py", "build_vs_buy": _bvb("reuse_partial")},
        {"id": "A002", "description": "y", "status": "complete",
         "target": "src/thing.py", "build_vs_buy": _bvb("reimplement_from_reference")},
    ])
    findings = _run_wave_build_vs_buy_drift_check(pcp_dir, [{"name": "widgets"}], wave_number=0)
    assert findings == []


def test_pending_criterion_not_checked(tmp_path):
    project_root = tmp_path
    pcp_dir = project_root / ".pcp"
    pcp_dir.mkdir()
    (project_root / "src").mkdir()
    (project_root / "src" / "thing.py").write_text("import os\n")
    _write_module(pcp_dir, project_root, "widgets", [
        {"id": "A001", "description": "x", "status": "pending",
         "target": "src/thing.py", "build_vs_buy": _bvb("reuse_whole")},
    ])
    findings = _run_wave_build_vs_buy_drift_check(pcp_dir, [{"name": "widgets"}], wave_number=0)
    assert findings == []
