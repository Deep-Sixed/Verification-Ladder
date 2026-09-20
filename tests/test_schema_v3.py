"""The evidence/3 model: what a gate's identity is made of, and what it refuses.

Nothing here asserts on emitted records. The v3 shape, its parsing and its
validation land ahead of the rules that will use them, so those rules are written
against a settled model rather than discovering it. Records on disk stay
evidence/2 until the rules land.
"""

import importlib.util
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "verification-ladder" / "bin" / "verify.py"
_spec = importlib.util.spec_from_file_location("ladder_verify_schema_v3", SCRIPT)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)

LEGACY = {
    "required_gates": ["lint", "tests"],
    "gates": {"local": {"lint": "ruff check .", "tests": "pytest -q"}},
    "ci_steps": {"lint": "Run ruff check ."},
}


@pytest.fixture
def repo(tmp_path):
    checkout = tmp_path / "repo"
    (checkout / "verification" / "features").mkdir(parents=True)
    (checkout / "verification" / "features" / "sign-in.md").write_text("# Sign in\n\nsuccess, error, logout.\n")
    (checkout / "outside.md").write_text("not under the root\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(checkout)], check=True)
    subprocess.run(["git", "add", "-A"], cwd=checkout, check=True)
    subprocess.run(["git", "-c", "user.email=ci@example.invalid", "-c", "user.name=ci",
                    "commit", "-qm", "baseline"], cwd=checkout, check=True)
    return checkout


def test_the_version_tracks_the_package_it_ships_with():
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert verify.VERSION == declared, "a verifier that misreports its own version is not evidence provenance"


def test_canonical_form_does_not_depend_on_key_order():
    assert verify.digest_of({"b": 1, "a": [1, 2]}) == verify.digest_of({"a": [1, 2], "b": 1})


def test_compatibility_is_not_the_schema():
    identity = verify.verifier_identity()
    assert identity["compatibility"] != identity["schema"], (
        "the record shape and the semantics applied to it version separately")
    assert identity["implementation_sha256"].startswith("sha256:")


def test_a_legacy_policy_normalizes_to_v3_defaults():
    gates = verify.normalize_policy(LEGACY)
    assert gates["tests"]["target"] == ["repository"]
    assert gates["tests"]["evidence_mode"] == "execution"
    assert gates["tests"]["authorities"] == {"local": {"command": "pytest -q"}}


def test_normalization_is_deterministic():
    assert verify.normalize_policy(LEGACY) == verify.normalize_policy(tomllib.loads(
        'required_gates = ["lint", "tests"]\n'
        '[gates.local]\nlint = "ruff check ."\ntests = "pytest -q"\n'
        '[ci_steps]\nlint = "Run ruff check ."\n'))


def test_one_gate_may_be_established_by_more_than_one_authority():
    assert sorted(verify.normalize_policy(LEGACY)["lint"]["authorities"]) == ["ci", "local"]


def test_target_belongs_to_the_gate_not_to_one_authority():
    with pytest.raises(SystemExit) as raised:
        verify.normalize_policy({
            "gates": {"local": {"g": {"command": "x", "target": ["repository"]}},
                      "ci": {"g": {"step": "s", "target": ["repository", "runtime"]}}}})
    assert raised.value.code == 2


@pytest.mark.parametrize("target", [[], ["repository", "galaxy"], "repository"])
def test_an_unusable_target_is_refused(target):
    with pytest.raises(SystemExit):
        verify.normalize_policy({"gates": {"local": {"g": {"command": "x", "target": target}}}})


def test_a_dotted_gate_name_is_refused_rather_than_silently_missing():
    declared = tomllib.loads('[gates.local."tests-3".11]\ncommand = "pytest"\n')
    with pytest.raises(SystemExit) as raised:
        verify.normalize_policy(declared)
    assert raised.value.code == 2


def test_a_specification_outside_its_root_is_refused(repo):
    gate = {"target": ["repository", "runtime"], "evidence_mode": "execution+attestation",
            "authorities": {"local": {"driver": "drive.mjs"}},
            "spec_root": "verification/features", "spec_files": ["outside.md"]}
    with pytest.raises(SystemExit) as raised:
        verify.spec_sources(repo, "verify-login", gate)
    assert raised.value.code == 2


def test_spec_root_itself_cannot_escape_the_repository(repo):
    external = repo.parent / "shared-specs"
    external.mkdir()
    (external / "sign-in.md").write_text("not project owned\n")
    gate = {"target": ["repository"], "evidence_mode": "execution+attestation",
            "authorities": {"local": {"driver": "drive.mjs"}},
            "spec_root": "../shared-specs", "spec_files": ["../shared-specs/sign-in.md"]}
    with pytest.raises(SystemExit) as raised:
        verify.spec_sources(repo, "verify-login", gate)
    assert raised.value.code == 2


def test_spec_files_without_a_root_are_refused(repo):
    gate = {"target": ["repository"], "evidence_mode": "execution",
            "authorities": {"local": {"driver": "d"}}, "spec_files": ["verification/features/sign-in.md"]}
    with pytest.raises(SystemExit):
        verify.spec_sources(repo, "verify-login", gate)


def _behavioural(repo, command="ruff check ."):
    return {
        "gates": {
            "local": {"lint": {"command": command, "target": ["repository"]}},
            "behavioral": {"verify-login": {
                "driver": "node drive.mjs", "target": ["repository", "runtime"],
                "evidence_mode": "execution+attestation",
                "spec_root": "verification/features",
                "spec_files": ["verification/features/sign-in.md"]}}}}


def test_definition_identity_is_gate_scoped(repo):
    before = verify.gate_definitions(repo, _behavioural(repo))
    after = verify.gate_definitions(repo, _behavioural(repo, command="ruff check . --fix"))
    assert before["lint"]["definition_sha256"] != after["lint"]["definition_sha256"]
    assert before["verify-login"]["definition_sha256"] == after["verify-login"]["definition_sha256"], (
        "editing one gate must not invalidate evidence for an unrelated one")


def test_changing_a_declared_specification_changes_the_gate_definition(repo):
    before = verify.gate_definitions(repo, _behavioural(repo))["verify-login"]
    (repo / "verification" / "features" / "sign-in.md").write_text("# Sign in\n\nopen the screen.\n")
    after = verify.gate_definitions(repo, _behavioural(repo))["verify-login"]
    assert before["definition_sha256"] != after["definition_sha256"], (
        "a weakened specification must not keep the identity its evidence was bound to")
    assert before["sources"][0]["path"] == "verification/features/sign-in.md"


def test_the_declared_target_is_part_of_the_definition(repo):
    wider = _behavioural(repo)
    narrowed = _behavioural(repo)
    narrowed["gates"]["behavioral"]["verify-login"]["target"] = ["repository"]
    assert (verify.gate_definitions(repo, wider)["verify-login"]["definition_sha256"]
            != verify.gate_definitions(repo, narrowed)["verify-login"]["definition_sha256"]), (
        "dropping runtime from a gate is a definition change, not a quiet weakening")


def test_a_baseline_record_says_how_it_came_to_exist(repo):
    captured = verify.baseline_record(repo, LEGACY, [])
    assert captured["origin"] == "captured"
    assert captured["schema"] == verify.BASELINE_SCHEMA
    assert captured["governing"]["policy_sha256"].startswith("sha256:")
    assert verify.baseline_record(repo, LEGACY, [], origin="reconstructed")["origin"] == "reconstructed"


def test_a_baseline_cannot_invent_an_origin(repo):
    with pytest.raises(SystemExit):
        verify.baseline_record(repo, LEGACY, [], origin="assumed")


def test_the_emitted_schema_has_not_moved_yet():
    assert verify.SCHEMA == "verification.ladder.evidence/2", (
        "the model lands before the rules; emitting v3 while composing under v2 would be worse than either")
