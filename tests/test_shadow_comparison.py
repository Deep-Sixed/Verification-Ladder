"""§M2: the evidence/3 reading of an execution, compared against the execution.

M1 proved v3 can be produced without authority. M2 must prove it can be read
back and interpreted independently, still without acquiring any. So the tests
here are about two things: that a pair is only compared when it can be shown to
describe one execution, and that a disagreement is visible without changing
anything the task depends on.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
POLICY = ('required_gates = ["unit"]\njudgment_rungs = ["diff"]\n\n[gates.local]\nunit = "true"\n')


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


def executed(repo):
    run(repo, "run", "--output", str(repo / ".verification" / "local.json"))
    return repo / ".verification" / "local.json"


def shadow(repo):
    return repo / ".verification" / "shadow" / "local.v3.json"


def tamper(repo, mutate):
    path = shadow(repo)
    record = json.loads(path.read_text())
    mutate(record)
    path.write_text(json.dumps(record, indent=2))


def test_the_ordinary_path_agrees(repo):
    executed(repo)
    code, output = run(repo, "compare", str(repo / ".verification" / "local.json"))
    assert code == 0
    assert "DISAGREE 0" in output
    assert "NOT COMPARABLE 0" in output
    assert "EVIDENCE/3 QUALIFIED     TRUE" in output


def test_comparison_does_not_touch_the_authoritative_record(repo):
    record = executed(repo)
    before = record.read_bytes()
    run(repo, "compare", str(record))
    assert record.read_bytes() == before, "M2 reads; it does not write to the authoritative side"


def test_a_disagreement_does_not_change_the_task_result(repo):
    record = executed(repo)
    tamper(repo, lambda r: r["gates"][0].update(definition_sha256="sha256:" + "0" * 64))
    compare_code, output = run(repo, "compare", str(record))
    assert (compare_code, "DISAGREE 1" in output) == (1, True)
    rerun_code, _ = run(repo, "run", "--output", str(record))
    assert rerun_code == 0, "the authoritative workflow is unaffected by a shadow disagreement"
    assert json.loads(record.read_text())["verdict"] == "PASS"


@pytest.mark.parametrize(
    ("label", "mutate", "predicate"),
    [
        pytest.param("definition drift", lambda r: r["gates"][0].update(
            definition_sha256="sha256:" + "0" * 64), "definition", id="definition"),
        pytest.param("index drift", lambda r: r["target_state"]["repository"].update(
            index_state="sha256:" + "1" * 64), "target projection", id="index"),
        pytest.param("wrong kind", lambda r: r["gates"][0].update(kind="attestation"), "kind", id="kind"),
        pytest.param("verifier mismatch", lambda r: r["verifier"].update(
            compatibility="evidence-v2.9"), "verifier contract", id="verifier"),
    ],
)
def test_the_comparator_names_the_predicate_that_disagreed(repo, label, mutate, predicate):
    record = executed(repo)
    tamper(repo, mutate)
    code, output = run(repo, "compare", str(record))
    assert code == 1, label
    assert "DISAGREE" in output
    assert predicate in output, f"{label}: the exact predicate must be named, not a generic mismatch"


def test_wrong_authority_is_caught_where_the_gate_permits_only_one(repo):
    """Control first: the gate must actually have a single permitted authority."""
    record = executed(repo)
    assert json.loads(shadow(repo).read_text())["gates"][0]["permitted_authorities"] == ["local"]
    tamper(repo, lambda r: r.update(authority="ci"))
    code, output = run(repo, "compare", str(record))
    assert (code, "authority" in output) == (1, True)


@pytest.mark.parametrize(
    ("label", "prepare"),
    [
        pytest.param("missing", lambda p: p.unlink(), id="missing"),
        pytest.param("malformed", lambda p: p.write_text("not json\n"), id="malformed"),
        pytest.param("not a shadow record", lambda p: p.write_text(
            '{"schema":"verification.ladder.evidence/3"}\n'), id="not-shadow"),
    ],
)
def test_an_unusable_shadow_is_not_comparable_rather_than_agreeing(repo, label, prepare):
    record = executed(repo)
    prepare(shadow(repo))
    code, output = run(repo, "compare", str(record))
    assert code == 2, label
    assert "NOT COMPARABLE" in output
    assert "AGREE" not in output.split("NOT COMPARABLE")[0], "absence of evidence is not agreement"


def test_a_shadow_from_another_execution_is_refused(repo):
    """A stale shadow beside a fresh record: the exact pairing failure §M2 forbids."""
    record = executed(repo)
    stale = shadow(repo).read_bytes()
    (repo / "unit.py").write_text("VALUE = 2\n")
    run(repo, "run", "--output", str(record))
    shadow(repo).write_bytes(stale)
    code, output = run(repo, "compare", str(record))
    assert code == 2
    assert "describes another execution" in output
    assert "repository state differs" in output


def test_pairing_is_derived_not_discovered(repo):
    """A shadow record at some other name is not found by searching for one."""
    record = executed(repo)
    shadow(repo).rename(shadow(repo).parent / "somethingelse.v3.json")
    code, output = run(repo, "compare", str(record))
    assert code == 2
    assert "no shadow record at local.v3.json" in output


def test_the_comparison_never_mixes_authority(repo):
    """The v2 composite still reads only v2 records, whatever the comparison said."""
    record = executed(repo)
    run(repo, "attest", "--rung", "diff", "--note", "read it")
    code, output = run(repo, "compose", str(record), str(repo / ".verification" / "attestations.json"))
    assert "shadow" not in output.lower()
    assert code == 0
