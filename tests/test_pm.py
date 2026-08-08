import yaml


# ── spec_changes preservation (Project W dogfood, 2026-08-08) ──
# `pcp pm` used for a narrow, acceptance-only intent still emitted a
# spec_changes object that omitted category_reference/build_vs_buy -- since
# _write_one_module writes spec_changes as the WHOLE new spec.yaml, an
# omitted field either vanished (category_reference had no preservation
# guard at all) or got silently overwritten by the LLM's own placeholder
# text mirrored from the prompt's schema example (build_vs_buy). Caught
# before commit; this locks in the fix for both.

def test_pm_prompt_instructs_omitting_spec_changes_when_unneeded():
    """Prompt-only fix has no unit-testable behavior of its own -- this just
    locks in that the instruction text (letting the LLM skip a full spec.yaml
    rewrite for an acceptance-only intent) survives an edit."""
    from pcp.commands.pm import SYSTEM_PROMPT
    assert "spec_changes` to null" in SYSTEM_PROMPT or "spec_changes\" to null" in SYSTEM_PROMPT


def test_category_reference_preserved_when_llm_omits_it(tmp_path):
    from pcp.commands.pm import _write_one_module

    mod_dir = tmp_path / "strategy" / "modules" / "billing"
    mod_dir.mkdir(parents=True)
    real_category_reference = {
        "category": "payments",
        "source_evidence": ["Stripe Connect docs, section 3"],
        "classification": "adopted",
        "rationale": "Matches researched category reference exactly.",
    }
    (mod_dir / "spec.yaml").write_text(yaml.dump({
        "version": "2.0",
        "module": "billing",
        "description": "Handles billing.",
        "category_reference": real_category_reference,
        "build_vs_buy": {"decision": "not_applicable", "rationale": "n/a", "candidates_considered": []},
    }))
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({"version": "2.0", "module": "billing", "criteria": []}))

    # Simulates the exact bug: LLM response for a narrow intent includes
    # spec_changes but omits category_reference (per the prompt's own
    # "omit rather than guess" instruction) and build_vs_buy (unchanged).
    mod_result = {
        "module_name": "billing",
        "spec_changes": {
            "version": "2.0",
            "module": "billing",
            "description": "Handles billing, now with refunds.",
        },
        "acceptance_changes": {"criteria": []},
    }

    _write_one_module(tmp_path, mod_result)

    written = yaml.safe_load((mod_dir / "spec.yaml").read_text())
    assert written["category_reference"] == real_category_reference
    assert written["build_vs_buy"]["rationale"] == "n/a"
    assert written["description"] == "Handles billing, now with refunds."


def test_spec_yaml_untouched_when_spec_changes_is_none(tmp_path):
    from pcp.commands.pm import _write_one_module

    mod_dir = tmp_path / "strategy" / "modules" / "billing"
    mod_dir.mkdir(parents=True)
    original = {
        "version": "2.0",
        "module": "billing",
        "description": "Handles billing.",
        "category_reference": {"category": "payments", "source_evidence": [], "classification": "adopted", "rationale": "x"},
    }
    (mod_dir / "spec.yaml").write_text(yaml.dump(original))
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({"version": "2.0", "module": "billing", "criteria": []}))

    mod_result = {
        "module_name": "billing",
        "spec_changes": None,
        "acceptance_changes": {"criteria": [
            {"id": "A001", "description": "x", "check": "manual", "status": "complete",
             "logic_tier": 6, "build_vs_buy": {"decision": "build_fresh", "rationale": "x", "candidates_considered": []},
             "depends_on": []},
        ]},
    }

    _write_one_module(tmp_path, mod_result)

    assert yaml.safe_load((mod_dir / "spec.yaml").read_text()) == original


# ── criterion-ID race (Project W dogfood, 2026-08-08) ──
# Two concurrent `pcp pm` calls on the same module both computed the same
# "next available" ID from the same stale snapshot; the second write
# silently clobbered the first's new criterion. Simulated here by writing a
# criterion to disk BETWEEN when this call's "existing" state would have
# been read and when _write_one_module actually runs -- exactly what a
# concurrent process landing in between looks like from this call's view.

