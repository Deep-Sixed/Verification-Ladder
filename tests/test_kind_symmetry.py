"""Executions and judgments, each unable to impersonate the other.

v2 guarded one direction: an attestation could not satisfy a required gate. The
converse was unguarded, and `--gate diff="true"` produced a passing judgment rung
at the CLI. A machine cannot hold an opinion about a diff and an agent cannot run
a container build, so neither may stand in for the other. Behavioural gates need
both halves, bound to the same bytes.
"""

import hashlib
import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
_spec = importlib.util.spec_from_file_location("ladder_verify_kinds", SCRIPT)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)

JUDGMENTS = ["task", "diff", "self-review"]
POLICY = {
    "required_gates": ["lint", "verify-login"],
    "judgment_rungs": JUDGMENTS,
    "gates": {
        "local": {"lint": {"command": "ruff check .", "target": ["repository"]}},
        "behavioral": {"verify-login": {"driver": "node drive.mjs", "target": ["repository"],
                                        "evidence_mode": "execution+attestation"}}},
}
TARGET = {"repository": {"head": "a" * 40, "index_state": "sha256:i", "worktree_state": "sha256:w"}}


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    return tmp_path


@pytest.fixture
def governing(repo):
    return verify.gate_definitions(repo, POLICY)


def test_an_execution_cannot_satisfy_a_judgment_rung(governing):
    """`--gate diff="true"` was a passing judgment rung at the CLI."""
    status, reason = verify.kind_status({"gate": "diff", "kind": verify.EXECUTION}, governing, JUDGMENTS)
    assert status == verify.INADMISSIBLE
    assert "not that anyone read it" in reason


def test_an_attestation_cannot_satisfy_an_executable_gate(governing):
    status, reason = verify.kind_status({"gate": "lint", "kind": verify.ATTESTATION}, governing, JUDGMENTS)
    assert status == verify.INADMISSIBLE
    assert "attested, never executed" in reason


def test_each_kind_satisfies_its_own_requirement(governing):
    assert verify.kind_status({"gate": "diff", "kind": verify.ATTESTATION}, governing, JUDGMENTS)[0] == (
        verify.ADMISSIBLE)
    assert verify.kind_status({"gate": "lint", "kind": verify.EXECUTION}, governing, JUDGMENTS)[0] == (
        verify.ADMISSIBLE)


def _artifact(repo, name, body):
    (repo / name).write_bytes(body)
    return {"kind": "recording", "path": name, "sha256": "sha256:" + hashlib.sha256(body).hexdigest()}


def _behavioural(repo, artifacts):
    row = {"gate": "verify-login", "kind": verify.EXECUTION, "authority": "local",
           "target_state": TARGET, "artifacts": artifacts}
    row["record_id"] = verify.record_id(row)
    return row


def _attestation(execution, artifacts, **over):
    return {"gate": "verify-login", "kind": verify.ATTESTATION, "target_state": TARGET,
            "execution_ref": execution["record_id"],
            "artifact_refs": [a["sha256"] for a in artifacts], **over}


def test_a_behavioural_execution_with_a_matching_attestation_is_admissible(repo, governing):
    artifacts = [_artifact(repo, "login.webm", b"frames")]
    execution = _behavioural(repo, artifacts)
    rows = [execution, _attestation(execution, artifacts)]
    assert verify.behavioural_status(execution, governing, rows, repo) == (verify.ADMISSIBLE, None)


def test_a_behavioural_execution_alone_establishes_nothing(repo, governing):
    artifacts = [_artifact(repo, "login.webm", b"frames")]
    execution = _behavioural(repo, artifacts)
    status, reason = verify.behavioural_status(execution, governing, [execution], repo)
    assert status == verify.INADMISSIBLE
    assert "nothing attests to what they show" in reason


def test_an_artifact_replaced_after_attestation_breaks_the_chain(repo, governing):
    """Only recomputing from disk catches this; comparing two recorded strings would not."""
    artifacts = [_artifact(repo, "login.webm", b"frames")]
    execution = _behavioural(repo, artifacts)
    rows = [execution, _attestation(execution, artifacts)]
    (repo / "login.webm").write_bytes(b"different frames")
    status, reason = verify.behavioural_status(execution, governing, rows, repo)
    assert status == verify.INADMISSIBLE
    assert "bytes have changed" in reason


def test_a_missing_artifact_breaks_the_chain(repo, governing):
    artifacts = [_artifact(repo, "login.webm", b"frames")]
    execution = _behavioural(repo, artifacts)
    rows = [execution, _attestation(execution, artifacts)]
    (repo / "login.webm").unlink()
    assert verify.behavioural_status(execution, governing, rows, repo)[0] == verify.INADMISSIBLE


def test_an_attestation_for_another_execution_does_not_count(repo, governing):
    artifacts = [_artifact(repo, "login.webm", b"frames")]
    execution = _behavioural(repo, artifacts)
    stale = _attestation(execution, artifacts, execution_ref="sha256:" + "0" * 64)
    status, reason = verify.behavioural_status(execution, governing, [execution, stale], repo)
    assert status == verify.INADMISSIBLE
    assert "nothing attests" in reason


def test_an_attestation_judging_a_different_artifact_set_does_not_count(repo, governing):
    artifacts = [_artifact(repo, "login.webm", b"frames"), _artifact(repo, "dom.json", b"{}")]
    execution = _behavioural(repo, artifacts)
    partial = _attestation(execution, artifacts[:1])
    status, reason = verify.behavioural_status(execution, governing, [execution, partial], repo)
    assert status == verify.INADMISSIBLE
    assert "different set of artifacts" in reason


def test_an_attestation_made_against_another_target_does_not_count(repo, governing):
    artifacts = [_artifact(repo, "login.webm", b"frames")]
    execution = _behavioural(repo, artifacts)
    elsewhere = _attestation(execution, artifacts,
                             target_state={"repository": {"head": "b" * 40}})
    status, reason = verify.behavioural_status(execution, governing, [execution, elsewhere], repo)
    assert status == verify.INADMISSIBLE
    assert "different target" in reason


def test_a_tampered_record_id_is_refused(repo, governing):
    artifacts = [_artifact(repo, "login.webm", b"frames")]
    execution = _behavioural(repo, artifacts)
    rows = [execution, _attestation(execution, artifacts)]
    execution["record_id"] = "sha256:" + "0" * 64
    status, reason = verify.behavioural_status(execution, governing, rows, repo)
    assert status == verify.INADMISSIBLE
    assert "record_id" in reason


def test_a_record_id_ignores_its_own_stored_value(repo):
    artifacts = [_artifact(repo, "a.bin", b"x")]
    row = _behavioural(repo, artifacts)
    assert verify.record_id(row) == verify.record_id({k: v for k, v in row.items() if k != "record_id"})


def test_a_behavioural_gate_that_produced_no_artifacts_is_refused(repo, governing):
    execution = _behavioural(repo, [])
    status, reason = verify.behavioural_status(execution, governing, [execution], repo)
    assert status == verify.INADMISSIBLE
    assert "no artifacts" in reason


def test_an_ordinary_execution_gate_needs_no_attestation(repo, governing):
    row = {"gate": "lint", "kind": verify.EXECUTION, "target_state": TARGET}
    assert verify.behavioural_status(row, governing, [row], repo) == (verify.ADMISSIBLE, None)
