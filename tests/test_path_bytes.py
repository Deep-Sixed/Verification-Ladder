"""Paths are bytes. The verifier read them as UTF-8 text.

On Linux a filename or a symlink target is any byte string without NUL or "/".
`git status -z` hands those bytes back unquoted, and the verifier decoded them
strictly, so one untracked file named with a stray 0xff byte ended `run`,
`baseline` and `attest` in a UnicodeDecodeError: exit 1, which this tool uses
for FAIL, where it should have identified the state or said why it could not.

The fix reads paths the way the operating system writes them - `os.fsdecode`
in, `os.fsencode` out - which is the identity on every valid UTF-8 path, so
every state id that could be computed before is computed the same way now.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
REPO = Path(__file__).resolve().parents[1]
# The v0.3.1 release commit: the last verifier that decoded paths strictly.
STRICT = "84418d8069cee3cc9317bdcc05d1ffc34e02bfb0"

POLICY = '''required_gates = ["unit"]
judgment_rungs = ["diff"]

[gates.local]
unit = "true"
'''

BAD_NAME = os.fsdecode(b"bad\xffname")
BAD_TARGET = os.fsdecode(b"/nowhere/t\xff")


def cli(repo, *args):
    done = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *args],
                          capture_output=True, text=True, check=False)
    return done.returncode, done.stdout + done.stderr


def commit(root, message):
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
                    "commit", "-qm", message], cwd=root, check=True)


def make_repo(root, policy=POLICY):
    root.mkdir()
    (root / ".gitignore").write_text(".verification/\n")
    (root / "verification.toml").write_text(policy)
    (root / "src.txt").write_text("src\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    commit(root, "base")
    return root


def load(source: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def verify():
    return load(SCRIPT, "ladder_verify_path_bytes")


@pytest.fixture
def repo(tmp_path):
    root = make_repo(tmp_path / "repo")
    try:
        (root / BAD_NAME).write_text("probe\n")
        (root / BAD_NAME).unlink()
    except (OSError, UnicodeError):
        pytest.skip("this filesystem refuses a filename that is not valid UTF-8")
    return root


def run(root):
    record = root / ".verification" / "local.json"
    code, output = cli(root, "run", "--output", str(record))
    return code, output, record


# --------------------------------------------------------------------------
# A path that is not UTF-8 is state, not a crash.
# --------------------------------------------------------------------------

def test_an_untracked_file_named_with_invalid_utf8_is_state(repo, verify):
    (repo / BAD_NAME).write_text("one\n")
    code, output, record = run(repo)
    assert "Traceback" not in output, output
    assert code == 0, output
    assert json.loads(record.read_text())["repository"]["dirty"] is True

    before = verify.state_id(repo)
    (repo / BAD_NAME).write_text("two\n")
    assert verify.state_id(repo) != before, "the file's content must still bind"


def test_a_modified_tracked_file_named_with_invalid_utf8_is_state(repo, verify):
    (repo / BAD_NAME).write_text("one\n")
    commit(repo, "a tracked file whose name is not UTF-8")
    clean = verify.state_id(repo)
    (repo / BAD_NAME).write_text("two\n")
    code, output, _ = run(repo)
    assert "Traceback" not in output, output
    assert code == 0, output
    assert verify.state_id(repo) != clean


def test_a_symlink_whose_target_is_invalid_utf8_is_state(repo, verify):
    os.symlink(BAD_TARGET, repo / "link")
    code, output, _ = run(repo)
    assert "Traceback" not in output, output
    assert code == 0, output

    before = verify.state_id(repo)
    (repo / "link").unlink()
    os.symlink(os.fsdecode(b"/nowhere/u\xff"), repo / "link")
    assert verify.state_id(repo) != before, "where the link points is its identity"


def test_invalid_utf8_inside_an_untracked_nested_repository_is_state(repo, verify):
    """`git status` reports a nested repository as one entry; its walk had the same fault."""
    nested = repo / "nested"
    nested.mkdir()
    subprocess.run(["git", "init", "-q", str(nested)], check=True)
    (nested / BAD_NAME).write_text("one\n")
    os.symlink(BAD_TARGET, nested / "link")
    code, output, _ = run(repo)
    assert "Traceback" not in output, output
    assert code == 0, output

    before = verify.state_id(repo)
    (nested / BAD_NAME).write_text("two\n")
    assert verify.state_id(repo) != before


# --------------------------------------------------------------------------
# A gate that creates such a path is drift, reported by name.
# --------------------------------------------------------------------------

MAKES_BAD_NAME = POLICY.replace('unit = "true"', 'unit = "python3 make.py"')
MAKER = 'open(b"made\\xff", "w").write("made by the gate\\n")\n'
MADE = os.fsdecode(b"made\xff")


def _gate_repo(tmp_path, adopt=False):
    root = make_repo(tmp_path / "repo", MAKES_BAD_NAME)
    (root / "make.py").write_text(MAKER)
    if adopt:
        (root / "verification.toml").write_text('evidence = "verification.ladder.evidence/3"\n'
                                               + MAKES_BAD_NAME)
    commit(root, "a gate that writes a file whose name is not UTF-8")
    if adopt:
        (root / ".verification").mkdir()
        (root / ".verification" / "authority.json").write_text(json.dumps(
            {"schema": "verification.ladder.authority/1", "authority": "verification.ladder.evidence/3"}))
        assert cli(root, "baseline")[0] == 0
    return root


@pytest.mark.parametrize("adopt", [False, True], ids=["evidence-2", "evidence-3"])
def test_a_gate_that_creates_an_invalid_utf8_path_is_drift_named_in_the_record(tmp_path, adopt):
    root = _gate_repo(tmp_path, adopt)
    code, output, record = run(root)
    assert (root / MADE).exists(), "premise: the gate wrote the file"
    assert "Traceback" not in output, output
    assert code == 2, output
    text = record.read_text()
    assert text.isascii(), "the record must not depend on how a reader decodes it"
    loaded = json.loads(text)
    assert MADE in loaded["drift_paths"], loaded
    assert "made\\udcff" in text, "the undecodable byte is carried as a JSON escape"


def test_a_record_naming_an_undecodable_path_survives_emit_and_load(tmp_path, verify):
    """The representation above is json.dumps' default, not an accident: pin it."""
    record = {"schema": "probe", "drift_paths": [MADE, BAD_NAME, "café.txt"]}
    path = tmp_path / "record.json"
    verify.emit(record, path)
    assert path.read_bytes().isascii()
    assert verify.load_json(path) == record
    assert [os.fsencode(p) for p in verify.load_json(path)["drift_paths"]] == \
        [b"made\xff", b"bad\xffname", "café.txt".encode()], "the bytes of the path round-trip"


