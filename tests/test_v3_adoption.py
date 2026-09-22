"""Adopting evidence/3: who may, how it survives, and what a damaged one does.

Three defects, reproduced against the CLI before any of this existed.

* A malformed `.verification/authority.json` made `authority_active` return
  False, which is what a repository that never activated returns. The next
  command emitted evidence/2 and composed it to READY FOR HUMAN GATE TRUE.
  Weaker semantics were selected by a parse failure, without an explicit error.
* The activation record is the only place the contract lived, and it sits under
  a git-ignored directory. `rm -rf .verification`, or a fresh clone, and the
  repository verified under the older contract with nothing to say so.
* Qualification was verifier-wide. A local-only project - no CI gate, no
  behavioural gate - was refused on `artifact chain`, `ci provenance` and
  `ci step`: predicates its own declaration can never put in play. It could not
  adopt evidence/3 at all, for lack of capabilities it does not use.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
V3 = "verification.ladder.evidence/3"
AUTHORITY_SCHEMA = "verification.ladder.authority/1"

LOCAL_ONLY = '''required_gates = ["unit"]
judgment_rungs = ["diff"]
evidence = "verification.ladder.evidence/3"

[gates.local]
unit = "true"
'''
NO_ADOPTION = '''required_gates = ["unit"]
judgment_rungs = ["diff"]

[gates.local]
unit = "true"
'''


def cli(repo, *args):
    done = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *args],
                          capture_output=True, text=True, check=False)
    return done.returncode, done.stdout + done.stderr


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text(".verification/\n")
    (root / "verification.toml").write_text(LOCAL_ONLY)
    (root / "src.txt").write_text("src\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
                    "commit", "-qm", "base"], cwd=root, check=True)
    return root


def authority(repo):
    return repo / ".verification" / "authority.json"


def pre_activation_evidence(repo):
    """What `qualify` and `activate` read: evidence/2 records and their shadows."""
    assert cli(repo, "baseline")[0] == 0
    local = repo / ".verification" / "local.json"
    assert cli(repo, "run", "--output", str(local))[0] == 0
    assert cli(repo, "attest", "--rung", "diff", "--note", "read it",
               "--execution", str(local))[0] == 0
    return local, repo / ".verification" / "attestations.json"


def activate(repo):
    local, attestations = pre_activation_evidence(repo)
    code, output = cli(repo, "activate", str(local), str(attestations))
    assert code == 0, output
    return output


# --------------------------------------------------------------------------
# A damaged activation declaration selects nothing.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("mangle", "because"), [
    pytest.param(lambda text: text[:-3], "unreadable", id="truncated"),
    pytest.param(lambda text: "not json at all", "unreadable", id="not-json"),
    pytest.param(lambda text: json.dumps({"schema": "something/else", "authority": V3}),
                 f"not a {AUTHORITY_SCHEMA}", id="wrong-schema"),
    pytest.param(lambda text: json.dumps({"schema": AUTHORITY_SCHEMA, "authority": "evidence/9"}),
                 "does not implement", id="unknown-authority"),
])
def test_a_damaged_activation_does_not_fall_back(repo, mangle, because):
    activate(repo)
    authority(repo).write_text(mangle(authority(repo).read_text()))

    for command in (["run", "--output", str(repo / ".verification" / "after.json")],
                    ["compose", str(repo / ".verification" / "local.json")]):
        code, output = cli(repo, *command)
        assert code == 2, f"{command}: {output}"
        assert because in output, f"{command}: {output}"
        assert "READY FOR HUMAN GATE     TRUE" not in output
        assert "evidence/2" not in output.replace("verification.ladder.evidence/2", ""), \
            "a damaged declaration must not be answered with the older contract"


# --------------------------------------------------------------------------
# The contract is committed, so it survives the evidence directory.
# --------------------------------------------------------------------------

def test_deleting_the_evidence_directory_does_not_downgrade_the_contract(repo):
    """`rm -rf .verification` is ordinary cleanup, and used to change the rules."""
    activate(repo)
    local, _ = pre_activation_evidence(repo)
    assert "evidence/3" in cli(repo, "run", "--output", str(local))[1]

    subprocess.run(["rm", "-rf", str(repo / ".verification")], check=True)
    code, output = cli(repo, "compose", str(local))
    assert code == 2, output
    assert "adopts verification.ladder.evidence/3" in output
    assert "no activation record" in output
    assert "READY FOR HUMAN GATE     TRUE" not in output


def test_an_activation_the_policy_does_not_carry_is_refused(repo):
    """The state that produced the hole: activated here, recorded nowhere durable."""
    activate(repo)
    (repo / "verification.toml").write_text(NO_ADOPTION)
    code, output = cli(repo, "run", "--output", str(repo / ".verification" / "x.json"))
    assert code == 2, output
    assert "does not adopt" in output
    assert "fresh clone" in output, "the refusal should say why it matters"


def test_the_migration_window_still_produces_evidence(repo):
    """Adopted but not yet activated is where pre-activation evidence comes from.

    Refusing to run there would make adoption impossible: `qualify` and
    `activate` read exactly the records this window produces. What is refused is
    reporting COMPLETION under the older contract, not producing evidence.
    """
    assert cli(repo, "baseline")[0] == 0
    local = repo / ".verification" / "local.json"
    code, output = cli(repo, "run", "--output", str(local))
    assert code == 0, output
    assert "pre-activation record" in output
    assert json.loads(local.read_text())["schema"] == "verification.ladder.evidence/2"

    code, output = cli(repo, "compose", str(local))
    assert code == 2, output
    assert "completion cannot be reported under verification.ladder.evidence/2" in output


# --------------------------------------------------------------------------
# A local-only consumer can adopt it.
# --------------------------------------------------------------------------

def test_a_local_only_project_qualifies(repo):
    local, attestations = pre_activation_evidence(repo)
    code, output = cli(repo, "qualify", str(local), str(attestations))
    assert code == 0, output
    assert "M3 QUALIFIED             TRUE" in output
    for predicate in ("artifact chain", "ci provenance", "ci step"):
        assert f"OUT OF SCOPE    {predicate}" in output, output
    assert "declares no gate that CI establishes" in output
    assert "declares no gate producing artifacts" in output


def test_activation_records_what_it_actually_covered(repo):
    output = activate(repo)
    assert "AUTHORITY SWITCH        TRUE" in output
    record = json.loads(authority(repo).read_text())
    assert record["authority"] == V3
    assert "outcome" in record["qualified"] and "governance" in record["qualified"]
    assert set(record["out_of_scope"]) == {"artifact chain", "ci provenance", "ci step"}
    assert not set(record["qualified"]) & set(record["out_of_scope"])


def test_adding_a_ci_gate_is_not_covered_by_a_local_only_activation(repo):
    """Narrowing the bar is only safe while the consumer stays narrow.

    The activation never exercised the provenance chain, so it cannot vouch for
    evidence that now needs it. Composition says so instead of reading the older
    activation as covering the new gate.
    """
    activate(repo)
    local, _ = pre_activation_evidence(repo)
    assert cli(repo, "compose", str(local))[1].count("ci provenance") == 0

    (repo / "verification.toml").write_text(LOCAL_ONLY + '''
[ci_steps]
unit = "Run the unit suite"

[shadow.ci]
repository = "acme/widget"
workflow = ".github/workflows/ci.yml"
job = "validate"
''')
    code, output = cli(repo, "compose", str(local))
    assert code != 0, output
    assert "ci provenance: this policy now needs it" in output
    assert "did not qualify it" in output


# --------------------------------------------------------------------------
# `check` speaks the authoritative contract.
# --------------------------------------------------------------------------

def test_check_reads_an_evidence_3_record(repo):
    """It read evidence/2 only, so on an activated repository it refused every
    record the other commands were writing."""
    activate(repo)
    local = repo / ".verification" / "v3.json"
    assert cli(repo, "run", "--output", str(local))[0] == 0
    assert json.loads(local.read_text())["schema"] == V3

    code, output = cli(repo, "check", str(local))
    assert code == 0, output
    assert "evidence/3" in output
    assert "differs from this record" not in output

    (repo / "src.txt").write_text("moved\n")
    code, output = cli(repo, "check", str(local))
    assert code == 2, output
    assert "repository differs from this record" in output


def test_check_refuses_a_shadow_record(repo):
    """A shadow record binds to nothing, so there is nothing to re-bind."""
    pre_activation_evidence(repo)
    shadow = repo / ".verification" / "shadow" / "local.v3.json"
    assert shadow.exists()
    code, output = cli(repo, "check", str(shadow))
    assert code == 2, output
    assert "diagnostic only" in output
