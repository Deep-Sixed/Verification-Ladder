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
    # Valid JSON that is not a table. Each of these parsed, and reading `.get`
    # off it raised AttributeError: a traceback, not the refusal promised above.
    pytest.param(lambda text: "[1]", "holds a JSON list", id="a-list"),
    pytest.param(lambda text: '"activated"', "holds a JSON str", id="a-string"),
    pytest.param(lambda text: "5", "holds a JSON int", id="a-number"),
    # A declaration whose coverage lists are damaged. Composition iterates them,
    # so `null` here was a TypeError at `compose` rather than a refusal at load.
    pytest.param(lambda text: json.dumps({**json.loads(text), "qualified": None}),
                 "`qualified` must be a list", id="qualified-null"),
    pytest.param(lambda text: json.dumps({**json.loads(text), "qualified": "outcome"}),
                 "`qualified` must be a list", id="qualified-a-string"),
    pytest.param(lambda text: json.dumps({**json.loads(text), "out_of_scope": ["ci step"]}),
                 "`out_of_scope` must map", id="out-of-scope-a-list"),
])
def test_a_damaged_activation_does_not_fall_back(repo, mangle, because):
    activate(repo)
    authority(repo).write_text(mangle(authority(repo).read_text()))

    for command in (["run", "--output", str(repo / ".verification" / "after.json")],
                    ["compose", str(repo / ".verification" / "local.json")]):
        code, output = cli(repo, *command)
        assert "Traceback" not in output, f"{command}: a crash is not a refusal\n{output}"
        assert code == 2, f"{command}: {output}"
        assert because in output, f"{command}: {output}"
        assert "READY FOR HUMAN GATE     TRUE" not in output
        assert "evidence/2" not in output.replace("verification.ladder.evidence/2", ""), \
            "a damaged declaration must not be answered with the older contract"


def bare_repo(tmp_path, policy_text=None):
    """A repository with no policy, or with exactly the policy text given."""
    root = tmp_path / "bare"
    root.mkdir()
    (root / ".gitignore").write_text(".verification/\n")
    (root / "src.txt").write_text("src\n")
    if policy_text is not None:
        (root / "verification.toml").write_text(policy_text)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
                    "commit", "-qm", "base"], cwd=root, check=True)
    return root


def test_reading_the_contract_does_not_print_a_refusal_it_then_ignores(tmp_path):
    """`attest` has never needed a policy, and must not announce BLOCKED and then succeed.

    Finding out which contract applies read the policy through `policy()` and
    caught its SystemExit. `blocked` prints as it builds that exit, so the
    onboarding refusal reached the reader anyway - twice, once for the authority
    check and once for the migration notice - above a command that exited 0.
    """
    root = bare_repo(tmp_path)
    code, output = cli(root, "attest", "--rung", "diff", "--note", "read it")
    assert code == 0, output
    assert "attested diff" in output
    assert "no verification policy found" not in output, output
    assert "BLOCKED" not in output, output


def test_a_policy_that_does_not_parse_is_not_read_as_silent(tmp_path):
    """Broken is not absent, and the refusal has to name what is actually wrong.

    The same `except SystemExit` read an unparseable policy as `{}`, so with an
    activation record present the reader was told the policy "does not adopt"
    evidence/3 - sending them to add a line their policy may well already have.
    """
    root = bare_repo(tmp_path, LOCAL_ONLY + "\nthis is not toml\n")
    (root / ".verification").mkdir()
    authority(root).write_text(json.dumps({"schema": AUTHORITY_SCHEMA, "authority": V3}))
    code, output = cli(root, "attest", "--rung", "diff", "--note", "read it")
    assert code == 2, output
    assert "policy does not parse" in output, output
    assert "does not adopt" not in output, output
    assert output.count("policy does not parse") == 1, "one refusal, stated once"


GATE_LEAVES_A_MARK = LOCAL_ONLY.replace('unit = "true"', 'unit = "touch gate-ran"')


