---
name: verification-ladder
description: Verify your own work before reporting it complete. Use after any change to repository state - implementation, bug fix, refactor, dependency bump, config or documentation edit - and again after every repair, to climb the verification ladder (baseline, task, mechanical gates, diff, self-review, requirements, regression, architecture, evidence, fresh-reviewer) and produce a state-bound evidence record. Required before claiming a task is done, tests pass, or a build works, in any repository.
---

# The Verification Ladder

Verification is the activity that produces objective evidence that the work
conforms to what was asked. This skill is the procedure. It is not the evidence:
an instruction that says "verify the build" is not proof that the build was
verified. Only a gate that ran against a known repository state is.

Two rules carry everything else:

1. **A verification result belongs to one repository state.** Change the state
   and every earlier result is stale. Repair is a state change.
2. **PASS is a claim about evidence you hold.** Absent the evidence, the verdict
   is BLOCKED, not PASS.

The procedure is the same in every repository. What counts as verified is not:
each project declares that in its own committed policy, which this skill reads.

## When to enter

Enter when the work is implemented and before you report on it, and re-enter
after every repair. Entering costs a few minutes; a false completion costs a
review cycle and the operator's trust in every other claim you make.

Do not enter for read-only work - questions, explanations, exploration - that
leaves repository state untouched.

## Setup

`bin/verify.py`, beside this file, is the machinery. Note its path once:

```sh
VERIFY="<this skill's base directory>/bin/verify.py"
python "$VERIFY" --repo . run --output .verification/local.json
```

It reads the repository's policy from `verification.toml`, or from
`[tool.verification]` in `pyproject.toml`. **A repository with no policy is
BLOCKED, not verified** - silence is not consent. If the project has none, that
is an onboarding task: copy the Ladder's `templates/verification.toml`, fill in
what this project requires, and propose it as its own reviewed change. Do not
invent a policy in passing and do not proceed as though the absence were a pass.

## The ladder

Climb in order. Each rung asks a broader question than the one below it, and a
rung is not attempted until the rungs beneath it hold. `references/ladder.md`
has the per-rung procedure.

Before any of them, capture the **baseline**: what exact state am I verifying,
and was it green before I started? HEAD, worktree status and a gate run on the
cold checkout. It is a precondition and not a rung, because it has to be taken
before the change exists.

| Rung | Question | Primary evidence |
|------|----------|------------------|
| 1 Task | Did I implement what was actually requested? | The request, read again, against the diff |
| 2 Mechanical | Do the project's gates pass? | `verify.py run` record |
| 3 Diff | Did I change only what I intended? | The full diff, read |
| 4 Self-review | Is the implementation correct, not merely green? | Reasoned review of each hunk |
| 5 Requirements | Is every part of the request satisfied, including the parts I found inconvenient? | Requirement-by-requirement trace |
| 6 Regression | Did I break anything outside the change? | The whole suite, not the subset I touched |
| 7 Architecture | Does this stay consistent with the surrounding system? | The project's own stated invariants |
| 8 Evidence | Can every claim I am about to make be substantiated? | Composite over local, CI and attested evidence, one state |
| 9 Fresh reviewer | Meeting this diff cold, what would I object to? | Adversarial pass, assumptions dropped |

## Findings, repair, re-entry

A finding is anything that would make a reviewer ask for a change: a failure, a
gap against the request, a bug you can see, a stale comment, a missing test.

```
finding → repair → RE-ENTER at the lowest rung the repair invalidated → continue
```

Never `finding → repair → assume fixed → continue`. A repair is new, unverified
work; it gets the same treatment as the original change.

| The repair | Re-enter at |
|------------|-------------|
| Any source, config, dependency or test change | Rung 2, and climb again |
| Comment, docstring or documentation only | Rung 3 |
| Reverting to a previously verified state | Rung 2 (the toolchain may have moved) |

Record each finding and its disposition as you go. A finding you fixed and a
finding you decided to leave are both reportable; a finding you quietly dropped
is a defect in the verification, not just in the code.

