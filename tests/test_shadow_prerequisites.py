"""§M3: the two surfaces the governance and CI-step predicates needed to exist.

`baseline_record`, `governing_definitions`, `definition_change`,
`governance_status` and `ci_step_status` were unreachable after M2 for the same
reason: no command produced the input shape they read. M4 depends on all five,
so M3 builds the surfaces rather than qualifying around them - and builds them
without authority, which is the part these tests are mostly about.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
_spec = importlib.util.spec_from_file_location("ladder_verify_prereq", SCRIPT)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)
POLICY = '''required_gates = ["unit"]
judgment_rungs = ["diff"]

[gates.local]
unit = "true"

[ci_steps]
unit = "Run the unit suite"

[shadow.ci]
repository = "acme/widget"
workflow = ".github/workflows/ci.yml"
job = "validate"
'''
ENV = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000"}


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".verification/\n")
    (tmp_path / "verification.toml").write_text(POLICY)
    commit(tmp_path, "base")
    return tmp_path


def commit(repo, message):
    import os
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.invalid", "-c", "user.name=a", "commit", "-qm", message],
                   cwd=repo, check=True, env={**os.environ, **ENV})


def cli(repo, *args):
    r = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *args],
                       capture_output=True, text=True, check=False)
    return r.returncode, r.stdout + r.stderr


def local(repo):
    return repo / ".verification" / "local.json"


def baseline_at(repo):
    return repo / ".verification" / "shadow" / "baseline.v3.json"


def ran(repo):
    code, output = cli(repo, "run", "--output", str(local(repo)))
    assert code == 0, output


def row(repo, record, predicate):
    code, output = cli(repo, "compare", str(record))
    for index, line in enumerate(output.splitlines()):
        if predicate in line and line.split()[0] in ("AGREE", "DISAGREE", "NOT", "N/A"):
            return line.strip().split()[0] + ("" if not line.strip().startswith("NOT") else " COMPARABLE"), \
                "\n".join(output.splitlines()[index:index + 4]), code
    raise AssertionError(f"no {predicate!r} row in:\n{output}")


# --- baseline: a surface, and not a prerequisite ----------------------------

def test_a_baseline_captures_definitions_and_runs_nothing(repo):
    code, output = cli(repo, "baseline")
    assert code == 0, output
    record = json.loads(baseline_at(repo).read_text())
    assert record["schema"] == "verification.ladder.baseline/1"
    assert record["origin"] == "captured"
    assert record["gates"] == [], "a baseline captures what gates mean, not what they did"
    assert set(record["governing"]["gates"]) == {"unit"}
    assert not (repo / ".verification" / "local.json").exists()


def test_a_missing_baseline_is_not_an_error(repo):
    ran(repo)
    assert not baseline_at(repo).exists()
    outcome, detail, code = row(repo, local(repo), "governance")
    assert (outcome, code) == ("N/A", 0), detail


def test_a_baseline_is_never_a_prerequisite_for_the_authoritative_result(repo):
    ran(repo)
    cli(repo, "attest", "--rung", "diff", "--note", "read it")
    without = cli(repo, "compose", str(local(repo)), str(repo / ".verification" / "attestations.json"))
    assert (without[0], "READY FOR HUMAN GATE     TRUE" in without[1]) == (0, True), without[1]
    cli(repo, "baseline")
    with_baseline = cli(repo, "compose", str(local(repo)),
                        str(repo / ".verification" / "attestations.json"))
    assert with_baseline == without, "capturing a baseline moved the authoritative composite"


def test_a_baseline_is_not_swept_into_composition(repo):
    cli(repo, "baseline")
    ran(repo)
    code, output = cli(repo, "compose", *[str(p) for p in sorted(
        (repo / ".verification").glob("*.json"))])
    assert "baseline" not in output
    assert code in (0, 1), output


def test_the_governing_definitions_are_the_baselines_not_the_trees(repo):
    cli(repo, "baseline")
    ran(repo)
    assert row(repo, local(repo), "governance")[0] == "AGREE"
    (repo / "verification.toml").write_text(POLICY.replace('unit = "true"', 'unit = "true  # weakened"'))
    outcome, detail, _ = row(repo, local(repo), "governance")
    assert outcome == "AGREE", "a definition change is the model working, not a v3 defect"
    assert "DEFINITION CHANGE PENDING" in detail


def test_a_definition_change_makes_its_own_gate_inadmissible_and_no_other(repo):
    cli(repo, "baseline")
    ran(repo)
    # Re-declare `unit` differently; `diff` is a judgment rung and unaffected.
    (repo / "verification.toml").write_text(POLICY.replace('unit = "true"', 'unit = "true  # changed"'))
    ran(repo)
    outcome, detail, _ = row(repo, local(repo), "definition")
    assert outcome == "DISAGREE", detail
    assert "the definition governing this task is" in detail


@pytest.mark.parametrize(
    ("label", "break_it", "because"),
    [
        pytest.param("malformed", lambda p: p.write_text("not json\n"),
                     "not a readable", id="malformed"),
        pytest.param("no origin", lambda p: p.write_text(json.dumps(
            {**json.loads(p.read_text()), "origin": None})), "not a readable", id="origin"),
        pytest.param("another line of development", lambda p: p.write_text(json.dumps(
            _moved(json.loads(p.read_text())))), "not an ancestor of this HEAD", id="foreign"),
    ],
)
def test_a_baseline_that_cannot_govern_says_so(repo, label, break_it, because):
    cli(repo, "baseline")
    ran(repo)
    break_it(baseline_at(repo))
    outcome, detail, _ = row(repo, local(repo), "governance")
    assert outcome == "NOT COMPARABLE", f"{label}: {detail}"
    assert because in detail, f"{label}: {detail}"


def _moved(record):
    record["target_state"]["repository"]["head"] = "b" * 40
    return record


def test_a_reconstructed_baseline_must_come_from_a_committed_state(repo):
    (repo / "scratch.txt").write_text("uncommitted\n")
    code, output = cli(repo, "baseline", "--origin", "reconstructed")
    assert code == 2
    assert "committed state" in output
    commit(repo, "commit the scratch")
    code, output = cli(repo, "baseline", "--origin", "reconstructed")
    assert code == 0, output
    assert json.loads(baseline_at(repo).read_text())["origin"] == "reconstructed"


# --- CI step provenance ------------------------------------------------------

def payload(repo, *, conclusion="success", step="Run the unit suite", repository="acme/widget",
            workflow=".github/workflows/ci.yml", job="validate", job_run_id=42):
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                         text=True, check=True).stdout.strip()
    directory = repo / ".verification" / "import"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.json").write_text(json.dumps({
        "id": 42, "run_attempt": 1, "head_sha": sha, "event": "push", "head_branch": "main",
        "conclusion": "success", "path": workflow, "repository": {"full_name": repository}}))
    (directory / "job.json").write_text(json.dumps({
        "id": 7, "run_id": job_run_id, "run_attempt": 1, "head_sha": sha, "name": job,
        "steps": [{"name": step, "conclusion": conclusion}]}))
    code, output = cli(repo, "import-ci", "--run", str(directory / "run.json"),
                       "--job", str(directory / "job.json"),
                       "--output", str(repo / ".verification" / "ci.json"))
    assert code in (0, 1, 2), output
    return repo / ".verification" / "ci.json"


def import_attempt(repo, **over):
    """`payload`, but returning what the importer did instead of assuming it wrote."""
    target = repo / ".verification" / "ci.json"
    target.unlink(missing_ok=True)
    directory = repo / ".verification" / "import"
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                         text=True, check=True).stdout.strip()
    directory.mkdir(parents=True, exist_ok=True)
    fields = {"repository": "acme/widget", "workflow": ".github/workflows/ci.yml",
              "job": "validate", "job_run_id": 42, "job_head_sha": sha, **over}
    (directory / "run.json").write_text(json.dumps({
        "id": 42, "run_attempt": 1, "head_sha": sha, "event": "push", "head_branch": "main",
        "conclusion": "success", "path": fields["workflow"],
        "repository": {"full_name": fields["repository"]}}))
    (directory / "job.json").write_text(json.dumps({
        "id": 7, "run_id": fields["job_run_id"], "run_attempt": 1,
        "head_sha": fields["job_head_sha"], "name": fields["job"],
        "steps": [{"name": "Run the unit suite", "conclusion": "success"}]}))
    code, output = cli(repo, "import-ci", "--run", str(directory / "run.json"),
                       "--job", str(directory / "job.json"), "--output", str(target))
    return code, output, target


def test_the_shadow_keeps_the_raw_step_conclusions(repo):
    payload(repo)
    shadow = json.loads((repo / ".verification" / "shadow" / "ci.v3.json").read_text())
    assert shadow["source"]["steps"] == [{"name": "Run the unit suite", "conclusion": "success"}], (
        "reading back a derived status would only establish that it was copied correctly")


@pytest.mark.parametrize(
    ("conclusion", "expected"),
    [("success", "PASS"), ("failure", "FAIL"), ("timed_out", "FAIL"),
     ("skipped", "BLOCKED"), ("cancelled", "BLOCKED"), (None, "BLOCKED")],
)
def test_the_v3_reading_of_a_step_matches_the_payload_v2_read(repo, conclusion, expected):
    record = payload(repo, conclusion=conclusion)
    assert json.loads(record.read_text())["gates"][0]["status"] == expected
    outcome, detail, _ = row(repo, record, "ci step")
    assert outcome == "AGREE", detail


def test_a_step_the_job_never_ran_establishes_nothing(repo):
    record = payload(repo, step="Run something else entirely")
    assert json.loads(record.read_text())["gates"][0]["status"] == "BLOCKED"
    assert row(repo, record, "ci step")[0] == "AGREE"


def test_tampering_with_the_raw_conclusions_is_caught(repo):
    """The control for every AGREE above: the predicate can disagree."""
    record = payload(repo, conclusion="failure")
    path = repo / ".verification" / "shadow" / "ci.v3.json"
    shadow = json.loads(path.read_text())
    shadow["source"]["steps"][0]["conclusion"] = "success"
    path.write_text(json.dumps(shadow, indent=2))
    outcome, detail, _ = row(repo, record, "ci step")
    assert outcome == "DISAGREE", detail
    assert "do not yield the status it reports" in detail


def test_a_shadow_without_raw_steps_cannot_be_read(repo):
    record = payload(repo)
    path = repo / ".verification" / "shadow" / "ci.v3.json"
    shadow = json.loads(path.read_text())
    del shadow["source"]["steps"]
    path.write_text(json.dumps(shadow, indent=2))
    outcome, detail, _ = row(repo, record, "ci step")
    assert outcome == "NOT COMPARABLE", detail


@pytest.mark.parametrize(
    ("label", "over", "because"),
    [
        pytest.param("another repository", {"repository": "evil/fork"},
                     "the repository under verification is", id="repository"),
        pytest.param("wrong workflow", {"workflow": ".github/workflows/nightly.yml"},
                     "the gate rests on", id="workflow"),
        pytest.param("wrong job", {"job": "smoke"}, "the gate rests on", id="job"),
        pytest.param("job from another run", {"job_run_id": 99},
                     "a job from another run establishes nothing", id="run"),
    ],
)
def test_the_provenance_chain_is_refused_at_import(repo, label, over, because):
    """The chain is now checked before a record exists, not only when one is read.

    An importer that writes a PASS-shaped record and leaves the links to be
    checked at composition has already produced the artifact someone will quote.
    """
    code, output, target = import_attempt(repo, **over)
    assert code == 2, f"{label}: {output}"
    assert because in output, f"{label}: {output}"
    assert not target.exists(), f"{label}: a refused import must write no evidence"


@pytest.mark.parametrize(
    ("label", "over", "because"),
    [
        pytest.param("another repository", {"repository": "evil/fork"},
                     "the repository under verification is", id="repository"),
        pytest.param("wrong workflow", {"workflow": ".github/workflows/nightly.yml"},
                     "the gate rests on", id="workflow"),
        pytest.param("wrong job", {"job": "smoke"}, "the gate rests on", id="job"),
        pytest.param("job from another run", {"job_run_id": 99},
                     "a job from another run establishes nothing", id="run"),
        pytest.param("job ran another commit", {"job_head_sha": "0" * 40},
                     "job ran 000000000000", id="commit"),
    ],
)
def test_the_provenance_chain_is_checked_again_at_composition(repo, label, over, because):
    """Defence in depth: a record that reached disk by any route is still read.

    The import gate above cannot see a record edited afterwards, or one written
    by an older verifier. So the same chain is re-derived from what the record
    preserved - which is why `job_head_sha` has to be preserved at all.
    """
    record = payload(repo)
    path = repo / ".verification" / "shadow" / "ci.v3.json"
    shadow = json.loads(path.read_text())
    for key, value in over.items():
        shadow["source"]["job_run_id" if key == "job_run_id" else key] = value
    path.write_text(json.dumps(shadow, indent=2))
    outcome, detail, _ = row(repo, record, "ci provenance")
    assert outcome == "DISAGREE", f"{label}: {detail}"
    assert because in detail, f"{label}: {detail}"


def test_a_record_without_the_job_commit_cannot_be_bound(repo):
    """Records written before the job's own commit was preserved must not compose.

    The importer stored only the run's head_sha, and the composition path rebuilt
    the job from it - so `job.head_sha != run.head_sha` compared a value with
    itself and could never fail. A record with the field missing is refused
    rather than read as agreeing.
    """
    record = payload(repo)
    path = repo / ".verification" / "shadow" / "ci.v3.json"
    shadow = json.loads(path.read_text())
    assert "job_head_sha" in shadow["source"], "the importer must preserve it"
    del shadow["source"]["job_head_sha"]
    path.write_text(json.dumps(shadow, indent=2))
    outcome, detail, _ = row(repo, record, "ci provenance")
    assert outcome == "DISAGREE", detail
    assert "predates job-commit binding" in detail


def test_an_undeclared_expectation_is_refused_rather_than_skipped(repo):
    """The links with nothing to compare against were simply not checked.

    `[shadow.ci]` was optional, and `ci_provenance_status` skips a link whose
    expectation is absent - so a policy that declared none accepted a run from
    any repository, workflow and job. Optional meant absent in practice.
    """
    (repo / "verification.toml").write_text(POLICY.split("[shadow.ci]")[0])
    code, output, target = import_attempt(repo, workflow=".github/workflows/anything.yml",
                                          job="anything")
    assert code == 2, output
    assert "declares repository, workflow, job nowhere" in output
    assert not target.exists()


def test_the_expectation_does_not_come_from_the_payload_being_checked(repo):
    """Declared, never read from the run under examination.

    Checking a run's workflow against the workflow that same run names compares a
    value with itself. The payload below is internally consistent - run.path and
    job.name agree with each other - and is still refused, because they disagree
    with the policy.
    """
    code, output, _ = import_attempt(repo, workflow=".github/workflows/anything.yml")
    assert code == 2, output
    assert "the gate rests on" in output


def test_ci_expectations_are_governed(repo):
    """Widening the accepted authority mid-task is a definition change, not a note."""
    baseline = verify.completion_contract(verify.policy(repo))
    assert baseline["ci"] == {"repository": "acme/widget",
                              "workflow": ".github/workflows/ci.yml", "job": "validate"}
    (repo / "verification.toml").write_text(POLICY.replace("acme/widget", "evil/fork"))
    widened = verify.completion_contract(verify.policy(repo))
    assert widened["ci"]["repository"] == "evil/fork"
    assert verify.digest_of(baseline) != verify.digest_of(widened), (
        "the completion contract digest must move, or governance cannot see it")
