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


# --------------------------------------------------------------------------
# Symlinks are never followed, however deep in an untracked directory.
# --------------------------------------------------------------------------

def _state(root):
    import importlib.util
    spec = importlib.util.spec_from_file_location("ladder_verify_boundaries", SCRIPT)
    verify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify)
    return verify.state_id(root)


def test_a_symlink_inside_an_untracked_nested_repository_is_not_followed(tmp_path):
    """Only the top level refused to follow links.

    `git status` reports an untracked nested repository as one directory entry,
    and hashing that directory followed the links inside it, so bytes outside the
    repository decided its state id. A link's identity is where it points.
    """
    root = make_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    (outside / "file.txt").write_text("one\n")
    (outside / "dir" / "inner.txt").write_text("one\n")
    nested = root / "nested"
    nested.mkdir()
    subprocess.run(["git", "init", "-q", str(nested)], check=True)
    (nested / "real.txt").write_text("real\n")
    (nested / "to-file").symlink_to(outside / "file.txt")
    (nested / "to-dir").symlink_to(outside / "dir")
    assert "?? nested/" in subprocess.run(["git", "status", "--porcelain"], cwd=root,
                                          capture_output=True, text=True, check=True).stdout, \
        "premise: git reports the nested repository as a single directory entry"

    before = _state(root)
    (outside / "file.txt").write_text("two\n")
    (outside / "dir" / "inner.txt").write_text("two\n")
    assert _state(root) == before, "bytes outside the repository moved its state id"

    # Narrower is not blind: what the repository does hold still binds.
    (nested / "to-file").unlink()
    (nested / "to-file").symlink_to(outside / "dir")
    retargeted = _state(root)
    assert retargeted != before, "a link that points somewhere else is a different state"
    (nested / "real.txt").write_text("changed\n")
    assert _state(root) != retargeted, "a real file inside the nested repository still binds"


# --------------------------------------------------------------------------
# A missing baseline is reported where compose actually looked.
# --------------------------------------------------------------------------

def _activated(root):
    """An evidence/3 repository: adopted in the policy, activated in the checkout."""
    (root / "verification.toml").write_text('evidence = "verification.ladder.evidence/3"\n' + POLICY)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
                    "commit", "-qam", "adopt"], cwd=root, check=True)
    (root / ".verification").mkdir(exist_ok=True)
    (root / ".verification" / "authority.json").write_text(json.dumps(
        {"schema": "verification.ladder.authority/1", "authority": "verification.ladder.evidence/3"}))
    assert cli(root, "baseline")[0] == 0
    record = root / ".verification" / "local.json"
    code, output = cli(root, "run", "--output", str(record))
    assert code == 0 and json.loads(record.read_text())["schema"] == "verification.ladder.evidence/3", output
    return record


def test_compose_names_the_baseline_it_looked_for(repo, tmp_path):
    """Read beside the FIRST record, and said to be missing without saying where.

    With the first record kept outside the evidence directory, the report sent
    the reader to recreate a baseline that sat beside the other records.
    """
    record = _activated(repo)
    elsewhere = tmp_path / "copies"
    elsewhere.mkdir()
    (elsewhere / "local.json").write_text(record.read_text())

    code, output = cli(repo, "compose", str(elsewhere / "local.json"), str(record))
    assert code == 2, output
    assert f"no baseline at {elsewhere / 'shadow' / 'baseline.v3.json'}" in output, output
    assert f"One exists beside another record given ({repo / '.verification' / 'shadow' / 'baseline.v3.json'})" in output

    code, output = cli(repo, "compose", str(elsewhere / "local.json"))
    assert code == 2, output
    assert f"no baseline at {elsewhere / 'shadow' / 'baseline.v3.json'}" in output
    assert "One exists beside" not in output, "nothing to point at when no given record has one"