## Termination

The loop must end, and it must not end by fatigue.

- Same finding survives two repair attempts → stop repairing. Report BLOCKED
  with what you tried and what you observed.
- A gate cannot run here (no network, no container runtime, no credential) →
  that gate is BLOCKED, never PASS, and it stays BLOCKED until an authority that
  can run it does.
- A finding needs a decision that is not yours (product behavior, a breaking
  interface change, anything destructive or outward-facing) → stop and ask.
- Rungs 1-9 hold over the captured baseline, with no open findings → the
  composite reads `CLEAN CLIMB TRUE` and `READY FOR HUMAN GATE TRUE`. That is
  readiness for a person to accept, not acceptance.

Never reach for green by weakening the check: do not skip, `xfail`, delete or
loosen a test, do not widen a lint ignore, do not narrow a test selection to the
part that passes. If a test is wrong, say so explicitly and fix it as its own
finding with its own reasoning.

## Evidence

Three authorities can establish something, and they establish different things.
`references/evidence.md` has the records, the predicate and the exit codes.

```sh
# what this checkout can execute, per the project's [gates.local]
python "$VERIFY" run --output .verification/local.json

# what only CI can execute: fetch the run that CHECKED OUT this commit.
# Payloads go under .verification/import/ - a raw payload written into the
# checkout would change the state under verification, and one written to
# .verification/ directly would be globbed into compose as if it were evidence.
python "$VERIFY" import-ci \
    --run .verification/import/github-run.json \
    --job .verification/import/github-job.json \
    --output .verification/ci.json

# what no machine can execute - the judgment rungs, one line each
python "$VERIFY" attest --rung diff --note "read every hunk; nothing unrelated"

# the predicate over all of it
python "$VERIFY" compose .verification/*.json   # 0 COMPLETE, 1 INCOMPLETE, 2 STALE
```

An attestation is not an execution and never satisfies a required gate. Saying
"I reviewed the diff" is evidence about your attention, not about the code; the
composite prints the distinction rather than hiding it.

Records are operational evidence, not repository content: keep them under
`.verification/` and quote from them in the report instead of committing them.
That directory must be git-ignored, along with whatever caches the project's
gates write - a gate whose own output lands in the tree moves the state it was
measuring, and the run reports BLOCKED on drift. `INSTALL.md` lists the set for
the shipped template.

## Completion gate

"Complete" is a computed predicate, not a feeling of being done. `compose`
prints it, and it holds only when every required gate passed **by execution**,
every judgment rung is attested for this state, no finding is open, and every
record still binds to the checkout in front of you:

```
  BLOCKED REQUIRED GATES   0
  STALE EVIDENCE           0
  STATE MATCH              TRUE
  CLEAN CLIMB              TRUE
  --------------------------------------------------------------
  READY FOR HUMAN GATE     TRUE
```

Anything else is not complete. Do not restate the missing rows as prose and call
it done: name what is missing and what would establish it.

A required gate this checkout cannot run stays BLOCKED until CI has run it and
`import-ci` brings that result back. That is a real wait, not a formality -
push, let CI run, import, compose again.

## Report

Keep it short and falsifiable. State the verdict, the state it binds to, which
authority established each row, what was found, and what is still open.

```
VERDICT: COMPLETE | INCOMPLETE | BLOCKED
STATE:   <sha> (<branch>), worktree clean | dirty
GATES:   <gate> PASS local · <gate> PASS ci run <id>
ATTESTED: task, diff, self-review, requirements, architecture, fresh-review
FOUND:   <finding> → <repair> → re-verified at rung <n>
OPEN:    <anything unresolved, or "none">
```

"Tests pass" and "CI says tests pass" are different claims, and only one of them
survives the checkout being wrong. Never report a gate you did not run, a count
you did not read, or a pass you inferred from a previous run. Read every number
out of the record at the moment you write it: a count carried across a state
change is exactly the failure this procedure exists to prevent.
