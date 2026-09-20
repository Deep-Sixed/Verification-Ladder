"""The forgeries, gathered in one place and mapped to the rule that refuses each.

Every case here either was demonstrated against the shipped CLI during review or
is named in `docs/design/evidence-v3.md` as an acceptance test. They live
together so one file answers "what can this not be fooled by", and so a rule
that stops refusing something fails here rather than quietly somewhere else.

Numbering follows the note's acceptance list. Cases still blocked on later work
are collected at the end, named and skipped rather than silently absent.
"""

import hashlib
import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
_spec = importlib.util.spec_from_file_location("ladder_verify_adversarial", SCRIPT)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)

SHA = "e" * 40
TARGET = {"repository": {"head": SHA, "index_state": "sha256:i", "worktree_state": "sha256:w"}}
POLICY = {
    "required_gates": ["lint", "container-build", "verify-login"],
    "judgment_rungs": ["diff", "self-review"],
    "gates": {
        "local": {"lint": {"command": "ruff check .", "target": ["repository"]}},
        "ci": {"container-build": {"step": "Run docker build .", "target": ["repository"]}},
        "behavioral": {"verify-login": {"driver": "node drive.mjs", "target": ["repository", "runtime"],
                                        "evidence_mode": "execution+attestation"}}},
}


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    return tmp_path


@pytest.fixture
def governing(repo):
    return verify.gate_definitions(repo, POLICY)


def _row(name, authority, governing, **over):
    return {"gate": name, "authority": authority, "kind": verify.EXECUTION,
            "definition_sha256": governing[name]["definition_sha256"], "target_state": TARGET, **over}


def _refused(result):
    status, reason = result
    assert status in (verify.INADMISSIBLE, "BLOCKED", "STALE"), f"admitted: {result}"
    assert reason, "a refusal must say what to do about it"
    return reason


# 1-2. A name is not an identity, and authority is declared.

def test_01_a_custom_command_impersonating_a_required_gate(governing):
    _refused(verify.gate_admissibility(
        _row("lint", "local", governing, definition_sha256="sha256:" + "0" * 64), governing, TARGET))


def test_02_a_local_command_impersonating_a_ci_only_gate(governing):
    _refused(verify.gate_admissibility(_row("container-build", "local", governing), governing, TARGET))


# 3-4. Executions and judgments cannot stand in for each other.

def test_03_an_execution_impersonating_a_judgment_rung(governing):
    _refused(verify.kind_status({"gate": "diff", "kind": verify.EXECUTION}, governing,
                                POLICY["judgment_rungs"]))


def test_04_an_attestation_impersonating_executable_evidence(governing):
    _refused(verify.kind_status({"gate": "lint", "kind": verify.ATTESTATION}, governing,
                                POLICY["judgment_rungs"]))


# 5-7. CI provenance is a chain, not a commit name.

EXPECTED_CI = {"head_sha": SHA, "repository": "o/r", "workflow": ".github/workflows/ci.yml",
               "job": "validate", "events": ["push"]}


def _ci_run(**o):
    return {"id": 1, "head_sha": SHA, "event": "push", "run_attempt": 1,
            "path": ".github/workflows/ci.yml", "repository": {"full_name": "o/r"}, **o}


def _ci_job(**o):
    return {"run_id": 1, "run_attempt": 1, "head_sha": SHA, "name": "validate", "steps": [], **o}


def test_05_a_ci_job_paired_with_another_run():
    _refused(verify.ci_provenance_status(_ci_run(), _ci_job(run_id=999), EXPECTED_CI))


def test_06_ci_evidence_from_another_repository():
    _refused(verify.ci_provenance_status(_ci_run(repository={"full_name": "fork/r"}), _ci_job(),
                                         EXPECTED_CI))


@pytest.mark.parametrize(("run_over", "job_over"),
                         [({"path": ".github/workflows/other.yml"}, {}), ({}, {"name": "publish"})])
def test_07_ci_evidence_from_the_wrong_workflow_or_job(run_over, job_over):
    _refused(verify.ci_provenance_status(_ci_run(**run_over), _ci_job(**job_over), EXPECTED_CI))


# 8. One contract per composition.

def test_08_evidence_from_an_incompatible_verifier():
    _refused(verify.verifier_status([
        {"verifier": {"compatibility": verify.COMPATIBILITY}},
        {"verifier": {"compatibility": "evidence-v2.9"}}]))


# 9. The index is part of the state.

def test_09_two_states_with_identical_worktrees_and_different_indexes(tmp_path):
    env = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000"}
    made = []
    for name, staged in (("left", "B\n"), ("right", "D\n")):
        path = tmp_path / name
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
        (path / "f").write_text("base\n")
        subprocess.run(["git", "add", "-A"], cwd=path, check=True)
        subprocess.run(["git", "-c", "user.email=a@b.invalid", "-c", "user.name=a", "commit", "-qm", "b"],
                       cwd=path, check=True, env={**__import__("os").environ, **env})
        (path / "f").write_text(staged)
        subprocess.run(["git", "add", "f"], cwd=path, check=True)
        (path / "f").write_text("C\n")
        made.append(path)
    assert verify.state_id(made[0]) == verify.state_id(made[1]), "premise: worktree digests collide"
    assert verify.repository_identity(made[0]) != verify.repository_identity(made[1])


# 10. A policy cannot decide its own verification.

def test_10_composition_under_a_weakened_working_tree_policy(repo):
    baseline = verify.baseline_record(repo, POLICY, [])
    weakened = {**POLICY, "gates": {**POLICY["gates"],
                                    "local": {"lint": {"command": "true", "target": ["repository"]}}}}
    state, findings = verify.governance_status(
        baseline, verify.gate_definitions(repo, weakened),
        [_row("lint", "local", verify.gate_definitions(repo, weakened))])
    assert state == verify.DEFINITION_PENDING
    assert findings


