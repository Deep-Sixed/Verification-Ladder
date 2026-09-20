"""A verification result belongs to one repository state and cannot be inherited.

`verify.py` is the mechanical half of the Verification Ladder: it runs a gate set
and binds the outcome to the state that produced it. These lock in the properties
the procedure depends on - a repair invalidates the prior PASS, a gate that could
not run is never a pass, a state change during the run leaves the results bound to
nothing, and a repository that has declared no policy is BLOCKED rather than
silently verified.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
_spec = importlib.util.spec_from_file_location("ladder_verify", SCRIPT)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)

PASS_GATE = f"{sys.executable} -c pass"


@pytest.fixture
def repo(tmp_path):
    """A one-commit repository standing in for a checkout under verification."""
    checkout = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(checkout)], check=True)
    (checkout / "unit.py").write_text("VALUE = 1\n")
    # A governed repository. `--gate` is a diagnostic convenience and no longer
    # licenses a record against a repository that has declared nothing, so the
    # tests that drive custom gates need a policy to be driving them against.
    (checkout / "verification.toml").write_text('required_gates = []\n\n[gates.local]\nunit = "true"\n')
    subprocess.run(["git", "add", "-A"], cwd=checkout, check=True)
    subprocess.run(
        ["git", "-c", "user.email=ci@example.invalid", "-c", "user.name=ci", "commit", "-qm", "baseline"],
        cwd=checkout,
        check=True,
    )
    return checkout


def run(repo, *args):
    """Invoke the recorder the way an agent does, returning (exit code, stdout)."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout


def record_of(repo, *gates, output=None):
    args = ["run"]
    for name, command in gates:
        args += ["--gate", f"{name}={command}"]
    if output:
        args += ["--output", str(output)]
    code, stdout = run(repo, *args)
    return code, json.loads(Path(output).read_text() if output else stdout)


def test_state_id_covers_the_worktree_not_just_head(repo):
    baseline = verify.state_id(repo)
    assert verify.state_id(repo) == baseline
    (repo / "unit.py").write_text("VALUE = 2\n")
    assert verify.state_id(repo) != baseline
    (repo / "unit.py").write_text("VALUE = 1\n")
    assert verify.state_id(repo) == baseline, "identical content must identify as the same state"
    (repo / "extra.txt").write_text("untracked\n")
    assert verify.state_id(repo) != baseline, "an untracked file is part of what would be verified"


def test_symlink_is_identified_by_target_not_by_what_it_points_at(repo, tmp_path):
    """Following a link would fold outside content into the state id."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.txt").write_text("one\n")
    baseline = verify.state_id(repo)

    link = repo / "link"
    link.symlink_to(outside)
    linked = verify.state_id(repo)
    assert linked != baseline

    (outside / "payload.txt").write_text("two\n")
    assert verify.state_id(repo) == linked, "content outside the repository is not part of its state"

    link.unlink()
    link.symlink_to(tmp_path / "elsewhere")
    assert verify.state_id(repo) != linked, "retargeting the link is a state change"


def test_pass_binds_to_the_state_that_produced_it(repo):
    code, record = record_of(repo, ("unit", PASS_GATE))
    assert code == 0
    assert record["verdict"] == "PASS"
    assert record["repository"]["state_id"] == verify.state_id(repo)
    assert record["repository"]["head"] == verify.head_sha(repo)
    assert record["gate_set"] == "custom"


def test_repair_makes_the_prior_pass_stale(repo, tmp_path):
    evidence = tmp_path / "evidence.json"
    code, record = record_of(repo, ("unit", PASS_GATE), output=evidence)
    assert (code, record["verdict"]) == (0, "PASS")
    assert run(repo, "check", str(evidence))[0] == 0

    (repo / "unit.py").write_text("VALUE = 2\n")  # the repair
    code, stdout = run(repo, "check", str(evidence))
    assert code == 2
    assert stdout.startswith("STALE")
    assert "re-verify" in stdout


def test_failing_gate_is_recorded_with_its_output(repo, tmp_path):
    failing = tmp_path / "contract.py"
    failing.write_text("import sys\nsys.stderr.write('requirement-not-met\\n')\nsys.exit(1)\n")
    code, record = record_of(repo, ("unit", PASS_GATE), ("contract", f"{sys.executable} {failing}"))
    assert code == 1
    assert record["verdict"] == "FAIL"
    gate = record["gates"][1]
    assert gate["status"] == "FAIL" and gate["exit_code"] == 1
    assert "requirement-not-met" in gate["output_tail"]


def test_gate_that_cannot_run_is_blocked_never_passed(repo):
    code, record = record_of(repo, ("container", "cerberus-no-such-executable --check"))
    assert code == 2
    assert record["verdict"] == "BLOCKED"
    assert record["gates"][0]["status"] == "BLOCKED"
    assert "FileNotFoundError" in record["gates"][0]["reason"]


def test_state_change_during_the_run_blocks_the_result(repo, tmp_path):
    mutate = tmp_path / "mutate.py"
    mutate.write_text("from pathlib import Path\nPath('unit.py').write_text('VALUE = 3\\n')\n")
    code, record = record_of(repo, ("mutating", f"{sys.executable} {mutate}"))
    assert code == 2
    assert record["gates"][0]["status"] == "PASS"
    assert record["drift"] is True
    assert record["verdict"] == "BLOCKED", "a green gate over a moving tree establishes nothing"
    assert record["state_id_after"] != record["repository"]["state_id"]


def test_drift_names_the_paths_that_moved(repo, tmp_path):
    """A digest cannot say what changed, and "two state ids" sends nobody anywhere.

    The common cause is a gate writing its own cache into the tree it is being
    measured against, so the record names the paths and the fix is obvious.
    """
    litter = tmp_path / "litter.py"
    litter.write_text("from pathlib import Path\nPath('.toolcache').mkdir(exist_ok=True)\n"
                      "Path('.toolcache/out.bin').write_text('x')\n")
    code, record = record_of(repo, ("littering", f"{sys.executable} {litter}"))
    assert (code, record["drift"]) == (2, True)
    assert record["gates"][0]["status"] == "PASS"
    assert record["drift_paths"] == [".toolcache/out.bin"]


def test_a_clean_run_records_no_drift_paths(repo):
    _, record = record_of(repo, ("unit", PASS_GATE))
    assert record["drift"] is False
    assert "drift_paths" not in record, "a path list on a clean run would imply drift there was none"


def test_record_written_inside_the_repository_does_not_invalidate_itself(repo):
    evidence = repo / ".verification" / "latest.json"
    code, record = record_of(repo, ("unit", PASS_GATE), output=evidence)
    assert (code, record["verdict"]) == (0, "PASS")
    assert run(repo, "check", str(evidence))[0] == 0


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("run", "--gate", "no-equals-sign"), id="malformed-gate"),
        pytest.param(("check", "/nonexistent-record.json"), id="missing-record"),
    ],
)
def test_unusable_invocation_never_reports_a_verdict(repo, args):
    """A usage error must not land on 0 or on the FAIL code a real gate failure uses."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert result.returncode not in (0, 1)


