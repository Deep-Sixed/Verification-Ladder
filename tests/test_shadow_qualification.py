"""§M3: whether everything M4 would take authority over has actually run here.

A green suite is not qualification. The suite was green throughout items 1-11
while the product exercised none of that code, so these tests drive the CLI and
ask a different question from "does the predicate work": did anything reach it?

The report distinguishes four outcomes, and UNCOVERED is the one M2 could not
produce - a predicate nobody evaluated reads exactly like one that passed unless
the report keeps them apart.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
ENV = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000"}
DRIVER = """import json, pathlib, sys
out = pathlib.Path(".verification/artifacts")
out.mkdir(parents=True, exist_ok=True)
out.joinpath("journey.json").write_text(json.dumps({"ok": True}) + "\\n")
sys.exit(0)
"""
POLICY = '''required_gates = ["unit", "verify-login"]
judgment_rungs = ["diff"]

[gates.local]
unit = "true"
verify-login = "python3 drive.py"

[ci_steps]
unit = "Run the unit suite"

[shadow.ci]
repository = "acme/widget"
workflow = ".github/workflows/ci.yml"
job = "validate"

[shadow.runtime]
facts_file = ".verification/runtime.json"

[shadow.gates.behavioral.verify-login]
driver = "python3 drive.py"
target = ["repository", "runtime"]
evidence_mode = "execution+attestation"
spec_root = "docs"
spec_files = ["docs/journey.md"]
artifacts = [".verification/artifacts/journey.json"]
'''


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "drive.py").write_text(DRIVER)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "journey.md").write_text("# Login\n\nSuccess, failure, logout.\n")
    (tmp_path / ".gitignore").write_text(".verification/\n__pycache__/\n")
    (tmp_path / "verification.toml").write_text(POLICY)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.invalid", "-c", "user.name=a", "commit", "-qm", "b"],
                   cwd=tmp_path, check=True, env={**os.environ, **ENV})
    (tmp_path / ".verification").mkdir(exist_ok=True)
    (tmp_path / ".verification" / "runtime.json").write_text('{"build": "abc123"}\n')
    return tmp_path


def cli(repo, *args):
    r = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *args],
                       capture_output=True, text=True, check=False)
    return r.returncode, r.stdout + r.stderr


def evidence(repo, name):
    return repo / ".verification" / f"{name}.json"


def head(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


def import_ci(repo, **over):
    directory = repo / ".verification" / "import"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.json").write_text(json.dumps({
        "id": 42, "run_attempt": 1, "head_sha": head(repo), "event": "push", "head_branch": "main",
        "conclusion": "success", "path": over.get("workflow", ".github/workflows/ci.yml"),
        "repository": {"full_name": over.get("repository", "acme/widget")}}))
    (directory / "job.json").write_text(json.dumps({
        "id": 7, "run_id": over.get("job_run_id", 42), "run_attempt": 1, "head_sha": head(repo),
        "name": over.get("job", "validate"),
        "steps": [{"name": "Run the unit suite", "conclusion": over.get("conclusion", "success")}]}))
    cli(repo, "import-ci", "--run", str(directory / "run.json"), "--job", str(directory / "job.json"),
        "--output", str(evidence(repo, "ci")))
    return evidence(repo, "ci")


def full_workflow(repo):
    """The ordinary end-to-end climb: baseline, gates, CI, judgments."""
    assert cli(repo, "baseline")[0] == 0
    assert cli(repo, "run", "--output", str(evidence(repo, "local")))[0] == 0
    import_ci(repo)
    cli(repo, "attest", "--rung", "verify-login", "--note", "watched it",
        "--execution", str(evidence(repo, "local")))
    cli(repo, "attest", "--rung", "diff", "--note", "read every hunk")
    return [str(evidence(repo, n)) for n in ("local", "ci", "attestations")]


def qualify(repo, *paths):
    return cli(repo, "qualify", *(paths or full_workflow(repo)))


def states(output):
    found = {}
    for line in output.splitlines():
        parts = line.split()
        if parts and parts[0] in ("QUALIFIED", "DISAGREEMENT", "UNCOVERED") or line.startswith(
                "  NOT COMPARABLE "):
            state = "NOT COMPARABLE" if line.strip().startswith("NOT COMPARABLE") else parts[0]
            found[line.strip().removeprefix(state).strip().split("  ")[0]] = state
    return found


# --- the report -------------------------------------------------------------

def test_the_ordinary_end_to_end_workflow_qualifies_every_predicate(repo):
    code, output = qualify(repo)
    assert "M3 QUALIFIED             TRUE" in output, output
    assert code == 0
    assert "UNCOVERED 0" in output
    assert all(state == "QUALIFIED" for state in states(output).values()), output


def test_a_predicate_nothing_exercised_reads_uncovered(repo):
    """The outcome M2 had no way to report, and the reason this command exists."""
    cli(repo, "run", "--output", str(evidence(repo, "local")))
    code, output = qualify(repo, str(evidence(repo, "local")))
    assert code == 2
    assert states(output)["ci provenance"] == "UNCOVERED"
    assert states(output)["ci step"] == "UNCOVERED"
    assert states(output)["governance"] == "UNCOVERED"
    assert "nothing in this evidence set exercised it" in output
    assert "M3 QUALIFIED             FALSE" in output


def test_a_disagreement_is_reported_as_one_and_names_the_gate(repo):
    paths = full_workflow(repo)
    path = repo / ".verification" / "shadow" / "local.v3.json"
    record = json.loads(path.read_text())
    record["gates"][0]["definition_sha256"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(record, indent=2))
    code, output = qualify(repo, *paths)
    assert code == 1
    assert states(output)["definition"] == "DISAGREEMENT"
    assert "unit:" in output


def test_an_unpairable_record_disqualifies_rather_than_being_skipped(repo):
    paths = full_workflow(repo)
    (repo / ".verification" / "shadow" / "ci.v3.json").unlink()
    code, output = qualify(repo, *paths)
    assert code == 2
    assert "unpaired ci.json" in output
    assert "M3 QUALIFIED             FALSE" in output


def test_qualification_writes_a_report_and_changes_no_evidence(repo):
    paths = full_workflow(repo)
    before = {p: Path(p).read_bytes() for p in paths}
    composite_before = cli(repo, "compose", *paths)
    code, _ = qualify(repo, *paths, "--output", str(repo / ".verification" / "m3.json"))
    report = json.loads((repo / ".verification" / "m3.json").read_text())
    assert (code, report["qualified"]) == (0, True)
    assert {e["predicate"] for e in report["predicates"]} >= {"artifact chain", "governance", "ci step"}
    assert {p: Path(p).read_bytes() for p in paths} == before
    assert cli(repo, "compose", *paths) == composite_before, (
        "M3 status reached the authoritative composite")


def test_a_failing_qualification_leaves_the_authoritative_result_alone(repo):
    paths = full_workflow(repo)
    composite = cli(repo, "compose", *paths)
    assert (composite[0], "READY FOR HUMAN GATE     TRUE" in composite[1]) == (0, True), composite[1]
    (repo / ".verification" / "artifacts" / "journey.json").write_bytes(b"{}")
    assert qualify(repo, *paths)[0] == 1, "premise: qualification now fails"
    assert cli(repo, "compose", *paths) == composite


# --- item 11's cases, through the surfaces rather than the predicates --------

def test_a_custom_command_cannot_wear_a_required_gates_name(repo):
    """Case 1, at the CLI: the row gets no definition, because it was produced under none.

    Control first. `--gate unit=true` runs exactly what the policy declares for
    `unit`, so it forges nothing and must be admissible - a probe whose command
    happens to match establishes nothing by disagreeing or by agreeing.
    """
    cli(repo, "run", "--gate", "unit=true", "--output", str(evidence(repo, "local")))
    honest = cli(repo, "compare", str(evidence(repo, "local")))[1]
    assert "AGREE           unit             definition" in honest, honest

    cli(repo, "run", "--gate", "unit=echo nothing ran", "--output", str(evidence(repo, "local")))
    code, output = cli(repo, "compare", str(evidence(repo, "local")))
    assert code in (1, 2)
    assert "an invocation the policy does not declare for it" in output
    assert "'echo nothing ran'" in output


def test_an_execution_cannot_wear_a_judgment_rungs_name(repo):
    """Case 3, at the CLI: `--gate diff=true` was a passing judgment rung."""
    cli(repo, "run", "--gate", "diff=true", "--output", str(evidence(repo, "local")))
    code, output = cli(repo, "compare", str(evidence(repo, "local")))
    assert "not that anyone read it" in output
    assert code in (1, 2)


def test_a_declared_specification_change_moves_the_gates_identity(repo):
    """Case 11: evidence produced before a declared spec file changed."""
    cli(repo, "baseline")
    cli(repo, "run", "--output", str(evidence(repo, "local")))
    assert "DISAGREE 0" in cli(repo, "compare", str(evidence(repo, "local")))[1], (
        "premise: nothing disagrees before the specification moves")
    (repo / "docs" / "journey.md").write_text("# Login\n\nOpen the login screen.\n")
    cli(repo, "run", "--output", str(evidence(repo, "local")))
    code, output = cli(repo, "compare", str(evidence(repo, "local")))
    assert "the definition governing this task is" in output
    assert "DEFINITION CHANGE PENDING" in output
    assert code in (1, 2)


def test_a_specification_outside_its_root_is_refused(repo):
    """Case 12, as a shadow diagnostic rather than a block."""
    (repo / "elsewhere.md").write_text("weaker\n")
    (repo / "verification.toml").write_text(
        POLICY.replace('spec_files = ["docs/journey.md"]', 'spec_files = ["elsewhere.md"]'))
    code, output = cli(repo, "run", "--output", str(evidence(repo, "local")))
    assert code == 0, "a shadow declaration cannot fail a task"
    assert "resolves outside spec_root" in output


def test_a_tampered_record_id_is_caught(repo):
    """Case 18, through the artifact chain the CLI builds."""
    paths = full_workflow(repo)
    path = repo / ".verification" / "shadow" / "local.v3.json"
    record = json.loads(path.read_text())
    row = next(g for g in record["gates"] if g["gate"] == "verify-login")
    row["record_id"] = "sha256:" + "5" * 64
    path.write_text(json.dumps(record, indent=2))
    code, output = qualify(repo, *paths)
    assert code == 1
    assert "does not match the record it identifies" in output


def test_a_candidate_definition_governs_once_it_is_the_next_tasks_baseline(repo):
    """Case 23: the change is pending until a human gate, then ordinary."""
    cli(repo, "baseline")
    (repo / "docs" / "journey.md").write_text("# Login\n\nSuccess, failure, logout, lockout.\n")
    cli(repo, "run", "--output", str(evidence(repo, "local")))
    assert "DEFINITION CHANGE PENDING" in cli(repo, "compare", str(evidence(repo, "local")))[1]
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.invalid", "-c", "user.name=a", "commit", "-qm", "n"],
                   cwd=repo, check=True, env={**os.environ, **ENV})
    cli(repo, "baseline")
    cli(repo, "run", "--output", str(evidence(repo, "local")))
    code, output = cli(repo, "compare", str(evidence(repo, "local")))
    assert "DEFINITION CHANGE PENDING" not in output, (
        "a definition that survived review must govern the next task normally")
    assert code in (0, 2)


def test_normalization_of_the_legacy_policy_form_is_deterministic(repo):
    """Case 24: two readings of one legacy file must agree on gate identity."""
    digests = []
    for _ in range(3):
        cli(repo, "run", "--output", str(evidence(repo, "local")))
        record = json.loads((repo / ".verification" / "shadow" / "local.v3.json").read_text())
        digests.append({g["gate"]: g.get("definition_sha256") for g in record["gates"]})
    assert digests[0] == digests[1] == digests[2]
    assert digests[0]["unit"], "the legacy [gates.local] string form must normalize to an identity"


def test_the_combined_forgery_is_refused_by_the_evidence_3_path(repo):
    """Every leg of the PR #1 replay, driven through the shadow surfaces."""
    cli(repo, "run", "--gate", "unit=echo nothing ran", "--gate", "verify-login=true",
        "--gate", "diff=true", "--output", str(evidence(repo, "local")))
    cli(repo, "attest", "--rung", "diff", "--note", "did not read it")
    code, output = cli(repo, "compare", str(evidence(repo, "local")),
                       "--attestations", str(evidence(repo, "attestations")))
    assert code in (1, 2)
    assert "'python3 drive.py'" not in output.split("verify-login     definition")[1][:400], (
        "the behavioural gate was satisfied by `true`; it must not keep the declared identity")
    assert output.count("an invocation the policy does not declare for it") == 2, (
        "both mechanical legs of the replay, not one")
    assert "not that anyone read it" in output
    assert "EVIDENCE/3 QUALIFIED     FALSE" in output


