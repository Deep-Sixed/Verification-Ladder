# Changelog

Each entry names the verifier `VERSION` and its compatibility contract. The
contract is what decides composition: records carrying a contract other than the
verifier's own are refused (`docs/design/evidence-v3.md` §J). A release that
changes what admissible means increments it; a patch that does not, keeps it.

## 0.3.1 — contract `evidence-v3.2`

A patch under the same contract: what counts as admissible evidence is
unchanged, so records written by 0.3.0 still compose with this verifier. Four
defects at the verifier's input and state boundaries, none of which let bad
evidence through (#9):

- **A record or CI payload that is valid JSON but not an object** (`[1]`,
  `"evidence"`, `5`, `null`) is refused as BLOCKED, naming the file and the
  type. `check`, `compose` and `import-ci` used to crash with a traceback.
- **`.verification/` is excluded from the state by prefix.** An artifact a gate
  wrote there was read as drift unless `.gitignore` hid it, because the
  exclusion listed only the files the directory held before the gates ran.
- **State hashing never follows a symlink inside a directory entry.** Git
  reports an untracked nested repository as one entry, and a link inside it
  pulled bytes from outside the repository into the state ID.
- **`compose` names the baseline path it checked** when the governing baseline
  is missing, and says where one does exist.

The second and third change the state ID only for trees with files under an
un-ignored `.verification/` or symlinks inside an untracked nested directory. A
record written by 0.3.0 over such a tree goes stale here; it fails safe and is
re-established by running again.

Installation still pins the 0.3.0 runtime. The installer refuses a pin that is
not already on `main`, so moving it to this release is a separate change.

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
