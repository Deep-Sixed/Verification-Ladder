"""Target projections, and the index the worktree digest cannot see.

Two properties, both demonstrated as defects before they were fixed. A worktree
digest collides across checkouts whose staged content differs, so the index is
part of the state. And comparing whole target states expires gates for movement
they cannot depend on, so each gate is compared on the dimensions it declared -
never on fewer, and never on more.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
_spec = importlib.util.spec_from_file_location("ladder_verify_projection", SCRIPT)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)


def _repo(path: Path, staged: str, worktree: str) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "f.py").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.invalid", "-c", "user.name=a", "commit", "-qm", "base"],
                   cwd=path, check=True)
    (path / "f.py").write_text(staged)
    subprocess.run(["git", "add", "f.py"], cwd=path, check=True)
    (path / "f.py").write_text(worktree)
    return path


def test_the_index_is_part_of_the_state(tmp_path):
    """Same HEAD, same worktree bytes, same status codes - different staged content."""
    left = _repo(tmp_path / "left", staged="STAGED_B\n", worktree="WORKTREE_C\n")
    right = _repo(tmp_path / "right", staged="STAGED_D_different\n", worktree="WORKTREE_C\n")
    assert verify.state_id(left) == verify.state_id(right), "the worktree digest collides here; that is the premise"
    assert verify.index_state(left) != verify.index_state(right), (
        "two trees whose `git diff --cached` differs must not share a repository identity")
    assert verify.repository_identity(left) != verify.repository_identity(right)


def test_repository_identity_is_stable_for_one_tree(tmp_path):
    repo = _repo(tmp_path / "one", staged="S\n", worktree="W\n")
    assert verify.repository_identity(repo) == verify.repository_identity(repo)


REPO_A = {"head": "a" * 40, "index_state": "sha256:i1", "worktree_state": "sha256:w1"}
RUNTIME_1 = {"manifest_sha256": "sha256:r1", "facts": {"build": "1"}}
RUNTIME_2 = {"manifest_sha256": "sha256:r2", "facts": {"build": "2"}}


def test_a_repository_only_gate_survives_a_runtime_move():
    record = {"repository": REPO_A, "runtime": RUNTIME_1}
    current = {"repository": REPO_A, "runtime": RUNTIME_2}
    assert verify.projection_status(record, ["repository"], current) == (verify.ADMISSIBLE, None), (
        "lint cannot depend on a runtime, so a moved runtime must not expire it")


def test_a_runtime_dependent_gate_expires_when_the_runtime_moves():
    status, reason = verify.projection_status({"repository": REPO_A, "runtime": RUNTIME_1},
                                              ["repository", "runtime"],
                                              {"repository": REPO_A, "runtime": RUNTIME_2})
    assert status == "STALE"
    assert "runtime" in reason


def test_evidence_omitting_a_declared_dimension_is_inadmissible():
    """The omission attack: drop runtime from the record and outlive a runtime move."""
    status, reason = verify.projection_status({"repository": REPO_A},
                                              ["repository", "runtime"],
                                              {"repository": REPO_A, "runtime": RUNTIME_2})
    assert status == verify.INADMISSIBLE
    assert "omits" in reason
    assert status != "STALE", "a missing dimension must not be reported as merely out of date"


def test_an_uninterrogable_runtime_is_blocked_not_stale():
    status, reason = verify.projection_status({"repository": REPO_A, "runtime": RUNTIME_1},
                                              ["repository", "runtime"],
                                              {"repository": REPO_A})
    assert status == "BLOCKED", "no target and a different target are different diagnoses"
    assert "no target to compare" in reason


def test_a_dimension_the_gate_does_not_declare_is_never_compared():
    record = {"repository": REPO_A, "runtime": RUNTIME_1}
    current = {"repository": REPO_A, "runtime": RUNTIME_2}
    assert verify.target_projection(record, ["repository"]) == {"repository": REPO_A}
    assert verify.projection_status(record, ["repository"], current)[0] == verify.ADMISSIBLE


def test_runtime_identity_binds_to_its_facts():
    assert verify.runtime_identity({"build": "1"}) != verify.runtime_identity({"build": "2"})
    assert verify.runtime_identity({"b": 1, "a": 2}) == verify.runtime_identity({"a": 2, "b": 1})
    with pytest.raises(SystemExit):
        verify.runtime_identity({})


def test_a_behavioural_gate_gains_ci_authority_only_when_declared():
    behavioural = {"gates": {"behavioral": {"verify-login": {
        "driver": "node drive.mjs", "target": ["repository", "runtime"],
        "evidence_mode": "execution+attestation"}}}}
    assert verify.normalize_policy(behavioural)["verify-login"]["authorities"].keys() == {"local"}

    behavioural["gates"]["ci"] = {"verify-login": {
        "step": "Run behavioral login verification", "target": ["repository", "runtime"],
        "evidence_mode": "execution+attestation"}}
    gained = verify.normalize_policy(behavioural)["verify-login"]
    assert sorted(gained["authorities"]) == ["ci", "local"], (
        "CI authority is declared, never inferred from the gate being behavioural")


def test_a_baseline_carries_the_full_repository_identity(tmp_path):
    repo = _repo(tmp_path / "base", staged="S\n", worktree="W\n")
    record = verify.baseline_record(repo, {"gates": {"local": {"g": "true"}}}, [])
    assert set(record["target_state"]["repository"]) == {"head", "index_state", "worktree_state"}
