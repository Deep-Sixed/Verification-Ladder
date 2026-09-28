"""Provenance and contract are different questions about a verifier.

Which build wrote a record is reported. Which rules it was written under decides
whether it composes with anything else. Keeping those apart is what lets two
builds of the same contract compose while a semantics change invalidates
earlier evidence.
"""

import importlib.util
import re
import subprocess
from pathlib import Path
from pathlib import Path as _P

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verification-ladder" / "bin" / "verify.py"
_spec = importlib.util.spec_from_file_location("ladder_verify_contract", SCRIPT)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)


def _record(**over):
    return {"verifier": {"schema": verify.SCHEMA_V3, "version": verify.VERSION,
                         "compatibility": verify.COMPATIBILITY, "commit": "a" * 40,
                         "implementation_sha256": "sha256:x", **over}}


def test_records_under_one_contract_compose():
    assert verify.verifier_status([_record(), _record()]) == (verify.ADMISSIBLE, None)


def test_a_different_build_of_the_same_contract_still_composes():
    """commit and implementation digest are provenance; they never decide composability."""
    assert verify.verifier_status([
        _record(commit="a" * 40, implementation_sha256="sha256:one"),
        _record(commit="b" * 40, implementation_sha256="sha256:two"),
    ]) == (verify.ADMISSIBLE, None)


def test_records_under_different_contracts_do_not_compose():
    status, reason = verify.verifier_status([_record(), _record(compatibility="evidence-v3.0")])
    assert status == verify.INADMISSIBLE
    assert "different verifier contracts" in reason


def test_a_contract_this_verifier_does_not_apply_is_refused():
    status, reason = verify.verifier_status([_record(compatibility="evidence-v9.9")])
    assert status == verify.INADMISSIBLE
    assert "the rules in force" in reason


def test_records_from_the_pre_hardening_contract_are_refused():
    """evidence-v3.1 is the contract the pre-hardening verifier still stamps.

    Its records have the same shape as these and were judged under weaker rules:
    no drift rejection, no bound CI chain, an adoption that fell back to
    evidence/2 on a parse failure. The contract was incremented so that they
    stop composing here, not merely so that the number moved.
    """
    assert verify.COMPATIBILITY != "evidence-v3.1"
    status, reason = verify.verifier_status([_record(compatibility="evidence-v3.1")])
    assert status == verify.INADMISSIBLE
    assert "'evidence-v3.1'" in reason and "the rules in force" in reason

    status, reason = verify.verifier_status([_record(), _record(compatibility="evidence-v3.1")])
    assert status == verify.INADMISSIBLE
    assert "different verifier contracts" in reason


def test_the_release_notes_name_the_contract_this_verifier_applies():
    """A release that changes the contract says so in its notes (§J).

    Held mechanically, like VERSION against pyproject: the newest CHANGELOG entry
    must name this verifier's VERSION and COMPATIBILITY, so neither can move
    without the notes moving with it.
    """
    changelog = (_P(__file__).resolve().parents[1] / "CHANGELOG.md").read_text()
    newest = re.search(r"^## (\S+) \S+ contract `([^`]+)`", changelog, re.MULTILINE)
    assert newest, "CHANGELOG.md has no '## <version> - contract `<contract>`' entry"
    assert newest.group(1) == verify.VERSION
    assert newest.group(2) == verify.COMPATIBILITY


def test_a_record_without_a_contract_establishes_nothing():
    status, reason = verify.verifier_status([{"verifier": {"commit": "a" * 40}}])
    assert status == verify.INADMISSIBLE
    assert "no verifier compatibility contract" in reason

    status, _ = verify.verifier_status([{}])
    assert status == verify.INADMISSIBLE


def test_the_contract_is_not_the_schema():
    identity = verify.verifier_identity()
    assert identity["compatibility"] != identity["schema"]
    assert identity["compatibility"] == verify.COMPATIBILITY


def test_install_tells_users_to_pin_the_revision_they_run():
    """Exact verifier binding makes a clone that tracks a branch a moving part."""
    install = (_P(__file__).resolve().parents[1] / "INSTALL.md").read_text()
    assert "Pin the revision you install" in install
    assert "checkout --detach" in install


def test_install_never_names_a_ref_this_repository_does_not_have():
    """The original bug: the guide said `checkout v0.1.0` and no tag existed.

    Following an install guide should not fail at the install step. Any version
    ref the guide tells a user to check out must be one this repository can
    resolve, so the guide cannot get ahead of the releases that back it.
    """
    root = _P(__file__).resolve().parents[1]
    install = (root / "INSTALL.md").read_text()
    named = set(re.findall(r"checkout\s+(v[0-9][^\s`]*)", install))
    if not named:
        return
    tags = set(subprocess.run(["git", "tag", "-l"], cwd=root, capture_output=True, text=True,
                              check=True).stdout.split())
    assert named <= tags, f"INSTALL.md names {sorted(named - tags)}, which this repository has no tag for"


def test_install_names_each_release_by_the_commits_it_really_is():
    """The release table is provenance, so it has to match the repository.

    It said "there are no release tags yet" for two releases after the first tag.
    Each row now names a tag, the commit it resolves to, and the runtime the
    installer at that tag pins; all three are checked, not trusted.
    """
    root = _P(__file__).resolve().parents[1]
    install = (root / "INSTALL.md").read_text()
    assert "no release tags" not in install
    rows = re.findall(r"^\| ([0-9][^ |]*) \| `(v[^`]+)` → `([0-9a-f]{40})` \| `([0-9a-f]{40})` \|$",
                      install, re.MULTILINE)
    assert rows, "INSTALL.md has no release table"

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)

    for version, tag, tagged, runtime in rows:
        resolved = git("rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}")
        if resolved.returncode != 0:
            pytest.skip(f"this checkout does not have {tag}")
        assert resolved.stdout.strip() == tagged, f"{tag} resolves to {resolved.stdout.strip()}, not {tagged}"
        installer = git("show", f"{tagged}:scripts/install-ladder.sh").stdout
        assert f"PIN={runtime}\n" in installer, f"the installer at {tag} does not pin {runtime}"
        pinned = git("show", f"{runtime}:skills/verification-ladder/bin/verify.py").stdout
        assert f'VERSION = "{version}"' in pinned, f"the runtime {runtime} is not {version}"