# --- what M3 deliberately did not resolve -----------------------------------

def test_the_v2_composer_still_accepts_the_forgery(repo):
    """Why one of the two named skips is still skipped, established by running it.

    The end-to-end refusal needs evidence/3 to be authoritative. Under v2 the
    combined replay still reports READY FOR HUMAN GATE TRUE, so the skip's
    premise holds and resolving it would mean asserting something untrue.
    """
    cli(repo, "run", "--gate", "unit=true", "--gate", "verify-login=true",
        "--output", str(evidence(repo, "local")))
    cli(repo, "attest", "--rung", "diff", "--note", "did not read it")
    code, output = cli(repo, "compose", str(evidence(repo, "local")),
                       str(evidence(repo, "attestations")))
    assert (code, "READY FOR HUMAN GATE     TRUE" in output) == (0, True), (
        "if this ever fails, the activation skip can be resolved - check before deleting it")


def test_qualification_is_not_a_findings_ledger(repo):
    """Why the other named skip is still skipped.

    Decision H's ledger tracks findings an AGENT discovered while climbing. This
    report tracks predicates the VERIFIER exercised. They are different objects,
    and M3 supplies neither the first nor a substitute for it.
    """
    paths = full_workflow(repo)
    report = repo / ".verification" / "m3.json"
    qualify(repo, *paths, "--output", str(report))
    body = json.loads(report.read_text())
    assert set(body) == {"schema", "qualified", "recorded_at", "verifier", "unpaired", "predicates"}
    assert all(set(entry) == {"predicate", "enforces", "state", "counts", "detail", "gate"}
               for entry in body["predicates"])
    assert "OPEN FINDINGS" not in cli(repo, "compose", *paths)[1]
