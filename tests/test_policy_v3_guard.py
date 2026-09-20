"""Evidence/3 policy syntax, named rather than misread.

docs/design/evidence-v3.md documents per-gate tables. This release parses that
form but does not act on it, and read as v2 it produced two wrong answers: a
dotted-gate-name complaint about a gate with no dot, and silence for gates
declared under [gates.ci.*] or [gates.behavioral.*]. Both failed closed. Both
misdiagnosed a policy that said clearly what it meant.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
_spec = importlib.util.spec_from_file_location("ladder_verify_policy_guard", SCRIPT)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)

V2_POLICY = 'required_gates = ["lint"]\n\n[gates.local]\nlint = "true"\n\n[ci_steps]\nlint = "Run true"\n'


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "unit.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.invalid", "-c", "user.name=a", "commit", "-qm", "b"],
                   cwd=tmp_path, check=True)
    return tmp_path


def run(repo, *args):
    result = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *args],
                            capture_output=True, text=True, check=False)
    return result.returncode, result.stdout + result.stderr


def test_a_v2_policy_is_untouched_by_the_guard(repo):
    (repo / "verification.toml").write_text(V2_POLICY)
    code, output = run(repo, "run", "--output", str(repo / ".verification" / "r.json"))
    assert code == 0, output
    assert "evidence/3" not in output


@pytest.mark.parametrize(
    ("block", "named"),
    [
        pytest.param('[gates.local.lint]\ncommand = "true"\ntarget = ["repository"]\n',
                     "[gates.local.lint]", id="local-per-gate-table"),
        pytest.param('[gates.ci.container-build]\nstep = "Run docker build ."\n',
                     "[gates.ci.*]", id="ci-block"),
        pytest.param('[gates.behavioral.verify-login]\ndriver = "node drive.mjs"\n'
                     'evidence_mode = "execution+attestation"\n',
                     "[gates.behavioral.*]", id="behavioral-block"),
    ],
)
def test_documented_v3_syntax_fails_closed_and_says_why(repo, block, named):
    (repo / "verification.toml").write_text('required_gates = ["lint"]\n\n' + block)
    code, output = run(repo, "run")
    assert code == 2, output
    assert named in output
    assert verify.SCHEMA_V3 in output
    assert "not yet authoritative" in output
    assert "rename the gate" not in output, "the gate has no dot in its name; that advice is wrong here"


def test_the_guard_covers_compose_and_import_ci_too(repo):
    (repo / "verification.toml").write_text('required_gates = ["lint"]\n\n[gates.ci.cb]\nstep = "s"\n')
    assert run(repo, "compose", "/dev/null")[0] == 2


def test_a_genuinely_dotted_gate_name_still_gets_the_dotted_explanation(repo):
    """`tests-3.11 = "..."` parses as table `tests-3` holding `11` - a different bug."""
    (repo / "verification.toml").write_text('required_gates = ["tests"]\n\n[gates.local]\n'
                                            'tests-3.11 = "pytest"\n')
    code, output = run(repo, "run")
    assert code == 2
    assert "rename the gate" in output
    assert verify.SCHEMA_V3 not in output, "a dotted name is not v3 syntax and must not be reported as it"


def test_a_quoted_dotted_gate_name_is_a_valid_v2_gate(repo):
    (repo / "verification.toml").write_text('required_gates = ["tests-3.11"]\n\n[gates.local]\n'
                                            '"tests-3.11" = "true"\n')
    code, output = run(repo, "run")
    assert code == 0, output


def test_malformed_v2_input_keeps_its_existing_diagnostics(repo):
    (repo / "verification.toml").write_text(V2_POLICY)
    assert "expects NAME=COMMAND" in run(repo, "run", "--gate", "noequals")[1]
    (repo / "verification.toml").write_text("this is not toml = = =\n")
    code, output = run(repo, "run")
    assert (code, "does not parse" in output) == (2, True)


def test_an_unconfigured_repository_still_reports_onboarding(repo):
    code, output = run(repo, "run")
    assert code == 2
    assert "no verification policy found" in output
    assert verify.SCHEMA_V3 not in output