# 11-12. Specification identity.

def test_11_evidence_produced_before_a_declared_specification_changed(repo):
    (repo / "features").mkdir()
    spec = repo / "features" / "sign-in.md"
    spec.write_text("success, error, logout\n")
    declared = {"gates": {"behavioral": {"vl": {"driver": "d", "target": ["repository"],
                                                "spec_root": "features",
                                                "spec_files": ["features/sign-in.md"]}}}}
    before = verify.gate_definitions(repo, declared)
    spec.write_text("open the screen\n")
    _refused(verify.definition_status(_row("vl", "local", before), verify.gate_definitions(repo, declared)))


def test_12_a_specification_resolving_outside_its_root(repo):
    (repo / "features").mkdir()
    (repo / "escape.md").write_text("x\n")
    with pytest.raises(SystemExit):
        verify.spec_sources(repo, "vl", {"spec_root": "features", "spec_files": ["escape.md"]})


# 13. Artifact bytes are recomputed, never compared between records.

def test_13_an_artifact_that_is_missing_or_rehashes_differently(repo, governing):
    body = b"frames"
    (repo / "login.webm").write_bytes(body)
    artifacts = [{"kind": "recording", "path": "login.webm",
                  "sha256": "sha256:" + hashlib.sha256(body).hexdigest()}]
    execution = _row("verify-login", "local", governing, artifacts=artifacts)
    execution["record_id"] = verify.record_id(execution)
    attestation = {"gate": "verify-login", "kind": verify.ATTESTATION, "target_state": TARGET,
                   "execution_ref": execution["record_id"],
                   "artifact_refs": [a["sha256"] for a in artifacts]}
    rows = [execution, attestation]
    assert verify.behavioural_status(execution, governing, rows, repo) == (verify.ADMISSIBLE, None)
    (repo / "login.webm").write_bytes(b"re-recorded")
    _refused(verify.behavioural_status(execution, governing, rows, repo))


# 14-15. Projections: declared dimensions, no more and no fewer.

def test_14_a_runtime_move_expires_only_the_gates_that_declared_one():
    moved = {**TARGET, "runtime": {"manifest_sha256": "sha256:r2"}}
    record = {**TARGET, "runtime": {"manifest_sha256": "sha256:r1"}}
    assert verify.projection_status(record, ["repository"], moved)[0] == verify.ADMISSIBLE
    assert verify.projection_status(record, ["repository", "runtime"], moved)[0] == "STALE"


def test_15_evidence_omitting_a_declared_dimension():
    status, reason = verify.projection_status(TARGET, ["repository", "runtime"],
                                              {**TARGET, "runtime": {"manifest_sha256": "sha256:r"}})
    assert status == verify.INADMISSIBLE
    assert "omits" in reason


# 16-19. The artifact chain must resolve.

def test_16_an_attestation_referencing_no_execution_in_the_composition(repo, governing):
    execution = _row("verify-login", "local", governing, artifacts=[])
    _refused(verify.behavioural_status(execution, governing, [execution], repo))


def test_17_an_attestation_for_a_different_gate_or_projection(repo, governing):
    body = b"x"
    (repo / "a.bin").write_bytes(body)
    artifacts = [{"kind": "dom", "path": "a.bin", "sha256": "sha256:" + hashlib.sha256(body).hexdigest()}]
    execution = _row("verify-login", "local", governing, artifacts=artifacts)
    execution["record_id"] = verify.record_id(execution)
    elsewhere = {"gate": "verify-login", "kind": verify.ATTESTATION,
                 "target_state": {"repository": {"head": "f" * 40}},
                 "execution_ref": execution["record_id"],
                 "artifact_refs": [a["sha256"] for a in artifacts]}
    _refused(verify.behavioural_status(execution, governing, [execution, elsewhere], repo))


def test_18_a_tampered_record_id(repo, governing):
    body = b"x"
    (repo / "a.bin").write_bytes(body)
    artifacts = [{"kind": "dom", "path": "a.bin", "sha256": "sha256:" + hashlib.sha256(body).hexdigest()}]
    execution = _row("verify-login", "local", governing, artifacts=artifacts)
    execution["record_id"] = "sha256:" + "0" * 64
    _refused(verify.behavioural_status(execution, governing, [execution], repo))


def test_19_a_behavioural_execution_with_no_attestation(repo, governing):
    body = b"x"
    (repo / "a.bin").write_bytes(body)
    artifacts = [{"kind": "dom", "path": "a.bin", "sha256": "sha256:" + hashlib.sha256(body).hexdigest()}]
    execution = _row("verify-login", "local", governing, artifacts=artifacts)
    execution["record_id"] = verify.record_id(execution)
    reason = _refused(verify.behavioural_status(execution, governing, [execution], repo))
    assert "nothing attests" in reason


# Distinguished, and asserted separately: absence of evidence is not a mismatch.

def test_an_uninterrogable_runtime_is_blocked_not_stale():
    status, reason = verify.projection_status(
        {**TARGET, "runtime": {"manifest_sha256": "sha256:r"}}, ["repository", "runtime"], TARGET)
    assert status == "BLOCKED"
    assert "no target to compare" in reason


# Blocked on later work. Named rather than absent, so the gap is visible.

@pytest.mark.skip(reason="blocked on a findings ledger (design note decision H)")
def test_a_clean_result_while_a_tracked_finding_remains_open():
    raise AssertionError


@pytest.mark.skip(reason="blocked on evidence/3 activation; the CLI still emits evidence/2")
def test_the_cli_refuses_to_report_ready_when_no_gate_actually_ran():
    raise AssertionError
