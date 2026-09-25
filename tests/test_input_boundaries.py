"""What the verifier accepts from disk and from the worktree, at the edges.

Four defects, each reproduced against the CLI on main before it was fixed:

* Valid JSON that is not an object reached `.get` and ended in a traceback.
* The evidence directory was excluded by the files it held when a command
  started, so a file a gate created there read as repository drift.
* A symlink inside an untracked nested repository was followed, folding bytes
  from outside the repository into its state id.
* A missing baseline was reported without saying where it had been looked for.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"

POLICY = '''required_gates = ["unit"]
judgment_rungs = ["diff"]

[gates.local]
unit = "true"
'''


def cli(repo, *args):
    done = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *args],
                          capture_output=True, text=True, check=False)
    return done.returncode, done.stdout + done.stderr


def make_repo(root, policy=POLICY, gitignore=".verification/\n"):
    root.mkdir()
    if gitignore is not None:
        (root / ".gitignore").write_text(gitignore)
    (root / "verification.toml").write_text(policy)
    (root / "src.txt").write_text("src\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
                    "commit", "-qm", "base"], cwd=root, check=True)
    return root


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


# --------------------------------------------------------------------------
# Valid JSON of the wrong shape is refused, not a traceback.
# --------------------------------------------------------------------------

WRONG_SHAPES = [pytest.param("[1]", "list", id="a-list"),
                pytest.param('"evidence"', "str", id="a-string"),
                pytest.param("5", "int", id="a-number"),
                pytest.param("null", "NoneType", id="null")]


@pytest.mark.parametrize(("text", "kind"), WRONG_SHAPES)
@pytest.mark.parametrize("command", ["check", "compose"])
def test_a_record_that_is_not_an_object_is_refused(repo, text, kind, command):
    record = repo / ".verification" / "bad.json"
    record.parent.mkdir()
    record.write_text(text)
    code, output = cli(repo, command, str(record))
    assert "Traceback" not in output, output
    assert code == 2, output
    assert f"holds a JSON {kind}, not an object" in output


@pytest.mark.parametrize(("text", "kind"), WRONG_SHAPES)
@pytest.mark.parametrize("which", ["run", "job"])
def test_a_ci_payload_that_is_not_an_object_is_refused(repo, tmp_path, text, kind, which):
    payloads = {"run": tmp_path / "run.json", "job": tmp_path / "job.json"}
    for name, path in payloads.items():
        path.write_text(text if name == which else "{}")
    code, output = cli(repo, "import-ci", "--run", str(payloads["run"]), "--job", str(payloads["job"]))
    assert "Traceback" not in output, output
    assert code == 2, output
    assert f"{payloads[which].name}: holds a JSON {kind}, not an object" in output


# --------------------------------------------------------------------------
# The evidence directory is excluded whole, including what a gate writes there.
# --------------------------------------------------------------------------

WRITES_ARTIFACT = POLICY.replace(
    'unit = "true"', '''unit = "sh -c 'mkdir -p .verification/artifacts && echo x > .verification/artifacts/new.json'"''')
WRITES_SOURCE = POLICY.replace('unit = "true"', '''unit = "sh -c 'echo x > generated.txt'"''')


def test_an_artifact_a_gate_writes_into_the_evidence_directory_is_not_drift(tmp_path):
    """The exclusion was the files the directory held when `run` started.

    A gate writing an artifact there - where evidence/3 tells behavioural gates to
    put them - produced a file that list did not contain, and the run read it as
    the gate moving the repository. Only a `.gitignore` entry hid it, and the
    exclusion exists precisely so correctness does not depend on one.
    """
    root = make_repo(tmp_path / "repo", WRITES_ARTIFACT, gitignore=None)
    (root / ".verification").mkdir()
    (root / ".verification" / "seed.txt").write_text("present before the run\n")
    code, output = cli(root, "run", "--output", str(root / ".verification" / "local.json"))
    assert (root / ".verification" / "artifacts" / "new.json").exists(), "premise: the gate wrote it"
    assert code == 0, output
    assert "drift" not in output, output
    assert json.loads((root / ".verification" / "local.json").read_text())["drift"] is False


def test_a_gate_writing_outside_the_evidence_directory_is_still_drift(tmp_path):
    """The other side: excluding by prefix must not stop drift being seen at all."""
    root = make_repo(tmp_path / "repo", WRITES_SOURCE, gitignore=None)
    code, output = cli(root, "run", "--output", str(root / ".verification" / "local.json"))
    assert code == 2, output
    assert "moved  generated.txt" in output
