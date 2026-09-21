"""§M4: evidence/3 becomes the authoritative completion path after activation."""

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
ci_head_events = ["push"]

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


def make_repo(tmp_path):
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


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def cli(repo, *args):
    r = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *args],
                       capture_output=True, text=True, check=False)
    return r.returncode, r.stdout + r.stderr


def evidence(repo, name):
    return repo / ".verification" / f"{name}.json"


def head(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


def import_ci(repo, name="ci", *, conclusion="success"):
    directory = repo / ".verification" / "import"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}-run.json").write_text(json.dumps({
        "id": 42, "run_attempt": 1, "head_sha": head(repo), "event": "push", "head_branch": "main",
        "conclusion": "success", "path": ".github/workflows/ci.yml",
        "repository": {"full_name": "acme/widget"}}))
    (directory / f"{name}-job.json").write_text(json.dumps({
        "id": 7, "run_id": 42, "run_attempt": 1, "head_sha": head(repo), "name": "validate",
        "steps": [{"name": "Run the unit suite", "conclusion": conclusion}]}))
    code, output = cli(repo, "import-ci", "--run", str(directory / f"{name}-run.json"),
                       "--job", str(directory / f"{name}-job.json"), "--output", str(evidence(repo, name)))
    assert code == 0, output
    return evidence(repo, name)


def preactivation(repo):
    assert cli(repo, "baseline")[0] == 0
    assert cli(repo, "run", "--output", str(evidence(repo, "local")))[0] == 0
    import_ci(repo)
    assert cli(repo, "attest", "--rung", "verify-login", "--note", "watched it",
               "--execution", str(evidence(repo, "local")))[0] == 0
    assert cli(repo, "attest", "--rung", "diff", "--note", "read every hunk")[0] == 0
    return [evidence(repo, n) for n in ("local", "ci", "attestations")]


def activate(repo):
    paths = preactivation(repo)
    code, output = cli(repo, "activate", *map(str, paths))
    assert code == 0, output
    assert "AUTHORITY SWITCH        TRUE" in output
    return paths


def postactivation(repo):
    assert cli(repo, "run", "--output", str(evidence(repo, "local-v3")))[0] == 0
    import_ci(repo, "ci-v3")
    assert cli(repo, "attest", "--rung", "verify-login", "--note", "watched it",
               "--execution", str(evidence(repo, "local-v3")),
               "--output", str(evidence(repo, "attest-v3")))[0] == 0
    assert cli(repo, "attest", "--rung", "diff", "--note", "read every hunk",
               "--output", str(evidence(repo, "attest-v3")))[0] == 0
    return [evidence(repo, n) for n in ("local-v3", "ci-v3", "attest-v3")]


def test_activation_switches_compose_to_authoritative_evidence_3(repo):
    activate(repo)
    paths = postactivation(repo)
    assert {json.loads(path.read_text())["schema"] for path in paths} == {"verification.ladder.evidence/3"}
    code, output = cli(repo, "compose", *map(str, paths))
    assert code == 0, output
    assert "STATE MATCH              TRUE" in output
    assert "EVIDENCE ADMISSIBLE      TRUE" in output
    assert "CLEAN CLIMB              TRUE" in output
    assert "READY FOR HUMAN GATE     TRUE" in output


def test_activation_requires_a_governing_baseline(repo):
    assert cli(repo, "run", "--output", str(evidence(repo, "local")))[0] == 0
    code, output = cli(repo, "activate", str(evidence(repo, "local")))
    assert code == 2
    assert "GOVERNING BASELINE MISSING" in output
    assert not (repo / ".verification" / "authority.json").exists()


def test_activation_refuses_an_uncovered_required_gate(repo):
    assert cli(repo, "baseline")[0] == 0
    assert cli(repo, "run", "--gate", "unit=true", "--output", str(evidence(repo, "local")))[0] == 0
    code, output = cli(repo, "activate", str(evidence(repo, "local")))
    assert code == 2
    assert "verify-login: UNCOVERED" in output
    assert not (repo / ".verification" / "authority.json").exists()


def test_post_activation_v2_records_are_migration_diagnostics(repo):
    old_paths = activate(repo)
    code, output = cli(repo, "compose", *map(str, old_paths))
    assert code == 2
    assert "evidence/2 records are migration diagnostics only" in output
    assert "Re-run `verify.py run`" in output


def test_definition_change_reaches_the_human_gate_as_a_definition_change(repo):
    activate(repo)
    (repo / "docs" / "journey.md").write_text("# Login\n\nSuccess, failure, logout, lockout.\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.invalid", "-c", "user.name=a", "commit", "-qm", "definition"],
                   cwd=repo, check=True, env={**os.environ, **ENV})
    paths = postactivation(repo)
    code, output = cli(repo, "compose", *map(str, paths))
    assert code == 0, output
    assert "DEFINITION CHANGE        PENDING" in output
    assert "CLEAN CLIMB              FALSE" in output
    assert "READY FOR HUMAN GATE     TRUE" in output


def test_the_combined_forgery_is_rejected_after_activation(repo):
    activate(repo)
    assert cli(repo, "run", "--gate", "unit=true", "--gate", "verify-login=true",
               "--output", str(evidence(repo, "local-v3")))[0] == 0
    assert cli(repo, "attest", "--rung", "diff", "--note", "did not read it",
               "--output", str(evidence(repo, "attest-v3")))[0] == 0
    code, output = cli(repo, "compose", str(evidence(repo, "local-v3")), str(evidence(repo, "attest-v3")))
    assert code == 1
    assert "no admissible execution evidence" in output
    assert "READY FOR HUMAN GATE     FALSE" in output
