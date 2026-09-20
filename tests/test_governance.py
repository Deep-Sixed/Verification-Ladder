"""A definition never governs its own introduction.

Composition reads the definitions captured at baseline, not the ones in the
working tree - a policy read from the tree at composition time is one the task
could have edited, and an uncommitted edit was enough to weaken it. A task that
does change a definition is its own class: it may collect evidence under the
candidate, and CLEAN CLIMB must not read TRUE on the strength of it.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
_spec = importlib.util.spec_from_file_location("ladder_verify_governance", SCRIPT)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)

STRICT = {"required_gates": ["lint", "tests"],
          "gates": {"local": {"lint": {"command": "ruff check .", "target": ["repository"]},
                              "tests": {"command": "pytest -q", "target": ["repository"]}}}}
WEAKENED = {"required_gates": ["lint", "tests"],
            "gates": {"local": {"lint": {"command": "ruff check . --select E501", "target": ["repository"]},
                                "tests": {"command": "pytest -q", "target": ["repository"]}}}}


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    return tmp_path


@pytest.fixture
def baseline(repo):
    return verify.baseline_record(repo, STRICT, [])


def _rows(definitions, *names):
    return [{"gate": n, "definition_sha256": definitions[n]["definition_sha256"]} for n in names]


def test_an_unchanged_definition_leaves_governance_alone(repo, baseline):
    same = verify.gate_definitions(repo, STRICT)
    assert verify.governance_status(baseline, same, _rows(same, "lint", "tests")) == (
        verify.DEFINITION_UNCHANGED, [])


def test_a_weakened_policy_is_a_governance_change_not_a_clean_climb(repo, baseline):
    """The uncommitted working-tree edit that used to decide its own verification."""
    candidate = verify.gate_definitions(repo, WEAKENED)
    state, findings = verify.governance_status(baseline, candidate, _rows(candidate, "lint", "tests"))
    assert state == verify.DEFINITION_PENDING
    assert findings == ["lint: candidate definition only"]


def test_an_unrelated_gate_is_not_dragged_into_the_change(repo, baseline):
    candidate = verify.gate_definitions(repo, WEAKENED)
    _, changed = verify.definition_change(baseline, candidate)
    assert changed == ["lint"], "tests did not change and must not be reported as though it had"


def test_adding_and_removing_gates_are_both_definition_changes(repo, baseline):
    added = dict(STRICT)
    added["gates"] = {"local": dict(STRICT["gates"]["local"],
                                    audit={"command": "audit", "target": ["repository"]})}
    state, changed = verify.definition_change(baseline, verify.gate_definitions(repo, added))
    assert (state, changed) == (verify.DEFINITION_PENDING, ["audit (added)"])

    dropped = {"gates": {"local": {"lint": STRICT["gates"]["local"]["lint"]}}}
    state, changed = verify.definition_change(baseline, verify.gate_definitions(repo, dropped))
    assert (state, changed) == (verify.DEFINITION_PENDING, ["tests (removed)"])


def test_the_governing_definitions_come_from_the_baseline_record(repo, baseline):
    governing = verify.governing_definitions(baseline)
    assert set(governing) == {"lint", "tests"}
    assert governing["lint"]["definition_sha256"] == (
        verify.gate_definitions(repo, STRICT)["lint"]["definition_sha256"])


def test_a_record_that_is_not_a_baseline_cannot_supply_governing_definitions():
    with pytest.raises(SystemExit) as raised:
        verify.governing_definitions({"schema": verify.SCHEMA, "governing": {"gates": {}}})
    assert raised.value.code == 2


def test_the_candidate_becomes_governing_only_as_the_next_baseline(repo):
    """Accepted at the human gate, the candidate governs subsequent tasks — not its own."""
    next_baseline = verify.baseline_record(repo, WEAKENED, [])
    candidate = verify.gate_definitions(repo, WEAKENED)
    assert verify.governance_status(next_baseline, candidate, _rows(candidate, "lint", "tests")) == (
        verify.DEFINITION_UNCHANGED, [])
