"""Mirror image of test_orphaned_work.py: complete with no verified_by.

Found live on ontology-foundry 2026-07-30 -- 12 criteria hand-flipped to
complete after the orphaned-work fix, because no sanctioned 'mark done' path
existed yet. More dangerous than a stale pending: a false complete means the
work is never checked again.

The baseline exists because the naive version was run against the real project
before shipping and returned 282 of 333 complete criteria (85%) -- verified_by
is 6 days old, so almost everything built before it predates the field by
construction. Un-baselined this is the exact CTRL-018 shape: a check with no
sanctioned way to comply, producing noise.
"""
import yaml

from pcp.orphaned_work import find_unverified_complete, format_unverified_findings


def _acc(tmp_path, criteria):
    root = tmp_path / "p"
    mod = root / ".pcp" / "strategy" / "modules" / "billing"
    mod.mkdir(parents=True)
    (mod / "acceptance.yaml").write_text(yaml.dump({
        "version": "2.0", "module": "billing", "criteria": criteria,
    }))
    return root / ".pcp"


def _unverified(cid="A001", status="complete"):
    return {"id": cid, "description": "d", "check": "manual", "status": status}


def test_first_call_ever_baselines_everything_and_reports_nothing(tmp_path):
    """No new problem exists yet -- reporting the baseline itself would be the
    same false accusation the naive version made against 282 real criteria."""
    pcp_dir = _acc(tmp_path, [_unverified("A001"), _unverified("A002")])
    assert find_unverified_complete(pcp_dir) == []
    assert (pcp_dir / "unverified_complete_baseline.yaml").exists()


def test_baselined_criteria_stay_silent_on_every_later_call(tmp_path):
    pcp_dir = _acc(tmp_path, [_unverified("A001")])
    find_unverified_complete(pcp_dir)          # writes the baseline
    assert find_unverified_complete(pcp_dir) == []
    assert find_unverified_complete(pcp_dir) == []   # not a one-shot fluke


def test_a_criterion_added_after_baselining_is_reported(tmp_path):
    """This is the actual signal: NEW hand-editing, not inherited debt."""
    pcp_dir = _acc(tmp_path, [_unverified("A001")])
    find_unverified_complete(pcp_dir)          # baseline = {A001}

    acc = pcp_dir / "strategy" / "modules" / "billing" / "acceptance.yaml"
    d = yaml.safe_load(acc.read_text())
    d["criteria"].append(_unverified("A002"))
    acc.write_text(yaml.dump(d))

    found = find_unverified_complete(pcp_dir)
    assert [f["criterion_id"] for f in found] == ["A002"]


def test_complete_with_verified_by_is_never_flagged_baselined_or_not(tmp_path):
    pcp_dir = _acc(tmp_path, [{"id": "A001", "description": "d", "check": "manual",
                              "status": "complete", "verified_by": "pcp_build"}])
    find_unverified_complete(pcp_dir)
    assert find_unverified_complete(pcp_dir) == []


def test_pending_criteria_are_never_flagged_here(tmp_path):
    """That is find_orphaned_work's job, not this one's."""
    pcp_dir = _acc(tmp_path, [_unverified("A001", status="pending")])
    find_unverified_complete(pcp_dir)
    assert find_unverified_complete(pcp_dir) == []


def test_baseline_grandfather_is_permanent_by_identity_not_status(tmp_path):
    """Deliberate: cycling pending -> complete must NOT resurrect a baselined
    criterion. acceptance.yaml is already gated behind human approval, so an
    innocent status edit resurrecting old debt as a 'new violation' would be a
    false alarm, not a real signal. `pcp verify` is the only way out."""
    pcp_dir = _acc(tmp_path, [_unverified("A001")])
    find_unverified_complete(pcp_dir)          # baseline = {A001}

    acc = pcp_dir / "strategy" / "modules" / "billing" / "acceptance.yaml"
    d = yaml.safe_load(acc.read_text())
    d["criteria"][0]["status"] = "pending"
    acc.write_text(yaml.dump(d))
    assert find_unverified_complete(pcp_dir) == []

    d["criteria"][0]["status"] = "complete"    # back to complete, still unverified
    acc.write_text(yaml.dump(d))
    assert find_unverified_complete(pcp_dir) == []   # still grandfathered, by design


def test_baseline_file_is_never_overwritten_once_it_exists(tmp_path):
    pcp_dir = _acc(tmp_path, [_unverified("A001")])
    find_unverified_complete(pcp_dir)
    baseline_path = pcp_dir / "unverified_complete_baseline.yaml"
    original = baseline_path.read_text()

    acc = pcp_dir / "strategy" / "modules" / "billing" / "acceptance.yaml"
    d = yaml.safe_load(acc.read_text())
    d["criteria"].append(_unverified("A999"))
    acc.write_text(yaml.dump(d))
    find_unverified_complete(pcp_dir)
    assert baseline_path.read_text() == original


def test_format_points_at_pcp_verify_with_the_arguments_filled_in():
    lines = format_unverified_findings([{"module": "billing", "criterion_id": "A001",
                                         "description": "d"}])
    body = "\n".join(lines)
    assert "pcp verify billing A001" in body


def test_format_is_empty_when_nothing_found():
    assert format_unverified_findings([]) == []
