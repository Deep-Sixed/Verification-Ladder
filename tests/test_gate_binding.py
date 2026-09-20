"""Gate identity and permitted authority — the two forgeries the CLI replay demonstrated.

A custom command wearing a required gate's name satisfied it; a local no-op
satisfied a CI-only gate. Both worked because composition read a name and a
status and nothing else. A row is now read as evidence only when the definition
it was produced under is the one in force, and the authority that produced it is
one the policy lets establish that gate.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
_spec = importlib.util.spec_from_file_location("ladder_verify_binding", SCRIPT)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)

POLICY = {
    "required_gates": ["lint", "container-build"],
    "gates": {"local": {"lint": {"command": "ruff check .", "target": ["repository"]}},
              "ci": {"container-build": {"step": "Run docker build .", "target": ["repository"]}}},
}
REPO = {"head": "a" * 40, "index_state": "sha256:i", "worktree_state": "sha256:w"}
TARGET = {"repository": REPO}


@pytest.fixture
def governing(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    return verify.gate_definitions(tmp_path, POLICY)


def _row(name, authority, governing, **over):
    return {"gate": name, "authority": authority,
            "definition_sha256": governing[name]["definition_sha256"],
            "target_state": TARGET, **over}


def test_an_honest_row_is_admissible(governing):
    assert verify.gate_admissibility(_row("lint", "local", governing), governing, TARGET) == (
        verify.ADMISSIBLE, None)


def test_a_custom_command_cannot_wear_a_required_gates_name(governing):
    """`--gate lint="true"` produced a row named lint under a definition of its own."""
    forged = _row("lint", "local", governing, definition_sha256="sha256:" + "0" * 64)
    status, reason = verify.gate_admissibility(forged, governing, TARGET)
    assert status == verify.INADMISSIBLE
    assert "definition" in reason


def test_a_local_command_cannot_satisfy_a_ci_only_gate(governing):
    """The container build this checkout cannot run, satisfied by a local no-op."""
    status, reason = verify.gate_admissibility(_row("container-build", "local", governing), governing, TARGET)
    assert status == verify.INADMISSIBLE
    assert "not by 'local'" in reason


def test_ci_may_establish_the_gate_it_is_declared_for(governing):
    assert verify.authority_status("container-build", "ci", governing) == (verify.ADMISSIBLE, None)


def test_a_gate_the_policy_does_not_declare_establishes_nothing(governing):
    status, reason = verify.gate_admissibility(_row("lint", "local", governing) | {"gate": "invented"},
                                               governing, TARGET)
    assert status == verify.INADMISSIBLE
    assert "not a gate this policy declares" in reason


def test_definition_drift_is_inadmissible_rather_than_failing(governing, tmp_path):
    """The gate passed — under a meaning no longer in force. That is not a FAIL."""
    weakened = {"required_gates": ["lint", "container-build"],
                "gates": {"local": {"lint": {"command": "ruff check . --select E501", "target": ["repository"]}},
                          "ci": POLICY["gates"]["ci"]}}
    now = verify.gate_definitions(tmp_path, weakened)
    status, reason = verify.definition_status(_row("lint", "local", governing), now)
    assert status == verify.INADMISSIBLE
    assert "re-verify under the definition in force" in reason


def test_an_unrelated_gate_survives_a_definition_change_elsewhere(governing, tmp_path):
    changed = {"required_gates": ["lint", "container-build"],
               "gates": {"local": {"lint": {"command": "ruff check . --fix", "target": ["repository"]}},
                         "ci": POLICY["gates"]["ci"]}}
    now = verify.gate_definitions(tmp_path, changed)
    assert verify.definition_status(_row("container-build", "ci", governing), now)[0] == verify.ADMISSIBLE, (
        "editing lint must not invalidate evidence for the container build")


def test_binding_is_checked_before_projection(governing):
    """A row whose definition moved is not evidence whose target is worth comparing."""
    forged = _row("lint", "local", governing, definition_sha256="sha256:" + "0" * 64,
                  target_state={"repository": {"head": "b" * 40}})
    status, reason = verify.gate_admissibility(forged, governing, TARGET)
    assert "definition" in reason, "the first thing wrong should be the first thing reported"
    assert status == verify.INADMISSIBLE


def test_a_behavioural_gate_declared_for_ci_accepts_ci(tmp_path):
    policy = {"gates": {
        "behavioral": {"verify-login": {"driver": "node drive.mjs", "target": ["repository", "runtime"],
                                        "evidence_mode": "execution+attestation"}},
        "ci": {"verify-login": {"step": "Run behavioral login verification",
                                "target": ["repository", "runtime"],
                                "evidence_mode": "execution+attestation"}}}}
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    now = verify.gate_definitions(tmp_path, policy)
    assert verify.authority_status("verify-login", "ci", now)[0] == verify.ADMISSIBLE
    assert verify.authority_status("verify-login", "local", now)[0] == verify.ADMISSIBLE


def test_a_behavioural_gate_not_declared_for_ci_refuses_ci(tmp_path):
    policy = {"gates": {"behavioral": {"verify-login": {
        "driver": "node drive.mjs", "target": ["repository", "runtime"],
        "evidence_mode": "execution+attestation"}}}}
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    now = verify.gate_definitions(tmp_path, policy)
    status, reason = verify.authority_status("verify-login", "ci", now)
    assert status == verify.INADMISSIBLE
    assert "never assumed" in reason, "CI permission is declared, not inferred from the gate being behavioural"