def test_a_directory_without_history_cannot_be_verified(tmp_path):
    code, _ = run(tmp_path, "run", "--gate", f"unit={PASS_GATE}")
    assert code == 2, "with no repository there is no state to bind a result to"


def test_gates_come_from_the_repository_not_from_this_tool(repo):
    """One installed copy serves every project, so the gate set is the project's."""
    (repo / "verification.toml").write_text(
        f'required_gates = ["unit"]\njudgment_rungs = []\n\n[gates.local]\nunit = "{sys.executable} -c pass"\n'
    )
    code, stdout = run(repo, "run")
    record = json.loads(stdout)
    assert code == 0
    assert record["gate_set"] == "policy"
    assert [gate["name"] for gate in record["gates"]] == ["unit"]


def test_a_repository_with_no_policy_is_blocked_not_verified(repo):
    """Silence is not consent: an unconfigured project must never read as passing."""
    (repo / "verification.toml").unlink()
    code, stdout = run(repo, "run")
    assert code == 2
    assert stdout.strip() == "", "no record is written for a state nothing was declared about"


def test_the_onboarding_message_names_what_is_missing(repo):
    (repo / "verification.toml").unlink()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "run"], capture_output=True, text=True, check=False
    )
    assert "no verification policy found" in result.stderr
    assert "verification.toml" in result.stderr


def test_a_broken_policy_is_reported_as_broken_not_as_missing(repo):
    """Otherwise the agent onboards a project that is already onboarded."""
    (repo / "verification.toml").unlink()
    (repo / "verification.toml").write_text('required_gates = ["unclosed\n')
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "run"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "does not parse" in result.stderr
    assert "no verification policy found" not in result.stderr


def test_a_python_project_may_keep_its_policy_in_pyproject(repo):
    (repo / "verification.toml").unlink()  # verification.toml wins; this tests the fallback
    (repo / "pyproject.toml").write_text(
        "[tool.verification]\n"
        'required_gates = ["unit"]\n\n'
        "[tool.verification.gates.local]\n"
        f'unit = "{sys.executable} -c pass"\n'
    )
    assert verify.policy(repo)["required_gates"] == ["unit"]
    assert verify.local_gates(verify.policy(repo)) == [("unit", f"{sys.executable} -c pass")]


def test_verification_toml_wins_over_pyproject(repo):
    """One repository, one answer to what verified means."""
    (repo / "pyproject.toml").write_text('[tool.verification]\nrequired_gates = ["from-pyproject"]\n')
    (repo / "verification.toml").write_text('required_gates = ["from-verification-toml"]\n')
    assert verify.policy(repo)["required_gates"] == ["from-verification-toml"]


def test_a_custom_gate_cannot_license_a_record_from_an_ungoverned_repository(repo):
    """`--gate` is a diagnostic convenience, not a way past the onboarding refusal."""
    (repo / "verification.toml").unlink()
    code, _ = run(repo, "run", "--gate", f"anything={PASS_GATE}")
    assert code == 2, "an unconfigured repository is BLOCKED, whatever gates were named"


def test_the_evidence_directory_does_not_perturb_the_state_it_describes(repo):
    """Two commands, each writing into .verification/, must agree on one state.

    Each used to exclude only the file it was handed, so an attestation written
    first made the later run see an extra untracked file and compute a different
    state id - for one unchanged commit. A consumer's .gitignore hid it, which
    made correctness depend on onboarding being remembered.
    """
    assert not (repo / ".gitignore").exists(), "the point is that this works without one"
    run(repo, "attest", "--rung", "diff", "--note", "read it")
    _, record = record_of(repo, ("unit", PASS_GATE), output=repo / ".verification" / "local.json")
    attested = json.loads((repo / ".verification" / "attestations.json").read_text())
    assert record["repository"]["state_id"] == attested["repository"]["state_id"]
    assert record["drift"] is False