@pytest.mark.parametrize(("policy_text", "authority_text", "because"), [
    pytest.param(GATE_LEAVES_A_MARK.replace(V3, "evidence/typo"), None,
                 "evidence must be", id="unrecognized-contract"),
    pytest.param(GATE_LEAVES_A_MARK, "not json at all", "unreadable", id="damaged-activation"),
    pytest.param(GATE_LEAVES_A_MARK.replace(f'evidence = "{V3}"\n', ""),
                 json.dumps({"schema": AUTHORITY_SCHEMA, "authority": V3}),
                 "does not adopt", id="activation-not-adopted"),
])
def test_run_refuses_a_contract_before_any_gate_executes(tmp_path, policy_text, authority_text, because):
    """The contract is decided before the suite runs, not after it.

    Each of these refuses. `run` used to ask only once every gate had finished,
    so a mistyped `evidence` line cost a full test run whose results were then
    discarded with no record written.
    """
    root = bare_repo(tmp_path, policy_text)
    (root / ".verification").mkdir()
    if authority_text is not None:
        authority(root).write_text(authority_text)
    record = root / ".verification" / "local.json"
    code, output = cli(root, "run", "--output", str(record))
    assert code == 2, output
    assert because in output, output
    assert not (root / "gate-ran").exists(), "a gate executed under a contract that was already refused"
    assert not record.exists()


def test_import_ci_refuses_a_contract_before_reading_the_payload(tmp_path):
    """The same order for `import-ci`: the contract is the answer, whatever the payload says."""
    root = bare_repo(tmp_path, LOCAL_ONLY.replace(V3, "evidence/typo"))
    run_payload, job_payload = tmp_path / "run.json", tmp_path / "job.json"
    run_payload.write_text(json.dumps({"head_sha": "0" * 40, "event": "push"}))
    job_payload.write_text("{}")
    code, output = cli(root, "import-ci", "--run", str(run_payload), "--job", str(job_payload))
    assert code == 2, output
    assert "evidence must be" in output, output
    assert "run is for" not in output, "the payload was examined under a contract that refuses"


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
    assert "declares no behavioural gate" in output


def test_activation_records_what_it_actually_covered(repo):
    output = activate(repo)
    assert "AUTHORITY SWITCH        TRUE" in output
    record = json.loads(authority(repo).read_text())
    assert record["authority"] == V3
    assert "outcome" in record["qualified"] and "governance" in record["qualified"]
    assert set(record["out_of_scope"]) == {"artifact chain", "ci provenance", "ci step"}
    assert not set(record["qualified"]) & set(record["out_of_scope"])


def shadow_gate(mode, artifacts=None):
    """`unit`, re-read under evidence/3 with the given mode and artifact declaration."""
    body = f'\n[shadow.gates.local.unit]\ncommand = "true"\nevidence_mode = "{mode}"\n'
    return LOCAL_ONLY + body + (f"artifacts = {artifacts}\n" if artifacts is not None else "")


def test_an_execution_gate_that_names_artifacts_does_not_put_the_chain_in_play(tmp_path):
    """The chain applies to behavioural gates. Naming a report file does not make one.

    Scope was decided by whether any gate declared `artifacts`, while
    `compare_gate` applies the chain only to `execution+attestation` gates. An
    execution gate naming a file put the chain in scope, the comparison then
    answered N/A for it, the predicate read UNCOVERED, and activation was
    refused on something this policy can never exercise.
    """
    root = bare_repo(tmp_path, shadow_gate("execution", '["build/report.txt"]'))
    local, attestations = pre_activation_evidence(root)
    code, output = cli(root, "qualify", str(local), str(attestations))
    assert code == 0, output
    assert "OUT OF SCOPE    artifact chain" in output, output
    assert "declares no behavioural gate" in output
    assert "M3 QUALIFIED             TRUE" in output

    code, output = cli(root, "activate", str(local), str(attestations))
    assert code == 0, output
    assert "artifact chain" in json.loads(authority(root).read_text())["out_of_scope"]


