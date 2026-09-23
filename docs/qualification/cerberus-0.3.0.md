# Consumer qualification: Cerberus on 0.3.0 (`evidence-v3.2`)

The first run of the Ladder end to end outside its own repository, against a
real consumer rather than more self-tests. Recorded on 2026-09-23.

| | |
|---|---|
| Ladder installed | runtime `c86bf47` (0.3.0, `evidence-v3.2`), via `scripts/install-ladder.sh` from `main` at `6ea587e` |
| Consumer | [Deep-Sixed/Cerberus](https://github.com/Deep-Sixed/Cerberus) at `3956a48`, branch `claude/verification-ladder-adoption` |
| Consumer change | [Deep-Sixed/Cerberus#20](https://github.com/Deep-Sixed/Cerberus/pull/20): `verification.toml` and `.verification/` in `.gitignore`, and nothing else |
| CI evidence | push run [35847808939](https://github.com/Deep-Sixed/Cerberus/actions/runs/35847808939), job `validate` |
| Result | `READY FOR HUMAN GATE TRUE` under authoritative evidence/3; six targeted mutations each refused for the reason that names them |

Nothing in the Ladder was changed to make this pass. The consumer supplied its
own policy, and the verifier ran as installed.

## 1. Install

The documented path, into a clean `HOME`:

```sh
git clone https://github.com/Deep-Sixed/verification-ladder /tmp/ladder-bootstrap
sh /tmp/ladder-bootstrap/scripts/install-ladder.sh
~/.local/share/verification-ladder/check-install.sh
```

The checker reported `INSTALL CONFORMS`: the clone was detached at the pin with a
replacement-free tracked tree, the skill symlink resolved into the pinned clone,
and the invariant was present verbatim and checksum-verified (`282f2117…`). The
installed `verify.py` reports `evidence-v3.2` / `0.3.0`.

With no policy, the installed verifier refuses Cerberus: `BLOCKED`, exit 2,
"no verification policy found". Silence is not consent.

## 2. The consumer's policy

Cerberus requires every step of its `validate` job as a gate:

- `suite` (`scripts/ci.sh`), which also runs locally;
- `build`, `frontend-syntax`, `compose-config`, `container-build`,
  `container-smoke` and `secret-scan`, which are established by CI only.

It adopts `verification.ladder.evidence/3` in the committed policy. It accepts CI
evidence only from `Deep-Sixed/Cerberus` → `.github/workflows/ci.yml` → job
`validate`, and only from push runs. Its six judgment rungs match this
repository's.

Unnamed `run:` steps are reported by the Actions API as `Run <command>`, and
that is the string each `[ci_steps]` entry names. All seven matched the real run
without adjustment.

## 3. Pre-activation evidence and qualification

All against state `3956a48 + sha256:d455437d…`:

| step | result |
|---|---|
| `baseline` | governs all 7 gates |
| `run` | `suite` PASS, 319 passed on managed Python 3.14.5, `drift: false`; evidence/2 record plus evidence/3 shadow, marked pre-activation |
| `import-ci` | all 7 gates PASS, each bound to its exact step; the provenance chain checked at import |
| `attest` ×6 | task, diff, self-review, requirements, architecture, fresh-review |
| `compose` | **refused**: the policy adopts evidence/3 and the checkout is not activated, so completion cannot be reported under evidence/2 |

`qualify` over the three records:

```
  QUALIFIED       outcome / kind / kind requirement / definition / authority
  QUALIFIED       projection binding / target projection
  OUT OF SCOPE    artifact chain   (no gate with evidence_mode = "execution+attestation")
  QUALIFIED       admissibility (aggregate) / verifier contract / governance
  QUALIFIED       ci provenance                1 agree
  QUALIFIED       ci step                      7 agree
  QUALIFIED 12  DISAGREEMENT 0  NOT COMPARABLE 0  UNCOVERED 0  OUT OF SCOPE 1
  M3 QUALIFIED             TRUE
```

`activate` → `AUTHORITY SWITCH TRUE`. The declaration records the 12 qualified
predicates and `artifact chain` as out of scope, which composition enforces if
Cerberus later adds a behavioural gate.

## 4. Authoritative evidence/3

`run`, `import-ci` for the same push run, and the six `attest` rungs were
re-run. All three records are `evidence/3`, `evidence-v3.2`, authoritative, and
PASS.

```
VERIFICATION STATE: 3956a48c83a9 + sha256:d455437d…  [evidence/3]
  * build, compose-config, container-build, container-smoke,
    frontend-syntax, secret-scan        PASS  execution  ci
  * suite                               PASS  execution  ci, local
    six judgment rungs                  PASS  attestation
  BLOCKED REQUIRED GATES   0
  STALE EVIDENCE           0
  STATE MATCH              TRUE
  EVIDENCE ADMISSIBLE      TRUE
  DEFINITION CHANGE        NONE
  CLEAN CLIMB              TRUE
  READY FOR HUMAN GATE     TRUE
```

## 5. Refusals

Each mutation changed one dimension, used copies of the records, and was undone
before the next:

| # | mutation | outcome |
|---|---|---|
| M1 | a tracked source file edited | `STALE EVIDENCE 3`, `STATE MATCH FALSE`, not ready |
| M2 | the CI record names another repository | "run belongs to 'someone-else/Cerberus', the repository under verification is 'Deep-Sixed/Cerberus'" |
| M3 | the CI job ran another commit | "job ran 000000000000, the run names 3956a48c83a9" |
| M4 | a record stamped with the pre-hardening contract | "records were produced under different verifier contracts (evidence-v3.1, evidence-v3.2)" |
| M5 | `main`'s green push run imported for this head | "run is for a0b39996bda0, expected 3956a48c83a9", and **no record written** |
| M6 | `.verification/authority.json` deleted | "this checkout carries no activation record … completion cannot be reported under verification.ladder.evidence/2" |

With the untouched evidence restored, `compose` read `READY FOR HUMAN GATE TRUE`
again, and Cerberus had no tracked changes.

## Observations

None of these is a defect that let bad evidence through. Each is recorded rather
than fixed here, so that qualifying a consumer doesn't turn into tailoring the
verifier until that consumer passes.

- **`compose` finds the baseline beside the first record it is given.** With the
  first record outside `.verification/`, it reports `GOVERNING BASELINE MISSING`
  even though the baseline exists beside the others. This fails closed, but the
  message sends the reader looking for a baseline that is present.
- **The consumer's local gate needs the consumer's toolchain.** `scripts/ci.sh`
  needs uv 0.11.29 and a uv-managed Python 3.14.5. The verifier runs whatever
  `PATH` provides, and the record's `output_tail` carries the toolchain line
  `scripts/ci.sh` prints. The Ladder itself needs only `git` and Python 3.11+.
- **The pinned checkout carries an older pair of scripts.** Since #7,
  `scripts/` inside `~/src/verification-ladder` is the pin's own pair, which
  expects an older pin. Run there, the checker reports `INSTALL DOES NOT CONFORM`
  rather than passing. `INSTALL.md` says to run the bootstrap copies.
- **This session's git access could not push the `v0.3.0` tag** (HTTP 403, the
  same restriction that refuses branch deletion). The installer pins by commit,
  so qualification did not depend on the tag. Tagging `6ea587e` is left to the
  maintainer.

## Not covered

- **Behavioural gates.** Cerberus declares none, so the artifact chain was out
  of scope rather than exercised on real artifacts. It is covered by this
  repository's own suite and by `tests/test_m4_activation.py`.
- **Gate timeouts and bounded execution**, which are deferred in the design
  (§G3–§G4).