# --------------------------------------------------------------------------
# What could be identified before is identified the same way now.
# --------------------------------------------------------------------------

def test_valid_utf8_state_ids_are_unchanged_from_the_strict_verifier(tmp_path, verify):
    source = subprocess.run(["git", "show", f"{STRICT}:skills/verification-ladder/bin/verify.py"],
                            cwd=REPO, capture_output=True, check=False)
    if source.returncode != 0:
        pytest.skip("this checkout does not have the v0.3.1 commit to compare against")
    strict_path = tmp_path / "strict_verify.py"
    strict_path.write_bytes(source.stdout)
    strict = load(strict_path, "ladder_verify_strict")

    root = make_repo(tmp_path / "repo")
    (root / "tracked-é.txt").write_text("one\n")
    (root / "to-rename.txt").write_text("rename me\n")
    commit(root, "more tracked files")
    (root / "tracked-é.txt").write_text("two\n")                       # modified
    subprocess.run(["git", "mv", "to-rename.txt", "renamed-ü.txt"], cwd=root, check=True)  # rename
    (root / "café.txt").write_text("untracked\n")                     # untracked, non-ASCII
    (root / "dir").mkdir()
    (root / "dir" / "日本.txt").write_text("deep\n")
    os.symlink("src.txt", root / "link-ñ")                            # symlink, non-ASCII
    nested = root / "nested"
    nested.mkdir()
    subprocess.run(["git", "init", "-q", str(nested)], check=True)
    (nested / "real-ö.txt").write_text("real\n")
    os.symlink("/elsewhere/ß", nested / "to-ß")

    assert verify.state_id(root) == strict.state_id(root)
    assert verify.state_paths(root) == strict.state_paths(root)


# --------------------------------------------------------------------------
# A path that cannot be read is BLOCKED, never a traceback.
# --------------------------------------------------------------------------

def _exit_of(verify, root, *args):
    try:
        return verify.main(["--repo", str(root), *args])
    except SystemExit as stop:
        return stop.code


def test_a_path_that_cannot_be_read_blocks_naming_it(repo, verify, monkeypatch, capsys):
    (repo / "secret.txt").write_text("unreadable\n")
    real = Path.read_bytes

    def refuse(self):
        if self.name == "secret.txt":
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", refuse)
    assert _exit_of(verify, repo, "run", "--output", str(repo / ".verification" / "local.json")) == 2
    err = capsys.readouterr().err
    assert "secret.txt" in err and "PermissionError" in err


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="permissions do not bind the superuser")
@pytest.mark.parametrize("where", ["file", "nested-directory"])
def test_an_unreadable_path_on_disk_blocks(repo, where):
    if where == "file":
        target = repo / "secret.txt"
        target.write_text("unreadable\n")
    else:
        nested = repo / "nested"
        nested.mkdir()
        subprocess.run(["git", "init", "-q", str(nested)], check=True)
        target = nested / "locked"
        target.mkdir()
        (target / "inside.txt").write_text("hidden\n")
    target.chmod(0)
    try:
        code, output, _ = run(repo)
    finally:
        target.chmod(0o700)
    assert "Traceback" not in output, output
    assert code == 2, output
