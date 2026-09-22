"""Drifted and refused executions, driven through the CLI into composition.

The defect these cover was reproducible end to end and invisible to every unit
test in this suite: `run` reported BLOCKED with `drift: true` and exit 2, and
`compose` read the same record and printed CLEAN CLIMB TRUE / READY FOR HUMAN
GATE TRUE. Neither composition path looked at drift, or at the record's own
verdict. So these cases run the real commands against real git repositories and
assert on what the operator sees, rather than on a function's return value.

Two mechanisms are checked separately, because they fail separately:

* **binding** - evidence binds to the state the gates MEASURED. v3 bound to the
  post-execution state, so a gate that wrote into the tree described its own
  output and matched the checkout exactly because it had put it there.
* **rejection** - composition refuses a drifted or refused record outright.
  v2 survived on binding alone: restore the tree afterwards and the mismatch
  disappears, taking the protection with it. An incidental protection is one
  nobody is maintaining.
"""

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
VERIFY = REPO / "skills" / "verification-ladder" / "bin" / "verify.py"
AUTHORITY = {"schema": "verification.ladder.authority/1",
             "authority": "verification.ladder.evidence/3"}

MUTATE = r"""sh -c 'printf mutated > src/app.txt'"""
FLIP_RUNTIME = r"""sh -c 'printf {\"build\":\"B\"} > .verification/runtime.json'"""


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _verify(project, *args):
    return subprocess.run(["python3", str(VERIFY), "--repo", str(project), *args],
                          check=False, capture_output=True, text=True)


