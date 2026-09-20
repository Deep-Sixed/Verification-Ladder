"""Provenance and contract are different questions about a verifier.

Which build wrote a record is reported. Which rules it was written under decides
whether it composes with anything else. Keeping those apart is what lets two
builds of the same contract compose while a semantics change invalidates
earlier evidence.
"""

import importlib.util
from pathlib import Path
from pathlib import Path as _P

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


def test_install_recommends_a_pinned_release_not_a_moving_clone():
    """Exact verifier binding makes a clone that tracks main a moving part under the evidence."""
    install = (_P(__file__).resolve().parents[1] / "INSTALL.md").read_text()
    assert "release tag rather than tracking `main`" in install
    assert "checkout v" in install
