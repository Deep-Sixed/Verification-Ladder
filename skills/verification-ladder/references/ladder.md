# The Verification Ladder, rung by rung

Each rung states its question, how to answer it, what counts as evidence, and
what a finding at that rung means. Climb in order; a rung is only attempted once
the rungs beneath it hold.

The commands are the project's, not this document's. `verification.toml` (or
`[tool.verification]` in `pyproject.toml`) names the gates under `[gates.local]`
and maps each CI-only gate to the workflow step that establishes it. Read that
file first: it is the only place that knows how this project is built and
checked.

## Rung 0 - Baseline

**Question.** What exact state am I verifying, and was it green before I touched it?

```sh
git rev-parse HEAD && git status --porcelain=v1 --untracked-files=all
python "$VERIFY" run --output .verification/baseline.json   # on a cold checkout
```

A pre-existing failure is not yours to hide or to silently inherit. Record it
before you start, keep it out of your own verdict, and say in the report that it
was red on arrival.

**Evidence.** The SHA, the dirty/clean state, and the baseline result.

## Rung 1 - Task

**Question.** Did I implement what was actually requested?

Re-read the request itself - not your summary of it, not the plan you wrote
after reading it. Enumerate the deliverables it names. Name anything you
substituted, narrowed or deferred. Where the request was about *this repository*
rather than about a topic, check that too: implementing a discussion in whatever
checkout happened to be selected is a rung-1 failure, not a rung-3 one.

**Finding here** means the change is aimed at the wrong target; repair before
spending effort on lower-cost rungs.

## Rung 2 - Mechanical

**Question.** Do the project's gates pass?

```sh
python "$VERIFY" run --output .verification/local.json
```

The gates come from `[gates.local]`. Gates the policy requires but this
environment cannot run - a container build with no runtime, a scanner that needs
a pinned binary - are not omitted and not assumed: they stay BLOCKED until CI
runs them and `import-ci` brings the result back.

**Evidence.** The record. Read the output, do not assume it: a suite that
collected 0 tests is not a suite that passed.

## Rung 3 - Diff

**Question.** Did I change only what I intended?

```sh
git diff --stat && git diff
git status --porcelain=v1 --untracked-files=all    # untracked files ship too
```

Read every hunk. Look for debugging leftovers, commented-out code, unrelated
reformatting, a dependency added for one call, an accidentally widened
interface, and anything that belongs to a different task.

Check the project's own publication rules while you are here - secrets, internal
hostnames, personal identity, private deployment paths. Whatever the project
scans for after the fact is cheaper to catch now.

## Rung 4 - Self-review

**Question.** Is the implementation correct, not merely green?

Review as though the diff arrived from someone else: error paths, boundary
values, empty and absent inputs, concurrency, partial failure, and what happens
on the second call. Ask what input would make this code wrong, then check
whether anything prevents that input.

Tests passing is evidence that the tests passed. It is not evidence that the
behavior is right - the tests may encode the same misunderstanding as the code.

## Rung 5 - Requirements

**Question.** Is every part of the request satisfied?

Trace requirement by requirement: for each one, the file and line that satisfies
it and the check that demonstrates it. An untraceable requirement is an open
finding, whether or not you believe it is met.

State assumptions you made where the request was ambiguous. An assumption stated
in the report is a decision the operator can overturn; an assumption left silent
is a defect waiting to surface.

## Rung 6 - Regression

**Question.** Did I break anything outside the change?

Run the whole suite, never only the file you touched. Consider callers of every
signature you changed, persisted state and migrations, shipped configuration,
and behavior guaranteed in the project's own documentation. Narrowing a test
selection is for diagnosis; it is never the basis of a verdict.

## Rung 7 - Architecture

**Question.** Does this stay consistent with the surrounding system?

Check the change against the invariants the project already states, in its
architecture and security documents and in the shape of the code around it. If
the change makes a documented statement false, the documentation is part of the
change, not a follow-up.

## Rung 8 - Evidence

**Question.** Can every claim I am about to make be substantiated?

Take each sentence you intend to write - "tests pass", "the build works", "this
fixes the bug" - and name the artifact that supports it. A claim with no
artifact gets deleted or demoted to "not verified".

Then assemble the artifacts into one predicate. Evidence comes from three
authorities, and composition is what turns them into a single answer instead of
three reports someone reconciles by reading:

```sh
python "$VERIFY" run --output .verification/local.json
python "$VERIFY" attest --rung diff --note "read every hunk; nothing unrelated"
python "$VERIFY" compose .verification/local.json .verification/attestations.json
```

`compose` exits 1 while a required gate is missing, and gates this checkout
cannot run stay missing until CI has run them. Push, let the workflow finish,
then bring its result back:

```sh
# fetch the run that CHECKED OUT this commit - on GitHub that is the push run -
# and its job, writing the payloads to disk
python "$VERIFY" import-ci --run run.json --job job.json --output .verification/ci.json
python "$VERIFY" compose .verification/local.json .verification/ci.json \
    .verification/attestations.json --output .verification/composite.json
```

`import-ci` refuses a run that names a different commit, and also one that did
not execute this one: a `pull_request` run checks out a synthetic merge of head
into base, and evidence about a merge nobody will land is not evidence about the
branch under review.

To re-bind a single record without composing, `verify.py check <record>` exits 2
once the state has moved.

## Rung 9 - Fresh reviewer

**Question.** Meeting this diff cold, with no memory of writing it, what would I
object to?

Drop every assumption you have been carrying since the first rung. Read the
final diff top to bottom. Ask what the most skeptical reviewer would say, then
answer honestly. This rung most often catches: a requirement quietly dropped
three repairs ago, a test that asserts the implementation rather than the
behavior, a name that no longer matches what the thing does, and a claim in the
report that the evidence does not actually support.

A finding here re-enters the ladder like any other. Two clean passes in a row
are not required; one clean pass at rung 9, over a state that has not moved
since rung 2, is.
