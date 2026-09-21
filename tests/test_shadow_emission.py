"""§M1: a second representation of one execution, with no authority at all.

Shadow emission exists so the evidence/3 construction path runs against real
executions before anything depends on it. Everything here is about what it must
NOT do - change a verdict, change an exit code, run a gate twice, reach a v2
composite, or perturb the state it is describing.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
PASS_GATE = f"{sys.executable} -c pass"
POLICY = ('required_gates = ["unit"]\njudgment_rungs = ["diff"]\n\n'
          '[gates.local]\nunit = "true"\n')


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "unit.py").write_text("VALUE = 1\n")
    (tmp_path / "verification.toml").write_text(POLICY)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.invalid", "-c", "user.name=a", "commit", "-qm", "b"],
                   cwd=tmp_path, check=True)
    return tmp_path


def run(repo, *args):
    r = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *args],
                       capture_output=True, text=True, check=False)
    return r.returncode, r.stdout + r.stderr


def shadow_of(repo, stem="local"):
    return json.loads((repo / ".verification" / "shadow" / f"{stem}.v3.json").read_text())


def test_shadow_emission_does_not_change_the_authoritative_verdict(repo):
    code, output = run(repo, "run", "--output", str(repo / ".verification" / "local.json"))
    authoritative = json.loads((repo / ".verification" / "local.json").read_text())
    assert (code, authoritative["verdict"]) == (0, "PASS")
    assert authoritative["schema"] == "verification.ladder.evidence/2"
    assert "shadow" not in authoritative
    assert "no authority" in output


def test_a_failing_gate_keeps_its_exit_code_with_shadow_emission(repo):
    code, _ = run(repo, "run", "--gate", "failing=false",
                  "--output", str(repo / ".verification" / "local.json"))
    assert code == 1, "the v2 verdict decides the exit code; the shadow record has no vote"


def test_both_representations_describe_one_execution(repo):
    """Not two runs. The v3 rows are built from the results the v2 record holds."""
    marker = repo / "ran.count"
    counter = repo / "count.py"
    counter.write_text("from pathlib import Path\n"
                       "p = Path(__file__).with_name('ran.count')\n"
                       "p.write_text(str(int(p.read_text()) + 1 if p.exists() else 1))\n")
    marker.unlink(missing_ok=True)
    run(repo, "run", "--gate", f"counted={sys.executable} {counter}",
        "--output", str(repo / ".verification" / "local.json"))
    assert marker.read_text() == "1", "the gate must execute once for both representations"
    v2 = json.loads((repo / ".verification" / "local.json").read_text())
    v3 = shadow_of(repo)
    assert [g["name"] for g in v2["gates"]] == [g["gate"] for g in v3["gates"]]
    assert [g["status"] for g in v2["gates"]] == [g["status"] for g in v3["gates"]]


def test_the_shadow_record_carries_no_authority(repo):
    run(repo, "run", "--output", str(repo / ".verification" / "local.json"))
    v3 = shadow_of(repo)
    assert v3["schema"] == "verification.ladder.evidence/3"
    assert v3["shadow"] is True
    assert "verdict" not in v3, "a shadow record states no verdict; only v2 does"
    assert v3["verifier"]["compatibility"]


def test_a_shadow_record_cannot_satisfy_a_v2_gate(repo):
    run(repo, "run", "--output", str(repo / ".verification" / "local.json"))
    shadow = repo / ".verification" / "shadow" / "local.v3.json"
    code, output = run(repo, "compose", str(shadow))
    assert code == 2
    assert "not a verification.ladder.evidence/2 record" in output


def test_a_v2_record_cannot_be_read_as_a_shadow_record(repo):
    """The mirror of the above. Neither reader accepts the other's representation."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ladder_shadow", SCRIPT)
    verify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify)
    run(repo, "run", "--output", str(repo / ".verification" / "local.json"))
    with pytest.raises(SystemExit) as raised:
        verify.load_shadow_record(repo / ".verification" / "local.json")
    assert raised.value.code == 2


def test_the_documented_glob_does_not_reach_shadow_records(repo):
    run(repo, "run", "--output", str(repo / ".verification" / "local.json"))
    run(repo, "attest", "--rung", "diff", "--note", "read it")
    globbed = sorted(p.name for p in (repo / ".verification").glob("*.json"))
    assert globbed == ["attestations.json", "local.json"], "shadow records sit below the glob"


def test_shadow_emission_does_not_perturb_the_state_it_describes(repo):
    """G2 again, for the files §M1 adds."""
    first = run(repo, "run", "--output", str(repo / ".verification" / "local.json"))
    one = json.loads((repo / ".verification" / "local.json").read_text())
    assert one["drift"] is False
    second = run(repo, "run", "--output", str(repo / ".verification" / "local.json"))
    two = json.loads((repo / ".verification" / "local.json").read_text())
    assert (first[0], second[0]) == (0, 0)
    assert one["repository"]["state_id"] == two["repository"]["state_id"], (
        "the shadow record written by the first run must not move the second run's state")
    assert two["drift"] is False


def test_a_gate_the_policy_does_not_declare_has_no_definition_identity(repo):
    """An ungoverned custom gate: v2 records it, the v3 path gives it no identity."""
    code, output = run(repo, "run", "--gate", f"undeclared={PASS_GATE}",
                       "--output", str(repo / ".verification" / "local.json"))
    assert code == 0, "the v2 result stands whatever the shadow path does"
    assert "shadow" in output, "a shadow outcome is always reported, never silent"
    v3 = shadow_of(repo)
    assert v3["gates"][0]["definition"] == "undeclared", (
        "a gate the policy does not declare has no definition identity, and says so")


def test_a_shadow_construction_failure_is_visible_and_harmless(repo):
    """The shadow path fails; the task does not.

    Provoked by occupying the shadow directory's path with a file, which is the
    one way to break the shadow write while leaving the authoritative write
    intact. Permissions will not do it when the tests run as root.
    """
    (repo / ".verification").mkdir(exist_ok=True)
    (repo / ".verification" / "shadow").write_text("not a directory\n")
    code, output = run(repo, "run", "--output", str(repo / ".verification" / "local.json"))
    assert code == 0, "a shadow failure must never fail a task whose gates passed"
    assert "shadow   not constructed" in output
    assert "the result above is unaffected" in output
    authoritative = json.loads((repo / ".verification" / "local.json").read_text())
    assert authoritative["verdict"] == "PASS"
