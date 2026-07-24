"""verified_by provenance (2026-07-24): the only place a criterion's status
flips to complete through the real gated loop stamps who/what did it, so
current_state.md can distinguish a pcp-build-audited completion from a
hand-edited one -- closes the "pcp build and a regular build say completed
the same way" gap."""

import yaml

from pcp.commands.build import _mark_criterion_complete
from pcp.schema.validator import load_yaml


def _mod(tmp_path, criteria):
    acc_path = tmp_path / "acceptance.yaml"
    acc_path.write_text(yaml.dump({"criteria": criteria}))
    return {"name": "m", "acc_path": acc_path}


def test_mark_complete_defaults_to_pcp_build(tmp_path):
    mod = _mod(tmp_path, [{"id": "A1", "status": "pending"}])
    _mark_criterion_complete(mod, "A1")
    acc = load_yaml(mod["acc_path"])
    crit = acc["criteria"][0]
    assert crit["status"] == "complete"
    assert crit["verified_by"] == "pcp_build"


def test_mark_complete_respects_explicit_verified_by(tmp_path):
    mod = _mod(tmp_path, [{"id": "A1", "status": "pending"}])
    _mark_criterion_complete(mod, "A1", verified_by="pcp_build_install_only")
    acc = load_yaml(mod["acc_path"])
    assert acc["criteria"][0]["verified_by"] == "pcp_build_install_only"


def test_mark_complete_only_touches_matching_id(tmp_path):
    mod = _mod(tmp_path, [
        {"id": "A1", "status": "pending"},
        {"id": "A2", "status": "pending"},
    ])
    _mark_criterion_complete(mod, "A1")
    acc = load_yaml(mod["acc_path"])
    by_id = {c["id"]: c for c in acc["criteria"]}
    assert by_id["A1"]["status"] == "complete"
    assert "verified_by" not in by_id["A2"]