@pytest.mark.parametrize("artifacts", [
    pytest.param(None, id="no-artifacts-declared"),
    pytest.param("[]", id="an-empty-list"),
    pytest.param('["build/never-written.json"]', id="an-artifact-never-produced"),
])
def test_a_behavioural_gate_keeps_the_artifact_chain_in_scope(repo, artifacts):
    """The other direction: a behavioural gate is never waved away for lacking artifacts.

    A missing or empty artifact declaration is exactly what the chain exists to
    refuse, so it cannot also be what takes the chain out of scope. Here a
    local-only activation - which recorded the chain OUT OF SCOPE - meets a
    policy that has since made `unit` behavioural. Keyed on `artifacts`, the
    first two cases still read the chain as out of scope, and composition
    accepted an activation that never exercised it.
    """
    activate(repo)
    assert "artifact chain" in json.loads(authority(repo).read_text())["out_of_scope"]
    local, _ = pre_activation_evidence(repo)

    (repo / "verification.toml").write_text(shadow_gate("execution+attestation", artifacts))
    code, output = cli(repo, "compose", str(local))
    assert code != 0, output
    assert "artifact chain: this policy now needs it" in output, output
    assert "READY FOR HUMAN GATE     TRUE" not in output


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
    assert "repository.worktree_state differs from this record" in output


def test_check_names_drift_the_way_the_rest_of_the_verifier_does(repo):
    """One reading of "which dimensions of the target differ", not two.

    `check` grew its own comparison over the target while `target_drift` lived
    on an unlanded branch, and the two disagreed on granularity: the helper
    names `repository.worktree_state`, the local one named `repository`. A
    verifier that answers "what moved" two ways teaches the reader to trust
    neither, so `check` delegates, and a stale record is described in the
    vocabulary a drifted run already prints.
    """
    activate(repo)
    local = repo / ".verification" / "v3.json"
    assert cli(repo, "run", "--output", str(local))[0] == 0

    (repo / "src.txt").write_text("moved\n")
    code, checked = cli(repo, "check", str(local))
    assert code == 2, checked
    assert "repository.worktree_state differs from this record" in checked
    assert "repository differs from this record" not in checked, \
        "the coarse dimension name would hide which part of the repository moved"

    # The same move, seen from the other side: a gate that writes into the tree
    # it is measuring. `run` names the dimension from `target_drift` directly,
    # and it is the name `check` just printed.
    (repo / "verification.toml").write_text(
        LOCAL_ONLY.replace('unit = "true"', 'unit = "sh -c \'printf moved-again > src.txt\'"'))
    code, ran = cli(repo, "run", "--output", str(repo / ".verification" / "drifted.json"))
    assert code == 2, ran
    assert "moved  repository.worktree_state" in ran


def test_check_still_binds_a_record_to_the_runtime_it_names(tmp_path):
    """The other side of re-binding over what a record binds: nothing looser.

    `check` compares only the dimensions a record carries, so a CI record, which
    binds the repository alone, is not expired by a runtime it never described.
    A local record that DOES name a runtime binds to it. The facts file sits in
    the git-ignored evidence directory here, so replacing it moves the runtime
    and nothing else - the one change the worktree digest cannot see.
    """
    root = bare_repo(tmp_path, LOCAL_ONLY + '\n[shadow.runtime]\nfacts_file = ".verification/runtime.json"\n')
    (root / ".verification").mkdir()
    (root / ".verification" / "runtime.json").write_text('{"build": "A"}\n')
    activate(root)
    local = root / ".verification" / "v3.json"
    assert cli(root, "run", "--output", str(local))[0] == 0
    assert "runtime" in json.loads(local.read_text())["target_state"]
    assert cli(root, "check", str(local))[0] == 0

    (root / ".verification" / "runtime.json").write_text('{"build": "B"}\n')
    code, output = cli(root, "check", str(local))
    assert code == 2, output
    assert "runtime differs from this record" in output
    assert not [line for line in output.splitlines() if "repository" in line and "differs" in line], \
        "only the runtime moved"


def test_check_refuses_a_shadow_record(repo):
    """A shadow record binds to nothing, so there is nothing to re-bind."""
    pre_activation_evidence(repo)
    shadow = repo / ".verification" / "shadow" / "local.v3.json"
    assert shadow.exists()
    code, output = cli(repo, "check", str(shadow))
    assert code == 2, output
    assert "diagnostic only" in output
