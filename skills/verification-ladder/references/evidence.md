# Evidence and composition

`bin/verify.py` records what each authority established and binds it to the
repository state it describes. The binding is the point: it is what makes a PASS
expire when the code changes, and what stops evidence about two different trees
from being added together.

```sh
python "$VERIFY" run [--gate NAME=COMMAND]... [--output PATH]
python "$VERIFY" attest --rung NAME --note TEXT [--output PATH]
python "$VERIFY" import-ci --run RUN.json --job JOB.json [--expect-head SHA] [--output PATH]
python "$VERIFY" compose PATH... [--output PATH]
python "$VERIFY" check PATH
```

`$VERIFY` is `bin/verify.py` beside this skill. Every command takes `--repo`
(default: the working directory) so one installed copy serves every project.

Exit codes: `0` PASS or COMPLETE, `1` FAIL or INCOMPLETE, `2` BLOCKED or STALE.

## The three authorities

| Authority | Establishes | Kind |
|-----------|-------------|------|
| `local` | What this checkout can execute, per the policy's `[gates.local]` | execution |
| `ci` | What only CI can execute - container builds, scanners needing pinned binaries, anything wanting a runtime this environment lacks | execution |
| `agent` | The judgment rungs: task, diff, self-review, requirements, architecture, fresh-review | attestation |

An attestation records that a rung was climbed and held. It is never evidence
that a gate ran, and `compose` refuses to let one satisfy a required gate.

## The record

```json
{
  "schema": "verification.ladder.evidence/2",
  "recorded_at": "2026-09-19T22:14:07Z",
  "authority": "local",
  "repository": {"head": "950fcdc...", "branch": "main", "state_id": "sha256:...", "dirty": false},
  "gate_set": "default",
  "gates": [
    {"name": "lint", "kind": "execution", "command": "uv run --frozen ruff check .",
     "status": "PASS", "exit_code": 0, "output_tail": "All checks passed!\n", "duration_s": 0.9}
  ],
  "drift": false,
  "verdict": "PASS"
}
```

`state_id` is a digest over HEAD plus every deviation from it - staged, unstaged
and untracked content alike. Two checkouts share a state id only if they would
verify identically - it does not cover the index, and two trees whose
`git diff --cached` differs can share one. Ignored paths are invisible to `git status` and so do not
perturb it, and a record excludes itself.

A CI record carries `source` (run id, event, conclusion, URL) instead of a gate
set, and its `state_id` is the digest a pristine checkout of that commit yields. That is why CI evidence composes with a local
record only when the local tree is clean at the same commit: a dirty tree is not
what CI verified.

## Verdicts

| Verdict | Meaning |
|---------|---------|
| PASS | Every gate in this record ran and passed, over one unchanging state |
| FAIL | A gate ran and failed. Its `output_tail` holds the failure |
| BLOCKED | A gate could not run, the gate set was empty, or the tree changed while the gates ran (`drift`) |
| STALE | `check` or `compose`: the evidence's state is not the current state |
| COMPLETE / INCOMPLETE | `compose` only: the completion predicate over all authorities |

BLOCKED is the load-bearing verdict. A gate that could not run has established
nothing, and a green gate over a moving tree has established nothing about
either state. Neither is allowed to read as PASS.

## Importing CI

The workflow uploads nothing. Its steps are already named gates with
conclusions, and the run payload carries the authoritative commit, so the agent
fetches the run and its job with its GitHub tools and normalizes them:

```sh
python "$VERIFY" import-ci \
    --run .verification/import/github-run.json \
    --job .verification/import/github-job.json \
    --output .verification/ci.json
```

- The gate-to-step map lives in the policy under `[ci_steps]`, so a renamed
  workflow step is a reviewed change rather than a silently missing gate.
- Several gates may rest on one step, when that step runs several checks. A
  failed step establishes none of them.
- A step that was skipped, cancelled or is absent from the job is BLOCKED.
- Two things must hold, and `import-ci` checks both. The run must **name** the
  commit under verification - a run whose `head_sha` is another commit is
  refused. The run must also have **executed** that commit: a `pull_request` run
  checks out a synthetic merge of head into base, so its results describe that
  merge. Only the events in `ci_head_events` (`push`, by default) check out the
  tip itself; anything else is refused, naming the run to import instead.
  Checking only the first would stamp merge-tree results with the head tree's
  state id - true while the branch is up to date with its base, and quietly
  false the moment it is not.

Uploading a record from inside the workflow would be the other way to do this.
It is deliberately not built: it needs a workflow change and a wider permission
for evidence the Actions API already holds, and the artifact's own provenance
would then need establishing too. If a gate ever needs more than a step
conclusion - counts, timings, output - that is when to revisit it.

## Composing

```sh
python "$VERIFY" compose .verification/local.json .verification/ci.json \
    .verification/attestations.json --output .verification/composite.json
```

```
VERIFICATION STATE: 950fcdccfb58 + sha256:bcdd250c...

  * lint               PASS    execution    local, ci run 35472429403
  * tests              PASS    execution    local, ci run 35472429403
  * container-build    PASS    execution    ci run 35472429403
    diff               PASS    attestation  agent
  --------------------------------------------------------------
  BLOCKED REQUIRED GATES   0
  STALE EVIDENCE           0
  STATE MATCH              TRUE
  CLEAN CLIMB              TRUE
  --------------------------------------------------------------
  READY FOR HUMAN GATE     TRUE
```

Rows marked `*` are required. The predicate holds when:

- every required gate has an `execution` row with status PASS;
- every judgment rung has an attestation for this state;
- no row is FAIL or BLOCKED;
- every record binds to the same state, and that state is the current checkout.

Two authorities that disagree about a gate do not average out - the row becomes
BLOCKED, because something ran twice and told two stories.

## Policy

`verification.toml` at the repository root, or the same tables under
`[tool.verification]` in `pyproject.toml` for a project that prefers one config
file. `templates/verification.toml` is a filled-in starting point.

```toml
required_gates = ["lint", "tests", "container-build"]
judgment_rungs = ["task", "diff", "self-review", "requirements", "architecture", "fresh-review"]
ci_head_events = ["push"]

[gates.local]
lint = "ruff check ."
tests = "pytest -q"

[ci_steps]
container-build = "Run docker build -t app:ci ."
```

The policy is committed, so what "complete" means is reviewed like any other
change rather than being whatever the agent running today decided to check.

**A repository with no policy is BLOCKED, not verified.** `verify.py` refuses to
run, compose or import against one, and says so. Silence is not consent: an
unconfigured project must never read as a passing one. Writing the policy is an
onboarding task with its own review, not something to improvise mid-change.

## Using a record

- After the gates, before writing the report: `run`, then `attest` each judgment
  rung, then `compose`.
- After any repair: every earlier record is stale by construction, and the
  attestation file is discarded wholesale. Look at the diff again; do not carry
  "I reviewed this" across a change to what you reviewed.
- Before claiming completion: `compose` must exit 0.
- In the report: quote the verdict, the state, and which authority established
  each row. Do not paste the whole composite, and do not commit it - records are
  git-ignored operational evidence, and their captured output can carry local
  paths that do not belong in a published tree.

## Waivers

A required gate that cannot run here is not waived; it stays BLOCKED until an
authority that can run it does. There is no field for waiving a gate, and that
is deliberate - the only ways to clear one are to run it, to import a run that
did, or to change the committed policy in a reviewed change.
