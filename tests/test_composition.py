"""Completion is a predicate over evidence from several authorities, bound to one state.

Local gates, CI and the agent's own judgment each establish different things.
These lock in the rules that make combining them honest: CI evidence binds to the
commit the run names, an attestation can never stand in for an execution, records
about different states do not compose, and a composite expires when the tree moves.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
_spec = importlib.util.spec_from_file_location("ladder_verify_composition", SCRIPT)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)

PASS_GATE = f"{sys.executable} -c pass"
POLICY = """
required_gates = ["lint", "container-build"]
judgment_rungs = ["diff"]
ci_head_events = ["push"]

[gates.local]
lint = "true"

[ci_steps]
container-build = "Run docker build ."

[ci]
repository = "acme/widget"
workflow = ".github/workflows/ci.yml"
job = "validate"
"""


@pytest.fixture
def repo(tmp_path):
    """A clean one-commit repository carrying a small completion policy."""
    checkout = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(checkout)], check=True)
    (checkout / "unit.py").write_text("VALUE = 1\n")
    (checkout / "verification.toml").write_text(POLICY)
    subprocess.run(["git", "add", "-A"], cwd=checkout, check=True)
    subprocess.run(
        ["git", "-c", "user.email=ci@example.invalid", "-c", "user.name=ci", "commit", "-qm", "baseline"],
        cwd=checkout,
        check=True,
    )
    return checkout


def run(repo, *args):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), *args], capture_output=True, text=True, check=False
    )
    return result.returncode, result.stdout + result.stderr


def ci_payloads(tmp_path, repo, *, conclusion="success", step="Run docker build .", sha=None,
                event="push", job_sha=None):
    """The two Actions payloads an agent fetches: the run, and the job with its steps.

    Complete, because the importer now requires the whole chain. The earlier
    fixtures carried a run with no workflow or repository and a job with nothing
    but steps, which is exactly the payload that used to satisfy a CI-only gate.
    """
    head = sha or verify.head_sha(repo)
    run_path, job_path = tmp_path / "run.json", tmp_path / "job.json"
    run_path.write_text(json.dumps({
        "id": 35472429403, "event": event, "head_sha": head, "head_branch": "main",
        "run_attempt": 1, "path": ".github/workflows/ci.yml",
        "repository": {"full_name": "acme/widget"},
        "conclusion": "success", "html_url": "https://github.invalid/run/1",
    }))
    job_path.write_text(json.dumps({
        "id": 5510, "run_id": 35472429403, "run_attempt": 1,
        "head_sha": job_sha or head, "name": "validate",
        "steps": [{"name": step, "conclusion": conclusion}]}))
    return run_path, job_path


def local_evidence(tmp_path, repo, gate="lint"):
    path = tmp_path / f"local-{gate}.json"
    code, _ = run(repo, "run", "--gate", f"{gate}={PASS_GATE}", "--output", str(path))
    assert code == 0
    return path


def ci_evidence(tmp_path, repo, **kwargs):
    run_path, job_path = ci_payloads(tmp_path, repo, **kwargs)
    path = tmp_path / "ci.json"
    run(repo, "import-ci", "--run", str(run_path), "--job", str(job_path), "--output", str(path))
    return path


def attestation(tmp_path, repo, rung="diff", note="read every hunk"):
    path = tmp_path / "attest.json"
    code, _ = run(repo, "attest", "--rung", rung, "--note", note, "--output", str(path))
    assert code == 0
    return path


def test_ci_state_id_matches_a_clean_local_checkout(repo):
    """The whole composition rests on this: CI verifies a pristine tree, so its
    state id is a function of the commit, and it meets a local record only when
    the local tree is clean at that commit."""
    head = verify.head_sha(repo)
    assert verify.clean_state_id(head) == verify.state_id(repo)
    (repo / "unit.py").write_text("VALUE = 2\n")
    assert verify.clean_state_id(head) != verify.state_id(repo), "a dirty tree is not what CI verified"


def test_import_ci_binds_to_the_commit_the_run_names(tmp_path, repo):
    code, _ = run(repo, "import-ci", *_args(ci_payloads(tmp_path, repo)), "--output", str(tmp_path / "ci.json"))
    record = json.loads((tmp_path / "ci.json").read_text())
    assert code == 0
    assert record["authority"] == "ci"
    assert record["repository"]["head"] == verify.head_sha(repo)
    assert record["repository"]["state_id"] == verify.clean_state_id(verify.head_sha(repo))
    assert record["gates"] == [{"name": "container-build", "kind": "execution",
                                "step": "Run docker build .", "status": "PASS"}]
    assert record["source"]["run_id"] == 35472429403


def test_import_ci_refuses_a_run_that_executed_a_merge_not_the_commit(tmp_path, repo):
    """A pull_request run checks out a merge of head into base. Its results are
    about that merge; stamping them with the head's state id is true only while
    the branch happens to be up to date with its base."""
    payloads = ci_payloads(tmp_path, repo, event="pull_request")
    code, output = run(repo, "import-ci", *_args(payloads))
    assert code == 2
    assert "checks out a merge" in output
    assert "Import the push run" in output


def test_import_ci_refuses_a_run_for_another_commit(tmp_path, repo):
    """A pull_request run's checkout is a synthetic merge commit; evidence must
    bind to the branch tip under review, never to a merge no one will land."""
    payloads = ci_payloads(tmp_path, repo, sha="0" * 40)
    code, output = run(repo, "import-ci", *_args(payloads))
    assert code == 2
    assert "expected" in output


@pytest.mark.parametrize("conclusion", ["skipped", "cancelled", None])
def test_a_step_that_did_not_run_is_blocked_not_passed(tmp_path, repo, conclusion):
    path = ci_evidence(tmp_path, repo, conclusion=conclusion)
    gate = json.loads(path.read_text())["gates"][0]
    assert gate["status"] == "BLOCKED"


def test_a_step_absent_from_the_job_establishes_nothing(tmp_path, repo):
    path = ci_evidence(tmp_path, repo, step="Run something else")
    gate = json.loads(path.read_text())["gates"][0]
    assert (gate["status"], gate["reason"]) == ("BLOCKED", "step absent")


def test_composition_of_all_three_authorities_completes(tmp_path, repo):
    paths = [local_evidence(tmp_path, repo), ci_evidence(tmp_path, repo), attestation(tmp_path, repo)]
    code, output = run(repo, "compose", *map(str, paths), "--output", str(tmp_path / "composite.json"))
    assert code == 0, output
    assert "READY FOR HUMAN GATE     TRUE" in output
    assert "CLEAN CLIMB              TRUE" in output
    composite = json.loads((tmp_path / "composite.json").read_text())
    assert composite["verdict"] == "COMPLETE"
    assert composite["findings"] == []
    assert {row["gate"]: row["kind"] for row in composite["rows"]} == {
        "lint": "execution", "container-build": "execution", "diff": "attestation"
    }


def test_an_attestation_cannot_satisfy_an_executable_gate(tmp_path, repo):
    """The failure this whole layer exists to prevent: an agent's belief about a
    gate reading as though the gate had run."""
    claimed = attestation(tmp_path, repo, rung="container-build", note="it would build")
    paths = [local_evidence(tmp_path, repo), claimed]
    code, output = run(repo, "compose", *map(str, paths))
    assert code == 1
    assert "container-build: attested, never executed" in output
    assert "READY FOR HUMAN GATE     FALSE" in output


def test_the_summary_no_longer_claims_to_count_findings(tmp_path, repo):
    """`UNRESOLVED FINDINGS 0` counted missing evidence, never findings anyone found."""
    paths = [local_evidence(tmp_path, repo), ci_evidence(tmp_path, repo), attestation(tmp_path, repo)]
    _, output = run(repo, "compose", *map(str, paths))
    assert "UNRESOLVED FINDINGS" not in output, "an instrument the composer cannot support"
    assert "VERIFICATION COMPLETE" not in output, "the machine reports readiness, not acceptance"
    assert "CLEAN CLIMB" in output


def test_a_required_gate_only_attested_is_counted_as_blocked(tmp_path, repo):
    """The summary counted status alone, so it read 0 beside `attested, never executed`."""
    claimed = tmp_path / "claimed.json"
    run(repo, "attest", "--rung", "container-build", "--note", "looks fine", "--output", str(claimed))
    _, output = run(repo, "compose", str(local_evidence(tmp_path, repo)), str(claimed))
    assert "container-build: attested, never executed" in output
    assert "BLOCKED REQUIRED GATES   1" in output, "two instruments must not disagree about one row"
    assert "READY FOR HUMAN GATE     FALSE" in output


def test_an_unattested_judgment_rung_blocks_completion(tmp_path, repo):
    paths = [local_evidence(tmp_path, repo), ci_evidence(tmp_path, repo)]
    code, output = run(repo, "compose", *map(str, paths))
    assert code == 1
    assert "diff: not attested for this state" in output


def test_a_required_gate_nobody_ran_is_counted_and_shown(tmp_path, repo):
    """Absence of evidence read as zero blocked gates would be the quietest way
    for a composite to overstate what it knows."""
    code, output = run(repo, "compose", str(attestation(tmp_path, repo)))
    assert code == 1
    assert "BLOCKED REQUIRED GATES   2" in output
    assert "lint               MISSING" in output
    assert "container-build    MISSING" in output


def test_records_about_different_states_do_not_compose(tmp_path, repo):
    local = local_evidence(tmp_path, repo)
    (repo / "unit.py").write_text("VALUE = 2\n")  # the repair
    later = local_evidence(tmp_path, repo, gate="tests")
    code, output = run(repo, "compose", str(local), str(later))
    assert code == 2
    assert "different states" in output


def test_a_composite_expires_when_the_tree_moves(tmp_path, repo):
    paths = [local_evidence(tmp_path, repo), ci_evidence(tmp_path, repo), attestation(tmp_path, repo)]
    assert run(repo, "compose", *map(str, paths))[0] == 0
    (repo / "unit.py").write_text("VALUE = 2\n")
    code, output = run(repo, "compose", *map(str, paths))
    assert code == 2
    assert "STATE MATCH              FALSE" in output


def test_authorities_that_disagree_block_the_gate(tmp_path, repo):
    failing = tmp_path / "local-fail.json"
    script = tmp_path / "fail.py"
    script.write_text("import sys\nsys.exit(1)\n")
    run(repo, "run", "--gate", f"container-build={sys.executable} {script}", "--output", str(failing))
    paths = [failing, ci_evidence(tmp_path, repo), local_evidence(tmp_path, repo), attestation(tmp_path, repo)]
    code, output = run(repo, "compose", *map(str, paths))
    assert code == 1
    assert "authorities disagree" in json.dumps(_rows(repo, paths))
    assert "container-build: BLOCKED" in output


def test_attestations_do_not_survive_a_repair(tmp_path, repo):
    path = attestation(tmp_path, repo, rung="diff")
    attestation_2 = tmp_path / "attest.json"
    assert json.loads(attestation_2.read_text())["gates"][0]["name"] == "diff"

    (repo / "unit.py").write_text("VALUE = 2\n")  # the repair
    code, output = run(repo, "attest", "--rung", "self-review", "--note", "looked again", "--output", str(path))
    assert code == 0
    assert "earlier attestations discarded" in output
    assert [gate["name"] for gate in json.loads(path.read_text())["gates"]] == ["self-review"]


def test_reattesting_a_rung_replaces_rather_than_duplicates(tmp_path, repo):
    path = attestation(tmp_path, repo, rung="diff", note="first pass")
    run(repo, "attest", "--rung", "diff", "--note", "second pass", "--output", str(path))
    gates = json.loads(path.read_text())["gates"]
    assert [gate["name"] for gate in gates] == ["diff"]
    assert gates[0]["note"] == "second pass"


def test_a_dotted_gate_name_is_refused_rather_than_silently_missing(tmp_path, repo):
    """`tests-3.11 = "x"` is TOML for table `tests-3` holding `11`; the gate would
    otherwise vanish into MISSING and read as nobody having run it."""
    (repo / "verification.toml").write_text(
        'required_gates = ["tests-3.11"]\njudgment_rungs = []\n\n'
        '[gates.local]\nlint = "true"\n\n[ci_steps]\ntests-3.11 = "Run tests"\n'
    )
    code, output = run(repo, "import-ci", *_args(ci_payloads(tmp_path, repo)))
    assert code == 2
    assert "is a table, not a command" in output


def test_a_repository_without_a_policy_cannot_declare_completion(tmp_path, repo):
    evidence = attestation(tmp_path, repo)
    (repo / "verification.toml").unlink()
    code, output = run(repo, "compose", str(evidence))
    assert code == 2
    assert "no verification policy found" in output


def _args(payloads):
    run_path, job_path = payloads
    return ["--run", str(run_path), "--job", str(job_path)]


def _rows(repo, paths):
    records = [json.loads(Path(p).read_text()) for p in paths]
    return verify.compose_rows(records)
