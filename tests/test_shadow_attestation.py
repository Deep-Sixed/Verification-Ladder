"""§M3: judgment, represented in evidence/3 without pretending to be execution.

M4 cannot make evidence/3 authoritative for a record class M3 never exercised
through it, and judgment is a record class. Nothing here re-runs anything to
manufacture the v3 view: it is derived from the authoritative attestation that
was already recorded, and that record must be unchanged by its existence.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
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

[shadow.gates.behavioral.verify-login]
driver = "python3 drive.py"
target = ["repository"]
evidence_mode = "execution+attestation"
artifacts = [".verification/artifacts/journey.json"]
'''


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "drive.py").write_text(DRIVER)
    (tmp_path / ".gitignore").write_text(".verification/\n__pycache__/\n")
    (tmp_path / "verification.toml").write_text(POLICY)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.invalid", "-c", "user.name=a", "commit", "-qm", "b"],
                   cwd=tmp_path, check=True)
    return tmp_path


def cli(repo, *args):
    r = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *args],
                       capture_output=True, text=True, check=False)
    return r.returncode, r.stdout + r.stderr


def local(repo):
    return repo / ".verification" / "local.json"


def judgments(repo):
    return repo / ".verification" / "attestations.json"


def shadow(repo, stem):
    return json.loads((repo / ".verification" / "shadow" / f"{stem}.v3.json").read_text())


def ran(repo):
    code, output = cli(repo, "run", "--output", str(local(repo)))
    assert code == 0, output
    return local(repo)


def attested(repo, rung="verify-login", note="watched it", execution=None):
    args = ["attest", "--rung", rung, "--note", note]
    if execution:
        args += ["--execution", str(execution)]
    code, output = cli(repo, *args)
    assert code == 0, output
    return judgments(repo)


def chain(repo):
    code, output = cli(repo, "compare", str(local(repo)), "--attestations", str(judgments(repo)))
    for index, line in enumerate(output.splitlines()):
        if "verify-login" in line and line.strip().endswith("artifact chain"):
            detail = "\n".join(output.splitlines()[index:index + 4])
            return line.split("verify-login")[0].strip(), detail, code
    raise AssertionError(f"no artifact-chain row:\n{output}")


# --- the record class exists, and it is honest about what it is --------------

def test_an_attestation_produces_a_shadow_representation(repo):
    ran(repo)
    attested(repo, execution=local(repo))
    record = shadow(repo, "attestations")
    assert record["authority"] == "agent"
    assert [(g["gate"], g["kind"]) for g in record["gates"]] == [("verify-login", "attestation")]
    assert record["unpaired"] == [], "a judgment record establishes no gate and answers for none"


def test_no_execution_is_fabricated(repo):
    ran(repo)
    attested(repo, execution=local(repo))
    for row in shadow(repo, "attestations")["gates"]:
        assert row["kind"] == "attestation"
        assert "exit_code" not in row and "command" not in row and "duration_s" not in row
    assert "at" in shadow(repo, "attestations")["gates"][0]


def test_nothing_is_re_run_to_produce_it(repo):
    ran(repo)
    before = (repo / ".verification" / "artifacts" / "journey.json").stat().st_mtime_ns
    attested(repo, execution=local(repo))
    after = (repo / ".verification" / "artifacts" / "journey.json").stat().st_mtime_ns
    assert before == after, "attest re-drove the gate to manufacture its evidence/3 view"


def test_the_authoritative_attestation_is_byte_identical_with_and_without_a_binding(repo):
    ran(repo)
    cli(repo, "attest", "--rung", "verify-login", "--note", "n",
        "--output", str(repo / ".verification" / "bare.json"))
    cli(repo, "attest", "--rung", "verify-login", "--note", "n", "--execution", str(local(repo)),
        "--output", str(repo / ".verification" / "bound.json"))
    bare = json.loads((repo / ".verification" / "bare.json").read_text())
    bound = json.loads((repo / ".verification" / "bound.json").read_text())
    for side in (bare, bound):
        side.pop("recorded_at")
        for gate in side["gates"]:
            gate.pop("at")
    assert bare == bound, "--execution reached the authoritative record; it must reach the shadow alone"
    assert shadow(repo, "bound")["gates"][0]["execution_ref"]
    assert "execution_ref" not in shadow(repo, "bare")["gates"][0]


def test_a_judgment_rung_is_not_read_as_an_undeclared_gate(repo):
    ran(repo)
    attested(repo, rung="diff", note="read every hunk")
    assert shadow(repo, "attestations")["gates"][0]["definition"] == "judgment rung"
    code, output = cli(repo, "compare", str(judgments(repo)))
    assert code == 0, output
    assert "N/A             diff             definition" in output


def test_a_binding_survives_attesting_a_second_rung(repo):
    """Each attest rewrites the whole record; a named binding must not evaporate."""
    ran(repo)
    attested(repo, execution=local(repo))
    attested(repo, rung="diff", note="read every hunk")
    bound = {g["gate"]: g for g in shadow(repo, "attestations")["gates"]}
    assert bound["verify-login"]["execution_ref"], "the earlier binding was dropped by the next attest"
    assert chain(repo)[0] == "AGREE"


# --- kind semantics, in both directions -------------------------------------

def test_a_v2_attestation_cannot_satisfy_an_execution_requirement(repo):
    ran(repo)
    attested(repo, rung="unit", note="looks fine to me")
    code, output = cli(repo, "compare", str(judgments(repo)))
    assert code == 1
    assert "attested, never executed" in output, "the kind requirement is what refuses this"
    assert "N/A             unit             authority" in output, (
        "permitted authorities say who may EXECUTE a gate; asking whether the agent may "
        "would refuse every honest judgment as well as this one")


def test_a_v3_shadow_attestation_cannot_satisfy_a_v2_gate(repo):
    """Control first: the v2 composer must be able to read a record at all."""
    ran(repo)
    code, output = cli(repo, "compose", str(local(repo)))
    assert (code, "unit" in output) == (1, True), "premise: the executed record composes and is read"
    assert "not a verification.ladder.evidence/2 record" not in output
    attested(repo, execution=local(repo))
    code, output = cli(repo, "compose", str(repo / ".verification" / "shadow" / "attestations.v3.json"))
    assert code == 2
    assert "not a verification.ladder.evidence/2 record" in output


def test_an_attestation_alone_reaches_no_human_gate(repo):
    attested(repo, rung="unit", note="fine")
    attested(repo, rung="verify-login", note="fine")
    attested(repo, rung="diff", note="fine")
    code, output = cli(repo, "compose", str(judgments(repo)))
    assert code == 1
    assert "READY FOR HUMAN GATE     FALSE" in output
    assert output.count("attested, never executed") == 2


# --- the chain, driven through the two commands that build it ---------------

def test_a_bound_judgment_completes_the_chain(repo):
    ran(repo)
    attested(repo, execution=local(repo))
    outcome, detail, code = chain(repo)
    assert (outcome, code) == ("AGREE", 0), detail


@pytest.mark.parametrize(
    ("label", "disturb", "because"),
    [
        pytest.param("no binding named", lambda r: None,
                     "nothing attests to what they show", id="unbound"),
        pytest.param("artifact rewritten after the judgment",
                     lambda r: (r / ".verification" / "artifacts" / "journey.json").write_bytes(b"{}"),
                     "bytes have changed", id="tampered"),
        pytest.param("artifact removed after the judgment",
                     lambda r: (r / ".verification" / "artifacts" / "journey.json").unlink(),
                     "not on disk", id="missing"),
    ],
)
def test_the_chain_fails_closed(repo, label, disturb, because):
    ran(repo)
    attested(repo, execution=None if label == "no binding named" else local(repo))
    disturb(repo)
    outcome, detail, _ = chain(repo)
    assert outcome == "DISAGREE", f"{label}: {detail}"
    assert because in detail, f"{label}: {detail}"


@pytest.mark.parametrize(
    ("label", "mutate", "because"),
    [
        pytest.param("execution_ref resolves to nothing",
                     lambda g: g.update(execution_ref="sha256:" + "0" * 64),
                     "nothing attests", id="ref"),
        pytest.param("judged a different artifact set",
                     lambda g: g.update(artifact_refs=["sha256:" + "9" * 64]),
                     "different set of artifacts", id="artifacts"),
        pytest.param("judged against another target",
                     lambda g: g["target_projection"]["repository"].update(head="b" * 40),
                     "different target", id="target"),
    ],
)
def test_a_tampered_binding_fails_closed(repo, label, mutate, because):
    ran(repo)
    attested(repo, execution=local(repo))
    path = repo / ".verification" / "shadow" / "attestations.v3.json"
    record = json.loads(path.read_text())
    mutate(record["gates"][0])
    path.write_text(json.dumps(record, indent=2))
    outcome, detail, _ = chain(repo)
    assert outcome == "DISAGREE", f"{label}: {detail}"
    assert because in detail, f"{label}: {detail}"


def test_an_artifact_change_does_not_alter_the_authoritative_record(repo):
    """A chain the v3 model refuses leaves the v2 result exactly where it was.

    Artifacts live under .verification/, which is excluded from repository state
    by construction, so v2 sees nothing at all here - which is the point. M3
    status must not leak into the composite.
    """
    ran(repo)
    attested(repo, execution=local(repo))
    attested(repo, rung="diff", note="read every hunk")
    before = (local(repo).read_bytes(), judgments(repo).read_bytes())
    was = cli(repo, "compose", str(local(repo)), str(judgments(repo)))
    assert (was[0], "READY FOR HUMAN GATE     TRUE" in was[1]) == (0, True), was[1]
    assert chain(repo)[0] == "AGREE"

    (repo / ".verification" / "artifacts" / "journey.json").write_bytes(b"{}")
    assert chain(repo)[0] == "DISAGREE", "premise: the v3 chain now refuses this evidence"
    now = cli(repo, "compose", str(local(repo)), str(judgments(repo)))
    assert now == was, "a qualification failure rewrote the authoritative composite"
    assert (local(repo).read_bytes(), judgments(repo).read_bytes()) == before
