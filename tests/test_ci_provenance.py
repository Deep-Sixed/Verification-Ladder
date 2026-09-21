"""CI evidence, bound along the whole chain rather than by the commit it names.

`head_sha` establishes that a run NAMED a commit. The CLI replay showed a job
payload from run 999999, carrying an all-zero head_sha, composing happily with
run 7001's metadata for the commit under verification. Every link is now checked:
repository, run, attempt, workflow, job, step, commit.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
_spec = importlib.util.spec_from_file_location("ladder_verify_ci", SCRIPT)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)

SHA = "c" * 40
EXPECTED = {"head_sha": SHA, "repository": "Deep-Sixed/Verification-Ladder",
            "workflow": ".github/workflows/ci.yml", "job": "validate", "events": ["push"]}


def _run(**over):
    return {"id": 7001, "head_sha": SHA, "event": "push", "run_attempt": 1,
            "path": ".github/workflows/ci.yml", "conclusion": "success",
            "repository": {"full_name": "Deep-Sixed/Verification-Ladder"}, **over}


def _job(**over):
    return {"id": 51, "run_id": 7001, "run_attempt": 1, "head_sha": SHA, "name": "validate",
            "steps": [{"name": "Run ruff check .", "conclusion": "success"}], **over}


def test_a_complete_chain_is_admissible():
    assert verify.ci_provenance_status(_run(), _job(), EXPECTED) == (verify.ADMISSIBLE, None)


def test_a_job_from_another_run_is_refused():
    """The exact forgery the CLI replay demonstrated."""
    status, reason = verify.ci_provenance_status(_run(), _job(run_id=999999, head_sha="0" * 40), EXPECTED)
    assert status == verify.INADMISSIBLE
    assert "job belongs to run 999999" in reason


def test_evidence_from_another_repository_is_refused():
    """A fork's run on the same commit is the plausible case, not the exotic one."""
    forked = _run(repository={"full_name": "someone-else/Verification-Ladder"})
    status, reason = verify.ci_provenance_status(forked, _job(), EXPECTED)
    assert status == verify.INADMISSIBLE
    assert "someone-else" in reason


def test_a_run_naming_no_repository_cannot_be_told_from_a_fork():
    status, reason = verify.ci_provenance_status(_run(repository={}), _job(), EXPECTED)
    assert status == verify.INADMISSIBLE
    assert "fork" in reason


def test_another_attempt_is_refused():
    status, reason = verify.ci_provenance_status(_run(run_attempt=1), _job(run_attempt=3), EXPECTED)
    assert status == verify.INADMISSIBLE
    assert "attempt 3" in reason


@pytest.mark.parametrize(("run_over", "job_over"), [({"run_attempt": None}, {}), ({}, {"run_attempt": None})])
def test_a_missing_attempt_identity_is_unverifiable(run_over, job_over):
    status, reason = verify.ci_provenance_status(_run(**run_over), _job(**job_over), EXPECTED)
    assert status == verify.INADMISSIBLE
    assert "run_attempt" in reason


def test_the_wrong_workflow_is_refused():
    status, reason = verify.ci_provenance_status(_run(path=".github/workflows/release.yml"), _job(), EXPECTED)
    assert status == verify.INADMISSIBLE
    assert "release.yml" in reason


def test_the_wrong_job_is_refused():
    status, reason = verify.ci_provenance_status(_run(), _job(name="publish"), EXPECTED)
    assert status == verify.INADMISSIBLE
    assert "'publish'" in reason


def test_another_commit_is_refused():
    status, reason = verify.ci_provenance_status(_run(head_sha="d" * 40), _job(), EXPECTED)
    assert status == verify.INADMISSIBLE
    assert "the commit under verification" in reason


def test_a_pull_request_run_is_refused_for_branch_tip_evidence():
    status, reason = verify.ci_provenance_status(_run(event="pull_request"), _job(), EXPECTED)
    assert status == verify.INADMISSIBLE
    assert "merge" in reason


def test_a_missing_run_id_is_unverifiable_not_waved_through():
    status, reason = verify.ci_provenance_status(_run(id=None), _job(), EXPECTED)
    assert status == verify.INADMISSIBLE
    assert "no run id" in reason


def test_a_run_without_head_sha_binds_to_nothing():
    status, reason = verify.ci_provenance_status(_run(head_sha=None), _job(), EXPECTED)
    assert status == verify.INADMISSIBLE
    assert "no head_sha" in reason


@pytest.mark.parametrize("conclusion", ["skipped", "cancelled", None])
def test_a_step_that_did_not_finish_establishes_nothing(conclusion):
    job = _job(steps=[{"name": "Run ruff check .", "conclusion": conclusion}])
    assert verify.ci_step_status(job, "Run ruff check .")[0] == "BLOCKED"


def test_an_absent_step_is_blocked_and_says_so():
    status, reason = verify.ci_step_status(_job(), "Run docker build .")
    assert status == "BLOCKED"
    assert "absent from this job" in reason


@pytest.mark.parametrize(("conclusion", "expected"), [("success", "PASS"), ("failure", "FAIL"),
                                                      ("timed_out", "FAIL")])
def test_a_finished_step_reads_as_its_conclusion(conclusion, expected):
    job = _job(steps=[{"name": "s", "conclusion": conclusion}])
    assert verify.ci_step_status(job, "s")[0] == expected
