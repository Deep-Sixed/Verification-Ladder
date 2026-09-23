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
import tomllib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
SCHEMA_V3 = "verification.ladder.evidence/3"
AUTHORITY = {"schema": "verification.ladder.authority/1", "authority": SCHEMA_V3}
# A v3 project declares the contract it adopted as well as carrying an
# activation record. PREPENDED wherever it is used: POLICY ends inside a [ci]
# table, so a key added after it parses as ci.evidence and declares nothing.
ADOPTION = f'evidence = "{SCHEMA_V3}"\n'
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
    (repo / "verification.toml").write_text((ADOPTION if v3 else "") + POLICY)
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

@pytest.mark.parametrize("project", [True, False], ids=["evidence3", "evidence2"], indirect=True)
def test_the_fixture_declares_the_contract_it_carries(project):
    """The fixture's own shape, asserted rather than assumed.

    An activation record alone used to be a complete evidence/3 repository. It
    is not: the contract is declared in the committed policy too, so it survives
    evidence cleanup and a fresh clone. This fails if the declaration ever stops
    reaching the policy - including by being appended into the trailing [ci]
    table, where it parses as a CI expectation and declares nothing at all.
    """
    policy = (project / "verification.toml").read_text()
    activated = (project / ".verification" / "authority.json").exists()
    assert tomllib.loads(policy).get("evidence") == (SCHEMA_V3 if activated else None)
    if activated:
        assert policy.startswith(ADOPTION), "a key after a table header belongs to that table"


@pytest.mark.parametrize("project", [False, True], ids=["evidence2", "evidence3"], indirect=True)
@pytest.mark.parametrize(("over", "because"), [
    pytest.param({"job_head_sha": OTHER_COMMIT}, "job ran 000000000000", id="job-ran-another-commit"),
    pytest.param({"repository": "attacker/unrelated"}, "run belongs to", id="another-repository"),
    pytest.param({"workflow": ".github/workflows/nightly.yml"}, "the gate rests on", id="another-workflow"),
    pytest.param({"job": "some-other-job"}, "the gate rests on", id="another-job"),
    pytest.param({"job_run_id": 9999}, "a job from another run", id="job-from-another-run"),
    pytest.param({"job_run_attempt": 2}, "is from attempt 2", id="another-attempt"),
    pytest.param({"job_run_attempt": None}, "cannot bind job to run attempt", id="no-job-attempt"),
])
def test_a_broken_chain_is_refused_before_any_record_exists(project, over, because):
    code, output, target = import_ci(project, **over)
    assert code == 2, output
    assert because in output, output
    assert not target.exists(), "a refused import must leave no evidence behind"


@pytest.mark.parametrize("project", [False, True], ids=["evidence2", "evidence3"], indirect=True)
def test_a_policy_with_no_declared_authority_cannot_import(project):
    """Optional expectations meant the links were simply not checked."""
    # Conditional, like the fixture: writing ADOPTION for both arms turns the
    # nominal evidence2 case into an adopted-but-not-activated one once the
    # v3-adoption package lands, and this case stops demonstrating that an
    # ordinary default project with no CI identities is refused.
    adopted = (project / ".verification" / "authority.json").exists()
    (project / "verification.toml").write_text(
        (ADOPTION if adopted else "") + POLICY.split("[ci]")[0])
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


@pytest.mark.parametrize(("field", "because"), [
    pytest.param("job_head_sha", "predates job-commit binding", id="commit"),
    pytest.param("job_run_attempt", "cannot bind job to run attempt", id="attempt"),
])
@pytest.mark.parametrize("project", [True], ids=["evidence3"], indirect=True)
def test_a_record_missing_a_job_side_identity_is_refused(project, field, because):
    """Absent is not evidence of a match, for either job-side identity.

    Both were read from the run's copy of the same thing. `job_head_sha` was
    never stored at all; `job_run_attempt` was stored but read as
    `get(k, source["run_attempt"])`, so deleting it from a record substituted
    the run's attempt and the link compared a value with itself. Worse than the
    commit case: `ci_provenance_status` refuses a None attempt on its own, and
    the fallback manufactured a value before it could.
    """
    assert import_ci(project)[0] == 0
    assert rewrite_source(project, **{field: None})
    code, output = cli(project, "compose", str(project / ".verification" / "ci.json"))
    assert code != 0, output
    assert because in output, output
    assert "READY FOR HUMAN GATE     FALSE" in output


@pytest.mark.parametrize("project", [True], ids=["evidence3"], indirect=True)
def test_no_job_side_identity_is_read_from_the_run(project):
    """The control for both: neither field may be defaulted from its run twin.

    Asserted on the record rather than on a verdict, because a reader that
    substitutes the run's value produces a record that passes every behavioural
    check for the wrong reason.
    """
    assert import_ci(project)[0] == 0
    source = record_of(project)["source"]
    for job_side, run_side in (("job_head_sha", "head_sha"), ("job_run_attempt", "run_attempt")):
        assert job_side in source, f"{job_side} must be preserved, not re-derived from {run_side}"
    reader = (Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin"
              / "verify.py").read_text()
    assert 'source.get("job_run_attempt", source.get("run_attempt"))' not in reader
    assert 'source.get("job_head_sha", source.get("head_sha"))' not in reader


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
    (project / "verification.toml").write_text(
        ADOPTION + POLICY.replace("acme/widget", "evil/fork"))
    code, output = cli(project, "compose", str(project / ".verification" / "ci.json"))
    assert "DEFINITION CHANGE PENDING" in output, output
    assert "completion contract changed" in output
    assert code != 0 or "CLEAN CLIMB              FALSE" in output


# --------------------------------------------------------------------------
# `check` re-binds a CI record over what it binds, not over the whole target.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("project", [True], ids=["evidence3"], indirect=True)
def test_check_rebinds_a_ci_record_over_the_dimensions_it_binds(project):
    """A CI record says nothing about the runtime, so a runtime cannot make it stale.

    `import-ci` binds the repository alone - CI establishes a clean checkout of
    one commit - while `check` compared the record against the whole current
    target. Wherever the policy declared a runtime, every CI record read STALE
    with "runtime differs", on the very commit it describes.
    """
    (project / "runtime.json").write_text('{"build": "A"}\n')
    with (project / "verification.toml").open("a") as policy:
        policy.write('\n[shadow.runtime]\nfacts_file = "runtime.json"\n')
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
                    "commit", "-qm", "declare a runtime"], cwd=project, check=True)

    code, output, target = import_ci(project)
    assert code == 0, output
    record = json.loads(target.read_text())
    assert record["schema"] == SCHEMA_V3
    assert set(record["target_state"]) == {"repository"}, "the premise: CI binds the repository alone"

    code, output = cli(project, "check", str(target))
    assert code == 0, output
    assert "differs from this record" not in output

    # Narrower is not looser: the repository still binds.
    (project / "src.txt").write_text("moved\n")
    code, output = cli(project, "check", str(target))
    assert code == 2, output
    assert "repository.worktree_state differs from this record" in output
