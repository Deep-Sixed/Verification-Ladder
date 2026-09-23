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

## Which contract decides

Two are implemented, and the project chooses. `verification.ladder.evidence/2`
is the default and is everything above. `verification.ladder.evidence/3` adds
the target model, the definition governance and the provenance chain described
in `docs/design/evidence-v3.md`.

Adopting evidence/3 takes **two** things, and neither alone is enough.

```toml
# verification.toml - committed, reviewed, and therefore durable
evidence = "verification.ladder.evidence/3"
```

```sh
python "$VERIFY" qualify .verification/*.json   # did this evidence reach every predicate?
python "$VERIFY" activate .verification/*.json  # writes .verification/authority.json
```

The policy records what the project adopted. The activation record records that
the qualified path actually ran in **this checkout**, and is deliberately not
committed - qualification is a property of a working copy, not of a commit. The
two are checked against each other on every command:

| policy | activation record | result |
|---|---|---|
| silent | absent | evidence/2, the default |
| adopts evidence/3 | present and valid | evidence/3 decides |
| adopts evidence/3 | absent | the migration window: records are produced and marked pre-activation, `compose` is BLOCKED |
| silent | present | BLOCKED - that activation is lost on `rm -rf .verification` or a fresh clone |
| either | present and damaged | BLOCKED - a parse failure must not select weaker semantics |

The last two rows are the point. A contract that lives only in a git-ignored
directory is a property of one working copy, and a declaration that fails to
parse used to read exactly like a repository that had never activated.

Before activation, the CLI writes an evidence/3 **shadow** beside each record,
built from the same execution rather than a second one, under
`.verification/shadow/<name>.v3.json`. Nothing reads it to decide anything, and
`compose .verification/*.json` cannot reach it.

A repository describes how its executions read under evidence/3 in a `[shadow]`
policy layer. Before activation that layer decides nothing: it cannot run a
command, satisfy a required gate, or change a verdict or exit code, and a gate
only it declares stays unpaired rather than becoming executable. After
activation it is how gates are read.

### What an activation covers

`qualify` asks whether this evidence set reached every predicate evidence/3
enforces. Some predicates a project cannot reach: a repository with no CI gate
never exercises the provenance chain, and one with no behavioural gate never
exercises the artifact chain. Those read `OUT OF SCOPE` rather than `UNCOVERED`
— a fact about the policy, not a gap in the evidence — and they do not block
activation.

The activation record keeps both lists. Adding a CI gate to a project that
activated without one is then reported at composition: that activation never
exercised the provenance chain and cannot vouch for evidence that now needs it.
Re-run `qualify` and `activate`.

Three commands work on that layer, and none of them decides anything:

```sh
python "$VERIFY" baseline                     # what the gates mean, before the task
python "$VERIFY" run --output .verification/local.json
python "$VERIFY" attest --rung verify-login --note "watched the journey" \
    --execution .verification/local.json      # binds the judgment to what it judged
python "$VERIFY" compare .verification/local.json \
    --attestations .verification/attestations.json
python "$VERIFY" qualify .verification/local.json .verification/ci.json \
    .verification/attestations.json
```

`compare` reports, per predicate, whether the evidence/3 reading of an execution
agrees with the execution, could not be compared, or does not apply. `qualify`
asks the other question: did anything in this evidence set reach each predicate
at all? A predicate nobody evaluated reads exactly like one that passed unless
the report keeps them apart, so `qualify` has a fourth outcome, `UNCOVERED`.

`--execution` on `attest` affects the shadow alone; the authoritative
attestation is byte-identical with it and without it.

A `baseline` is never a prerequisite for `compare` or `qualify`: its absence
reports `N/A` and changes nothing there. Under evidence/3 it **is** a
prerequisite for `compose` and for `activate`, because the definitions that
govern a task have to be captured before the task changes them. Take it first,
keep `.verification/shadow/baseline.v3.json` with the evidence, and re-take it
when the task legitimately changes what a gate means - the composite then reads
`DEFINITION CHANGE PENDING` rather than a clean climb, which is the human gate
doing its job.

`check` re-binds one record to the checkout in front of you under either
contract. For an evidence/3 record it re-binds every dimension the record binds
to, and always the repository, so a record that still describes this worktree
but names a runtime that has since been replaced reads STALE, and says which
dimension moved - `repository.worktree_state` rather than `repository`, in the
same vocabulary `run` uses for drift. A CI record binds the repository alone,
since a clean checkout of one commit is all CI establishes, so a declared
runtime cannot expire it.

See `docs/design/evidence-v3.md` §M for what each stage must establish before
the next, and §M4 for why the authority switch is its own review point.
