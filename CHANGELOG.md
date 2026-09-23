# Changelog

Each entry names the verifier `VERSION` and its compatibility contract. The
contract is what decides composition: records carrying a contract other than the
verifier's own are refused (`docs/design/evidence-v3.md` §J). A release that
changes what admissible means increments it; a patch that does not, keeps it.

## 0.3.0 — contract `evidence-v3.2`

The same evidence/3 record shape as `evidence-v3.1`, judged under stricter
rules. Records written under `evidence-v3.1` no longer compose with this
verifier: re-run `run`, `attest` and `import-ci` to re-establish them.

What changed what admissible means:

- **Execution binds to the state it measured** (#3). `run` captures the target
  before the gates execute and records any dimension that moved while they ran.
  A drifted record, or one whose own verdict was BLOCKED or FAIL, is refused at
  composition rather than rehabilitated by the passing rows inside it.
- **CI evidence binds the whole provenance chain** (#4). Repository, run,
  attempt, workflow, job and commit are checked against expectations the policy
  declares in `[ci]`, never against values read from the payload under
  examination. `import-ci` refuses a chain it cannot bind before writing any
  record, and composition checks the chain again.
- **The adopted contract is committed and fails closed** (#5). A project adopts
  evidence/3 with `evidence = "verification.ladder.evidence/3"` in its policy
  *and* an activation record in the checkout. A damaged activation record, an
  unrecognized contract, or an activation the policy does not carry is refused
  rather than read as evidence/2. Qualification is scoped to the predicates the
  consumer's own policy puts in play.

Installation (#2, #7): `scripts/install-ladder.sh` and `scripts/check-install.sh`
install and check a pinned checkout. #7 moves their pin from `760977f`, which
predates everything above and reports `evidence-v3.1`, to `c86bf47`, the commit
that released this contract. It had to be a separate change because the
installer refuses a pin that is not already on `main`.

## 0.2.0 — contract `evidence-v3.1`

evidence/3 authority becomes available behind qualification and activation
(`docs/design/evidence-v3.md` §M4).
