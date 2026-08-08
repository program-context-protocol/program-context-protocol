"""pcp enrich -- agy research pass over an existing module's gaps, routed
into pm's existing gated write path. See commands/enrich.py's docstring."""

from unittest.mock import patch

from click.testing import CliRunner

from pcp.cli import cli


def _project(tmp_path, module="billing", spec_extra=None, criteria=None):
    pcp = tmp_path / ".pcp"
    mod_dir = pcp / "strategy" / "modules" / module
    mod_dir.mkdir(parents=True)
    (pcp / "objective.md").write_text("# Objective\n\nRun a billing platform.\n")
    spec = {"version": "2.0", "module": module, "description": "Handles invoicing."}
    if spec_extra:
        spec.update(spec_extra)
    import yaml
    (mod_dir / "spec.yaml").write_text(yaml.dump(spec))
    acc = {"version": "2.0", "module": module, "criteria": criteria or []}
    (mod_dir / "acceptance.yaml").write_text(yaml.dump(acc))
    return pcp


def test_enrich_requires_existing_module(tmp_path):
    pcp = tmp_path / ".pcp"
    (pcp / "strategy" / "modules").mkdir(parents=True)
    (pcp / "objective.md").write_text("# Objective\n")
    result = CliRunner().invoke(cli, ["enrich", "nonexistent", "--path", str(tmp_path)])
    assert result.exit_code == 2
    assert "no spec.yaml" in result.output.lower()


def test_enrich_no_missing_features_is_a_clean_noop(tmp_path):
    _project(tmp_path)
    research = {"researched_features": [], "summary": "billing module already covers the category"}
    with patch("pcp.llm.client.call_json", return_value=research) as mock_call:
        result = CliRunner().invoke(cli, ["enrich", "billing", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "No missing features" in result.output
    assert mock_call.call_args.kwargs.get("harness") == "agy"


def test_enrich_routes_accepted_features_into_pm(tmp_path):
    _project(tmp_path)
    research = {
        "researched_features": [
            {"feature": "dunning emails", "rationale": "standard for billing products", "source_evidence": "training-data recall, unverified"},
        ],
        "summary": "found one gap",
    }
    pm_result = {
        "capabilities_enumerated": ["dunning emails"],
        "overall_explanation": "adds dunning emails",
        "modules": [
            {
                "module_action": "modify",
                "module_name": "billing",
                "module_explanation": "adds dunning",
                "spec_changes": None,
                "acceptance_changes": {
                    "version": "2.0",
                    "module": "billing",
                    "criteria": [
                        {
                            "id": "A001",
                            "description": "Send dunning emails on failed payment",
                            "check": "manual",
                            "status": "pending",
                            "logic_tier": 6,
                            "build_vs_buy": {"decision": "build_fresh", "rationale": "x", "candidates_considered": []},
                            "depends_on": [],
                            "target": "src/billing/dunning.py",
                        }
                    ],
                },
            }
        ],
    }

    def fake_call_json(system, user, model=None, pcp_dir=None, command="", harness=None, **kw):
        if harness == "agy":
            return research
        return pm_result

    with patch("pcp.llm.client.call_json", side_effect=fake_call_json):
        result = CliRunner().invoke(
            cli, ["enrich", "billing", "--path", str(tmp_path)],
            input="y\ny\n",
        )
    assert result.exit_code == 0, result.output
    acc_text = (tmp_path / ".pcp" / "strategy" / "modules" / "billing" / "acceptance.yaml").read_text()
    assert "dunning" in acc_text.lower()


def test_enrich_declined_at_research_stage_writes_nothing(tmp_path):
    pcp = _project(tmp_path)
    research = {
        "researched_features": [
            {"feature": "dunning emails", "rationale": "x", "source_evidence": "y"},
        ],
        "summary": "found one gap",
    }
    with patch("pcp.llm.client.call_json", return_value=research):
        result = CliRunner().invoke(
            cli, ["enrich", "billing", "--path", str(tmp_path)],
            input="n\n",
        )
    assert result.exit_code == 0
    acc_text = (pcp / "strategy" / "modules" / "billing" / "acceptance.yaml").read_text()
    assert "dunning" not in acc_text.lower()


def test_enrich_agy_missing_binary_reports_clearly(tmp_path):
    _project(tmp_path)
    with patch("pcp.llm.client.call_json", side_effect=RuntimeError("agy CLI not found at 'agy'.")):
        result = CliRunner().invoke(cli, ["enrich", "billing", "--path", str(tmp_path)])
    assert result.exit_code == 2
    assert "agy" in result.output.lower()


def test_enrich_prompt_includes_category_reference(tmp_path):
    _project(tmp_path, spec_extra={
        "category_reference": {
            "category": "Billing Platform",
            "rationale": "handles recurring invoicing",
            "source_evidence": ["x"],
            "classification": "adopted",
        }
    })
    research = {"researched_features": [], "summary": "s"}
    with patch("pcp.llm.client.call_json", return_value=research) as mock_call:
        CliRunner().invoke(cli, ["enrich", "billing", "--path", str(tmp_path)])
    user_prompt = mock_call.call_args[0][1]
    assert "Billing Platform" in user_prompt