def _project(tmp_path, policy, *, v3, files=None):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.txt").write_text("original\n")
    (root / "verification.toml").write_text(policy)
    (root / ".gitignore").write_text(".verification/\n")
    _git("init", "-q", ".", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    _git("add", "-A", cwd=root)
    _git("-c", "commit.gpgsign=false", "commit", "-qm", "init", cwd=root)
    evidence = root / ".verification"
    evidence.mkdir(exist_ok=True)
    for name, body in (files or {}).items():
        (evidence / name).write_text(body)
    if v3:
        (evidence / "authority.json").write_text(json.dumps(AUTHORITY))
    assert _verify(root, "baseline").returncode == 0
    return root


def _policy(gate_command, *, runtime=False):
    body = ('required_gates = ["gate"]\njudgment_rungs = []\nci_head_events = ["push"]\n\n'
            f'[gates.local]\ngate = "{gate_command}"\n')
    if runtime:
        body += '\n[shadow.runtime]\nfacts_file = ".verification/runtime.json"\n'
    return body


def _record(project):
    return json.loads((project / ".verification" / "local.json").read_text())


# --------------------------------------------------------------------------
# The reproduction, both authorities, end to end.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("v3", [False, True], ids=["evidence2", "evidence3"])
def test_a_gate_that_moves_the_tree_cannot_compose_clean(tmp_path, v3):
    project = _project(tmp_path, _policy(MUTATE), v3=v3)

    run = _verify(project, "run", "--output", str(project / ".verification" / "local.json"))
    assert run.returncode == 2, "a drifted run is BLOCKED"
    record = _record(project)
    assert record["drift"] is True
    assert [g for g in record["gates"] if g.get("status") == "PASS"], \
        "the gate passes - that is what makes this composable and therefore dangerous"

    composed = _verify(project, "compose", str(project / ".verification" / "local.json"))
    assert composed.returncode != 0
    assert "READY FOR HUMAN GATE     FALSE" in composed.stdout
    assert "DRIFT" in composed.stdout


@pytest.mark.parametrize("v3", [False, True], ids=["evidence2", "evidence3"])
def test_rejection_does_not_depend_on_the_state_having_moved(tmp_path, v3):
    """The decisive case: restore the tree, so the record binds to the checkout.

    Before this change the v2 path refused a drifted record only because its
    evidence bound to the pre-execution state and the gate's own output then
    moved the checkout away from it - reported as staleness, not as drift.
    Restoring the tree removes the mismatch, and with it the protection. The
    same record must still be refused, and for the right reason.
    """
    project = _project(tmp_path, _policy(MUTATE), v3=v3)
    assert _verify(project, "run", "--output",
                   str(project / ".verification" / "local.json")).returncode == 2
    _git("checkout", "--", "src/app.txt", cwd=project)
    assert _git("status", "--porcelain", cwd=project).stdout == "", "tree is back to the bound state"

    composed = _verify(project, "compose", str(project / ".verification" / "local.json"))
    assert "STATE MATCH              TRUE" in composed.stdout, \
        "the premise of this test: binding no longer catches it"
    assert "STALE EVIDENCE           0" in composed.stdout
    assert composed.returncode != 0
    assert "READY FOR HUMAN GATE     FALSE" in composed.stdout
    assert "DRIFT" in composed.stdout


def test_v3_evidence_binds_to_the_state_the_gates_measured(tmp_path):
    """Not to the one they left behind."""
    project = _project(tmp_path, _policy(MUTATE), v3=True)
    baseline = json.loads((project / ".verification" / "shadow" / "baseline.v3.json").read_text())
    before = baseline["target_state"]["repository"]["worktree_state"]

    assert _verify(project, "run", "--output",
                   str(project / ".verification" / "local.json")).returncode == 2
    record = _record(project)
    bound = record["target_state"]["repository"]["worktree_state"]
    after = record["target_state_after"]["repository"]["worktree_state"]
    assert bound == before, "the record binds to the pre-execution state"
    assert after != bound, "and names the post-execution state separately"
    assert record["drift_dimensions"] == ["repository.worktree_state"]
    assert record["drift_paths"] == ["src/app.txt"]


def test_v3_run_says_what_moved(tmp_path):
    """v2 has explained drift since the beginning; the v3 summary printed BLOCKED alone."""
    project = _project(tmp_path, _policy(MUTATE), v3=True)
    run = _verify(project, "run", "--output", str(project / ".verification" / "local.json"))
    assert "drift" in run.stderr
    assert "moved  repository.worktree_state" in run.stderr
    assert "path   src/app.txt" in run.stderr
    assert "after  sha256:" in run.stderr


def test_a_gate_that_moves_only_the_runtime_is_drift(tmp_path):
    """The worktree digest cannot see a declared runtime manifest change.

    `.verification/` is git-ignored by construction, so a gate that rewrites the
    runtime facts it is being measured against leaves `git status` empty and the
    worktree state id identical. It has still measured one target and left
    another.
    """
    project = _project(tmp_path, _policy(FLIP_RUNTIME, runtime=True), v3=True,
                       files={"runtime.json": '{"build":"A"}\n'})
    run = _verify(project, "run", "--output", str(project / ".verification" / "local.json"))
    assert run.returncode == 2
    assert _git("status", "--porcelain", cwd=project).stdout == "", "the worktree never moved"
    record = _record(project)
    assert record["drift"] is True
    assert record["drift_dimensions"] == ["runtime"]
    assert "moved  runtime" in run.stderr

    composed = _verify(project, "compose", str(project / ".verification" / "local.json"))
    assert composed.returncode != 0
    assert "DRIFT - runtime changed" in composed.stdout


# --------------------------------------------------------------------------
# A refused record is not evidence, however it came to be refused.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("v3", [False, True], ids=["evidence2", "evidence3"])
def test_a_blocked_record_with_passing_gates_is_refused(tmp_path, v3):
    """Drift is one way to be BLOCKED with every gate green; it is not the only one."""
    project = _project(tmp_path, _policy("true"), v3=v3)
    output = project / ".verification" / "local.json"
    assert _verify(project, "run", "--output", str(output)).returncode == 0
    record = json.loads(output.read_text())
    assert record["verdict"] == "PASS"
    record["verdict"] = "BLOCKED"
    output.write_text(json.dumps(record))

    composed = _verify(project, "compose", str(output))
    assert composed.returncode != 0
    assert "record verdict is BLOCKED" in composed.stdout
    assert "READY FOR HUMAN GATE     FALSE" in composed.stdout


@pytest.mark.parametrize("v3", [False, True], ids=["evidence2", "evidence3"])
def test_a_clean_run_still_composes_complete(tmp_path, v3):
    """The guard against over-rejection: a gate that touches nothing is unaffected."""
    project = _project(tmp_path, _policy("true"), v3=v3)
    output = project / ".verification" / "local.json"
    assert _verify(project, "run", "--output", str(output)).returncode == 0
    assert _record(project)["drift"] is False

    composed = _verify(project, "compose", str(output))
    assert composed.returncode == 0, composed.stdout
    assert "READY FOR HUMAN GATE     TRUE" in composed.stdout
    assert "DRIFT" not in composed.stdout