def test_id_collision_from_concurrent_pm_call_is_renumbered_not_clobbered(tmp_path):
    from pcp.commands.pm import _write_one_module, _next_free_criterion_id

    mod_dir = tmp_path / "strategy" / "modules" / "billing"
    mod_dir.mkdir(parents=True)
    (mod_dir / "spec.yaml").write_text(yaml.dump({"version": "2.0", "module": "billing", "description": "d"}))
    # A concurrent pm call already landed A002 with different content by the
    # time THIS call writes -- both calls' LLM picked "A002" independently.
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({
        "version": "2.0", "module": "billing",
        "criteria": [{"id": "A002", "description": "Refund flow (from the OTHER concurrent call)",
                      "check": "manual", "status": "pending", "logic_tier": 6,
                      "build_vs_buy": {"decision": "build_fresh", "rationale": "x", "candidates_considered": []},
                      "depends_on": []}],
    }))

    mod_result = {
        "module_name": "billing",
        "spec_changes": None,
        "acceptance_changes": {"criteria": [
            {"id": "A002", "description": "Discount codes (THIS call's own new criterion)",
             "check": "manual", "status": "pending", "logic_tier": 6,
             "build_vs_buy": {"decision": "build_fresh", "rationale": "y", "candidates_considered": []},
             "depends_on": []},
        ]},
    }

    warnings = _write_one_module(tmp_path, mod_result)

    written = yaml.safe_load((mod_dir / "acceptance.yaml").read_text())
    ids = {c["id"]: c["description"] for c in written["criteria"]}
    # Both criteria survive -- neither was clobbered.
    assert ids["A002"] == "Refund flow (from the OTHER concurrent call)"
    assert "Discount codes (THIS call's own new criterion)" in ids.values()
    assert len(written["criteria"]) == 2
    assert any("renumbered" in w for w in warnings)


def test_known_id_is_treated_as_a_real_edit_even_with_a_changed_description(tmp_path):
    """The collision guard's signal is known_ids (the snapshot taken before
    the LLM call), NOT a description diff -- an earlier version of this guard
    used description-match and broke the ordinary case of pm legitimately
    rewording an existing criterion while marking it complete. An ID present
    in known_ids must never be treated as a collision, however much its
    description changed."""
    from pcp.commands.pm import _write_one_module

    mod_dir = tmp_path / "strategy" / "modules" / "billing"
    mod_dir.mkdir(parents=True)
    (mod_dir / "spec.yaml").write_text(yaml.dump({"version": "2.0", "module": "billing", "description": "d"}))
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({
        "version": "2.0", "module": "billing",
        "criteria": [{"id": "A001", "description": "Refund flow works.", "check": "manual", "status": "pending",
                      "logic_tier": 6, "build_vs_buy": {"decision": "build_fresh", "rationale": "x", "candidates_considered": []},
                      "depends_on": [], "verified_by": "pcp_verify:test_passes"}],
    }))

    mod_result = {
        "module_name": "billing",
        "spec_changes": None,
        "acceptance_changes": {"criteria": [
            # Reworded, not just status-flipped -- this is exactly the shape
            # that broke under the old description-diff heuristic.
            {"id": "A001", "description": "Refund flow validates the order ID and reverses the charge.",
             "check": "manual", "status": "complete",
             "logic_tier": 6, "build_vs_buy": {"decision": "build_fresh", "rationale": "x", "candidates_considered": []},
             "depends_on": []},
        ]},
    }

    warnings = _write_one_module(tmp_path, mod_result, known_ids={"A001"})

    written = yaml.safe_load((mod_dir / "acceptance.yaml").read_text())
    assert len(written["criteria"]) == 1
    assert written["criteria"][0]["status"] == "complete"
    assert written["criteria"][0]["description"] == "Refund flow validates the order ID and reverses the charge."
    assert written["criteria"][0]["verified_by"] == "pcp_verify:test_passes"  # preserved, not clobbered
    assert not any("renumbered" in w for w in warnings)


def test_known_criterion_ids_snapshots_before_the_llm_call(tmp_path):
    from pcp.commands.pm import _known_criterion_ids

    pcp_dir = tmp_path / ".pcp"
    mod_dir = pcp_dir / "strategy" / "modules" / "billing"
    mod_dir.mkdir(parents=True)
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({
        "version": "2.0", "module": "billing",
        "criteria": [{"id": "A001", "description": "x"}, {"id": "A002", "description": "y"}],
    }))

    assert _known_criterion_ids(pcp_dir) == {"billing": {"A001", "A002"}}


def test_next_free_criterion_id_skips_taken_ids():
    from pcp.commands.pm import _next_free_criterion_id
    assert _next_free_criterion_id({"A001", "A002"}, "A002") == "A003"
    assert _next_free_criterion_id({"A001", "A003"}, "A002") == "A004"
    assert _next_free_criterion_id(set(), "A001") == "A001"
