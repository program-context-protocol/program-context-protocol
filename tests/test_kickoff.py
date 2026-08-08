

# ── Screen/shared-entity DECOMPOSE-FIRST instructions actually shipped ──
# (2026-08-08) -- prompt-only additions have no unit-testable behavior of
# their own; this just locks in that the instruction text survives an edit.

def test_kickoff_prompt_instructs_screen_field():
    from pcp.commands.kickoff import SYSTEM_PROMPT
    assert "`screen`" in SYSTEM_PROMPT
    assert "shared_entities_enumerated" in SYSTEM_PROMPT
    assert "owns_entities" in SYSTEM_PROMPT


def test_pm_prompt_instructs_screen_field():
    from pcp.commands.pm import SYSTEM_PROMPT
    assert "`screen`" in SYSTEM_PROMPT
    assert "shared_entities_enumerated" in SYSTEM_PROMPT
    assert "owns_entities" in SYSTEM_PROMPT


# ── Orphaned modules from a prior kickoff (2026-07-27 Project S dogfood) ──

def test_orphaned_modules_from_a_prior_kickoff_are_reported(tmp_path, capsys):
    """`--force` only suppresses the overwrite confirm; it never removed prior
    module directories, so a re-scoped kickoff wrote its modules ALONGSIDE the
    old ones. `pcp build` reads modules from DISK, not decomposition.md, so the
    orphans would be built even though the new objective rules them out."""
    from pcp.commands.kickoff import _report_orphaned_modules

    modules = tmp_path / "strategy" / "modules"
    for name in ("sender-auth", "notifications", "field-placement-editor"):
        d = modules / name
        d.mkdir(parents=True)
        (d / "spec.yaml").write_text("module: x\n")

    orphans = _report_orphaned_modules(tmp_path, {"field-placement-editor"})

    assert orphans == ["notifications", "sender-auth"]
    out = capsys.readouterr().out
    assert "notifications" in out and "sender-auth" in out
    assert "field-placement-editor" not in out.split("NOT in this decomposition")[-1].split("rm -rf")[0]


def test_no_orphan_noise_on_a_clean_kickoff(tmp_path, capsys):
    from pcp.commands.kickoff import _report_orphaned_modules

    modules = tmp_path / "strategy" / "modules"
    for name in ("a", "b"):
        d = modules / name
        d.mkdir(parents=True)
        (d / "spec.yaml").write_text("module: x\n")

    assert _report_orphaned_modules(tmp_path, {"a", "b"}) == []
    assert capsys.readouterr().out == ""


def test_orphan_check_ignores_stray_dirs_without_a_spec(tmp_path):
    """A directory with no spec.yaml is not a module -- don't tell the user to
    delete something that was never scaffolded as one."""
    from pcp.commands.kickoff import _report_orphaned_modules

    modules = tmp_path / "strategy" / "modules"
    (modules / "real").mkdir(parents=True)
    (modules / "real" / "spec.yaml").write_text("module: x\n")
    (modules / "__pycache__").mkdir(parents=True)

    assert _report_orphaned_modules(tmp_path, {"real"}) == []


def test_orphan_check_noop_without_a_modules_dir(tmp_path):
    from pcp.commands.kickoff import _report_orphaned_modules
    assert _report_orphaned_modules(tmp_path, {"anything"}) == []
