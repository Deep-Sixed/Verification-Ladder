"""§M2a: evidence/3 policy accepted for the shadow path, and for nothing else.

M2 could compare no behavioural gate, because a behavioural gate needs v3 policy
syntax and the authoritative parser refuses it. The `[shadow]` layer cuts that
circularity. Everything here is therefore about a boundary rather than a
feature: a shadow declaration must be able to describe an execution under
evidence/3 while remaining unable to cause one, satisfy one, or fail a task.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
_spec = importlib.util.spec_from_file_location("ladder_verify_shadow_policy", SCRIPT)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)

# The driver writes its artifact under .verification/, which is excluded from
# repository state by construction - otherwise the gate moves the tree it is
# measuring and every run reports BLOCKED on drift.
DRIVER = """import json, pathlib, sys
out = pathlib.Path(".verification/artifacts")
out.mkdir(parents=True, exist_ok=True)
out.joinpath("journey.json").write_text(json.dumps({"ok": True}) + "\\n")
sys.exit(0)
"""
BASE = '''required_gates = ["unit", "verify-login"]
judgment_rungs = ["diff"]

[gates.local]
unit = "true"
verify-login = "python3 drive.py"

[ci_steps]
unit = "Run true"
'''
SHADOW = '''
[shadow.runtime]
facts_file = ".verification/runtime.json"

[shadow.gates.behavioral.verify-login]
driver = "python3 drive.py"
target = ["repository", "runtime"]
evidence_mode = "execution+attestation"
spec_root = "."
spec_files = ["journey.md"]
artifacts = [".verification/artifacts/journey.json"]
'''
FACTS = '{"build": "abc123", "instance": "one"}\n'


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "drive.py").write_text(DRIVER)
    (tmp_path / "journey.md").write_text("# Login\n\nExercise success and failure.\n")
    (tmp_path / ".gitignore").write_text(".verification/\n__pycache__/\n")
    (tmp_path / "verification.toml").write_text(BASE + SHADOW)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.invalid", "-c", "user.name=a", "commit", "-qm", "b"],
                   cwd=tmp_path, check=True)
    (tmp_path / ".verification").mkdir(exist_ok=True)
    (tmp_path / ".verification" / "runtime.json").write_text(FACTS)
    return tmp_path


def cli(repo, *args):
    r = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *args],
                       capture_output=True, text=True, check=False)
    return r.returncode, r.stdout + r.stderr


def record(repo, name="local"):
    return repo / ".verification" / f"{name}.json"


def executed(repo, name="local"):
    # Remove any earlier shadow first. A shadow the policy refused to rebuild
    # leaves the previous one in place, and a test that then reads it is
    # asserting against the run before the one it set up.
    stale = repo / ".verification" / "shadow" / f"{name}.v3.json"
    stale.unlink(missing_ok=True)
    code, output = cli(repo, "run", "--output", str(record(repo, name)))
    assert code == 0, output
    return record(repo, name)


def shadow_of(repo, name="local"):
    return json.loads((repo / ".verification" / "shadow" / f"{name}.v3.json").read_text())


def row(repo, gate, predicate, name="local"):
    code, output = cli(repo, "compare", str(record(repo, name)))
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and gate in parts and line.strip().endswith(predicate):
            return line.split(gate)[0].strip(), output, code
    raise AssertionError(f"no {predicate!r} row for {gate!r} in:\n{output}")


# --- the authority boundary --------------------------------------------------

def test_a_shadow_declaration_does_not_become_executable(repo):
    """A gate only the shadow layer declares must never be run."""
    (repo / "verification.toml").write_text(BASE + SHADOW + '''
[shadow.gates.behavioral.verify-checkout]
driver = "python3 does-not-exist.py"
target = ["repository"]
evidence_mode = "execution+attestation"
''')
    code, output = cli(repo, "run", "--output", str(record(repo)))
    assert code == 0, output
    ran = {gate["name"] for gate in json.loads(record(repo).read_text())["gates"]}
    assert ran == {"unit", "verify-login"}, "the shadow layer put a command in the run"
    assert shadow_of(repo)["unpaired"] == ["verify-checkout"]


def test_an_unpaired_shadow_gate_is_reported_not_dropped(repo):
    executed(repo)
    _, output = cli(repo, "compare", str(record(repo)))
    assert "verify-checkout" not in output
    (repo / "verification.toml").write_text(BASE + SHADOW + '''
[shadow.gates.local.only-here]
command = "true"
target = ["repository"]
''')
    executed(repo)
    outcome, output, _ = row(repo, "only-here", "pairing")
    assert outcome == "NOT COMPARABLE", output
    assert "does not become executable" in output


def test_the_authoritative_parser_never_reads_the_shadow_layer(repo):
    declared = verify.policy(repo)
    assert dict(verify.local_gates(declared)) == {"unit": "true", "verify-login": "python3 drive.py"}
    assert verify.gate_map(declared, "ci_steps") == {"unit": "Run true"}


def test_v3_syntax_in_the_authoritative_namespace_is_still_refused(repo):
    (repo / "verification.toml").write_text(BASE + SHADOW + '''
[gates.behavioral.sneaky]
driver = "true"
''')
    code, output = cli(repo, "run", "--output", str(record(repo)))
    assert code == 2
    assert "[gates.behavioral.*]" in output and "evidence/3" in output


def test_a_malformed_shadow_policy_is_a_diagnostic_not_a_block(repo):
    (repo / "verification.toml").write_text(BASE + '''
[shadow.gates.behavioral.broken]
target = ["repository"]
''')
    code, output = cli(repo, "run", "--output", str(record(repo)))
    assert code == 0, "a broken shadow layer must not fail a task it has no authority over"
    assert json.loads(record(repo).read_text())["verdict"] == "PASS"
    assert "shadow   not constructed" in output
    assert not (repo / ".verification" / "shadow" / "local.v3.json").exists()


def test_a_malformed_shadow_policy_makes_the_comparison_incomparable(repo):
    executed(repo)
    (repo / "verification.toml").write_text(BASE + '''
[shadow.gates.behavioral.broken]
target = ["repository"]
''')
    code, output = cli(repo, "compare", str(record(repo)))
    assert code == 2
    assert "NOT COMPARABLE" in output


def test_the_shadow_layer_replaces_rather_than_merges(repo):
    """One gate, one meaning: the driver declaration wins outright."""
    executed(repo)
    gate = next(g for g in shadow_of(repo)["gates"] if g["gate"] == "verify-login")
    assert gate["permitted_authorities"] == ["local"], (
        "the authoritative [gates.local] entry must not survive alongside the shadow driver")
    assert gate["evidence_mode"] == "execution+attestation"
    unit = next(g for g in shadow_of(repo)["gates"] if g["gate"] == "unit")
    assert unit["permitted_authorities"] == ["ci", "local"], "a gate the shadow layer is silent about keeps both"


def test_ci_authority_is_declared_in_the_shadow_layer_too(repo):
    executed(repo)
    outcome, _, _ = row(repo, "verify-login", "authority")
    assert outcome == "AGREE"
    (repo / "verification.toml").write_text(BASE + SHADOW + '''
[shadow.gates.ci.verify-login]
step = "Run drive"
target = ["repository", "runtime"]
evidence_mode = "execution+attestation"
''')
    executed(repo)
    gate = next(g for g in shadow_of(repo)["gates"] if g["gate"] == "verify-login")
    assert gate["permitted_authorities"] == ["ci", "local"]


def test_a_gate_whose_shadow_authorities_disagree_about_its_target_is_refused(repo):
    """Target and mode belong to the gate, not to one way of establishing it."""
    (repo / "verification.toml").write_text(BASE + SHADOW + '''
[shadow.gates.ci.verify-login]
step = "Run drive"
''')
    code, output = cli(repo, "run", "--output", str(record(repo)))
    assert code == 0, "still only a diagnostic"
    assert "target differs between its authorities" in output


# --- what the layer makes reachable -----------------------------------------

def test_a_behavioural_gate_records_its_declared_artifacts(repo):
    executed(repo)
    gate = next(g for g in shadow_of(repo)["gates"] if g["gate"] == "verify-login")
    body = (repo / ".verification" / "artifacts" / "journey.json").read_bytes()
    import hashlib
    assert gate["artifacts"] == [{"path": ".verification/artifacts/journey.json",
                                  "sha256": "sha256:" + hashlib.sha256(body).hexdigest()}]


def test_a_missing_artifact_is_recorded_rather_than_losing_the_record(repo):
    executed(repo)
    (repo / ".verification" / "artifacts" / "journey.json").unlink()
    code, output = cli(repo, "run", "--output", str(record(repo, "again")))
    assert code == 0, output
    # The driver recreates it, so remove it from the definition's reach instead.
    (repo / "verification.toml").write_text(
        (BASE + SHADOW).replace('".verification/artifacts/journey.json"', '".verification/absent.json"'))
    executed(repo, "third")
    gate = next(g for g in shadow_of(repo, "third")["gates"] if g["gate"] == "verify-login")
    assert gate["artifacts"] == [{"path": ".verification/absent.json", "sha256": None}]


def test_every_gate_carries_only_the_dimensions_it_declared(repo):
    executed(repo)
    gates = {g["gate"]: g for g in shadow_of(repo)["gates"]}
    assert set(gates["unit"]["target_projection"]) == {"repository"}
    assert set(gates["verify-login"]["target_projection"]) == {"repository", "runtime"}


@pytest.mark.parametrize(
    ("label", "prepare", "outcome", "because"),
    [
        pytest.param("matching", lambda p: None, "AGREE", "", id="matching"),
        pytest.param("moved", lambda p: p.write_text('{"build": "def456"}\n'),
                     "DISAGREE", "binds to another 'runtime'", id="moved"),
        pytest.param("uninterrogable", lambda p: p.unlink(),
                     "NOT COMPARABLE", "cannot be interrogated here", id="uninterrogable"),
    ],
)
def test_the_runtime_dimension_distinguishes_its_three_readings(repo, label, prepare, outcome, because):
    executed(repo)
    prepare(repo / ".verification" / "runtime.json")
    got, output, _ = row(repo, "verify-login", "target projection")
    assert got == outcome, f"{label}: {output}"
    assert because in output
    # The repository-only gate must survive every one of them, or this is a
    # global freshness flag wearing a projection's name.
    survived, _, _ = row(repo, "unit", "target projection")
    assert survived == "AGREE", f"{label}: a runtime move expired a gate that cannot depend on one"


def test_a_runtime_absent_when_the_record_was_made_is_not_a_matching_one(repo):
    (repo / ".verification" / "runtime.json").unlink()
    executed(repo)
    (repo / ".verification" / "runtime.json").write_text(FACTS)
    got, output, _ = row(repo, "verify-login", "target projection")
    assert got == "NOT COMPARABLE"
    assert "omits the 'runtime' dimension" in output


def test_the_recorded_projection_must_be_the_records_own_target(repo):
    executed(repo)
    path = repo / ".verification" / "shadow" / "local.v3.json"
    shadow = json.loads(path.read_text())
    shadow["target_state"]["repository"]["index_state"] = "sha256:" + "1" * 64
    path.write_text(json.dumps(shadow, indent=2))
    got, output, _ = row(repo, "unit", "projection binding")
    assert got == "DISAGREE", output
    assert "DISAGREE 2" in output, "both gates' projections are bound to the one target"


def test_the_aggregate_says_what_its_parts_say(repo):
    """gate_admissibility is an aggregator, and that is shown rather than asserted."""
    executed(repo)
    _, output = cli(repo, "compare", str(record(repo)))
    assert "admissibility (aggregate)" in output
    governing = verify.gate_definitions(repo, verify.shadow_declaration(verify.policy(repo)))
    current = verify.shadow_target(repo, verify.policy(repo),
                                   verify.repository_identity(repo, verify.relative_inside(repo, [])))
    good = {"gate": "unit", "authority": "local",
            "definition_sha256": governing["unit"]["definition_sha256"],
            "target_projection": {"repository": current["repository"]}}
    cases = [
        ("clean", good),
        ("definition moved", {**good, "definition_sha256": "sha256:" + "0" * 64}),
        ("authority not permitted", {**good, "authority": "agent"}),
        ("projection moved", {**good, "target_projection": {"repository": {"head": "b" * 40}}}),
        ("dimension omitted", {**good, "target_projection": {}}),
        ("unknown gate", {**good, "gate": "nowhere"}),
    ]
    for label, claimed in cases:
        parts = [verify.definition_status(claimed, governing)[0],
                 verify.authority_status(claimed["gate"], claimed["authority"], governing)[0]]
        if claimed["gate"] in governing:
            parts.append(verify.projection_status(claimed["target_projection"],
                                                  governing[claimed["gate"]]["target"], current)[0])
        expected = verify.ADMISSIBLE if all(p == verify.ADMISSIBLE for p in parts) else "refused"
        got = verify.gate_admissibility(claimed, governing, current)[0]
        assert (got == verify.ADMISSIBLE) == (expected == verify.ADMISSIBLE), label


# --- the boundary the whole layer rests on ----------------------------------

def test_the_shadow_layer_changes_nothing_about_the_authoritative_result(repo):
    """Everything the task depends on, with the layer and without it.

    The state id is deliberately not compared: rewriting a tracked policy file
    moves the tree, so a whole-record equality here would be measuring the edit
    rather than the layer. What must not move is the verdict, the exit code, the
    drift reading and every gate outcome.
    """
    def outcome(name):
        stale = repo / ".verification" / "shadow" / f"{name}.v3.json"
        stale.unlink(missing_ok=True)
        code, _ = cli(repo, "run", "--output", str(record(repo, name)))
        got = json.loads(record(repo, name).read_text())
        return code, got["verdict"], got["drift"], got["gate_set"], [
            (g["name"], g["status"], g["exit_code"]) for g in got["gates"]]

    with_layer = outcome("with")
    (repo / "verification.toml").write_text(BASE)
    without_layer = outcome("without")
    assert with_layer == without_layer, (
        "the [shadow] layer altered the authoritative result it has no authority over")
