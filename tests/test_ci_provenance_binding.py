"""CI evidence from the importer's input to the completion predicate.

The unit tests for `ci_provenance_status` were already thorough, and the chain
still broke — because the importer never fed it the job's own commit. It stored
the run's `head_sha` and nothing else, so the composition path rebuilt the job
from that same value and `job.head_sha != run.head_sha` compared a value with
itself. A function cannot be tested into correctness while its caller supplies
the answer.

So these drive the real commands end to end: payloads in, `import-ci`, then
`compose`, under both authorities. The forged payload below is the one from the
original reproduction - an unrelated repository, an unrelated workflow, an
unrelated job, and a job that ran a different commit - which satisfied a CI-only
required gate and reported READY FOR HUMAN GATE TRUE.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
AUTHORITY = {"schema": "verification.ladder.authority/1",
             "authority": "verification.ladder.evidence/3"}
OTHER_COMMIT = "0" * 40

POLICY = '''required_gates = ["ci-only"]
judgment_rungs = []
ci_head_events = ["push"]

[ci_steps]
ci-only = "Run the real suite"

[ci]
repository = "acme/widget"
workflow = ".github/workflows/ci.yml"
job = "validate"
'''


def cli(repo, *args):
    done = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *args],
                          capture_output=True, text=True, check=False)
    return done.returncode, done.stdout + done.stderr


@pytest.fixture
def project(tmp_path, request):
    v3 = getattr(request, "param", False)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text(".verification/\n")
    (repo / "verification.toml").write_text(POLICY)
    (repo / "src.txt").write_text("src\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
                    "commit", "-qm", "base"], cwd=repo, check=True)
    (repo / ".verification").mkdir(exist_ok=True)
    if v3:
        (repo / ".verification" / "authority.json").write_text(json.dumps(AUTHORITY))
    assert cli(repo, "baseline")[0] == 0
    return repo


def head(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


def payloads(repo, **over):
    """A complete, internally consistent Actions run and job, overridable per field."""
    sha = head(repo)
    fields = {"repository": "acme/widget", "workflow": ".github/workflows/ci.yml",
              "job": "validate", "run_id": 4242, "job_run_id": 4242, "run_attempt": 1,
              "job_run_attempt": 1, "head_sha": sha, "job_head_sha": sha,
              "conclusion": "success", "event": "push", **over}
    directory = repo / ".verification" / "import"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.json").write_text(json.dumps({
        "id": fields["run_id"], "head_sha": fields["head_sha"], "event": fields["event"],
        "run_attempt": fields["run_attempt"], "path": fields["workflow"], "conclusion": "success",
        "head_branch": "main", "repository": {"full_name": fields["repository"]}}))
    (directory / "job.json").write_text(json.dumps({
        "id": 99, "run_id": fields["job_run_id"], "run_attempt": fields["job_run_attempt"],
        "head_sha": fields["job_head_sha"], "name": fields["job"],
        "steps": [{"name": "Run the real suite", "conclusion": fields["conclusion"]}]}))
    return directory / "run.json", directory / "job.json"


def import_ci(repo, **over):
    run_path, job_path = payloads(repo, **over)
    target = repo / ".verification" / "ci.json"
    target.unlink(missing_ok=True)
    code, output = cli(repo, "import-ci", "--run", str(run_path), "--job", str(job_path),
                       "--output", str(target))
    return code, output, target


def record_of(repo):
    return json.loads((repo / ".verification" / "ci.json").read_text())


def rewrite_source(repo, **over):
    """Edit a record that already reached disk, which the import gate cannot see."""
    path = repo / ".verification" / "ci.json"
    record = json.loads(path.read_text())
    for key, value in over.items():
        if value is None:
            record["source"].pop(key, None)
        else:
            record["source"][key] = value
    path.write_text(json.dumps(record, indent=2))
    return path


# --------------------------------------------------------------------------
# The importer refuses before it writes.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("project", [False, True], ids=["evidence2", "evidence3"], indirect=True)
@pytest.mark.parametrize(("over", "because"), [
    pytest.param({"job_head_sha": OTHER_COMMIT}, "job ran 000000000000", id="job-ran-another-commit"),
    pytest.param({"repository": "attacker/unrelated"}, "run belongs to", id="another-repository"),
    pytest.param({"workflow": ".github/workflows/nightly.yml"}, "the gate rests on", id="another-workflow"),
    pytest.param({"job": "some-other-job"}, "the gate rests on", id="another-job"),
    pytest.param({"job_run_id": 9999}, "a job from another run", id="job-from-another-run"),
    pytest.param({"job_run_attempt": 2}, "is from attempt 2", id="another-attempt"),
])
def test_a_broken_chain_is_refused_before_any_record_exists(project, over, because):
    code, output, target = import_ci(project, **over)
    assert code == 2, output
    assert because in output, output
    assert not target.exists(), "a refused import must leave no evidence behind"


@pytest.mark.parametrize("project", [False, True], ids=["evidence2", "evidence3"], indirect=True)
def test_a_policy_with_no_declared_authority_cannot_import(project):
    """Optional expectations meant the links were simply not checked."""
    (project / "verification.toml").write_text(POLICY.split("[ci]")[0])
    code, output, target = import_ci(project)
    assert code == 2, output
    assert "declares repository, workflow, job nowhere" in output
    assert "[ci]" in output, "the refusal should show what to add"
    assert not target.exists()


@pytest.mark.parametrize("project", [False, True], ids=["evidence2", "evidence3"], indirect=True)
def test_the_importer_preserves_the_jobs_own_commit(project):
    """Wherever the evidence/3 source lives for this authority.

    Under v3 it is the record itself. Under v2 the authoritative record keeps a
    deliberately minimal source and the full one is in the shadow beside it -
    either way the job's own commit has to survive the import, because that is
    the field the chain is re-derived from.
    """
    assert import_ci(project)[0] == 0
    record = record_of(project)
    if record["schema"].endswith("/2"):
        record = json.loads((project / ".verification" / "shadow" / "ci.v3.json").read_text())
    source = record["source"]
    assert "job_head_sha" in source, (
        "without this the composition path rebuilds the job from the run's commit")
    assert source["job_head_sha"] == head(project)
    assert source["head_sha"] == head(project)


# --------------------------------------------------------------------------
# Composition re-derives the chain from what the record preserved.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("project", [True], ids=["evidence3"], indirect=True)
def test_a_record_edited_after_import_is_still_refused(project):
    """Defence in depth: the import gate cannot see a record changed afterwards."""
    assert import_ci(project)[0] == 0
    rewrite_source(project, job_head_sha=OTHER_COMMIT)
    code, output = cli(project, "compose", str(project / ".verification" / "ci.json"))
    assert code != 0
    assert "job ran 000000000000" in output
    assert "READY FOR HUMAN GATE     FALSE" in output


@pytest.mark.parametrize("project", [True], ids=["evidence3"], indirect=True)
def test_a_record_predating_job_commit_binding_is_refused(project):
    """The shape every record written by an earlier verifier has.

    It must not read as agreeing: the field it needs is absent, and absent is
    not evidence of a match.
    """
    assert import_ci(project)[0] == 0
    rewrite_source(project, job_head_sha=None)
    code, output = cli(project, "compose", str(project / ".verification" / "ci.json"))
    assert code != 0
    assert "predates job-commit binding" in output
    assert "READY FOR HUMAN GATE     FALSE" in output


@pytest.mark.parametrize("project", [True], ids=["evidence3"], indirect=True)
def test_a_complete_chain_composes(project):
    """The control: every refusal above must be about the chain, not about the setup."""
    assert import_ci(project)[0] == 0
    code, output = cli(project, "compose", str(project / ".verification" / "ci.json"))
    assert code == 0, output
    assert "READY FOR HUMAN GATE     TRUE" in output


@pytest.mark.parametrize("project", [True], ids=["evidence3"], indirect=True)
def test_widening_the_accepted_authority_is_a_definition_change(project):
    """Repointing [ci] at the fork the green run came from must not read as clean."""
    assert import_ci(project)[0] == 0
    assert "READY FOR HUMAN GATE     TRUE" in cli(project, "compose",
                                                  str(project / ".verification" / "ci.json"))[1]
    (project / "verification.toml").write_text(POLICY.replace("acme/widget", "evil/fork"))
    code, output = cli(project, "compose", str(project / ".verification" / "ci.json"))
    assert "DEFINITION CHANGE PENDING" in output, output
    assert "completion contract changed" in output
    assert code != 0 or "CLEAN CLIMB              FALSE" in output
