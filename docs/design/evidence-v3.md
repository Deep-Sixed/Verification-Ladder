# Evidence v3 — admissibility as a single invariant

**Status: the model is implemented and is not authoritative.** This note fixed
the shape of `verification.ladder.evidence/3` so the enforcement work could be
written against a settled schema rather than discovering one. Items 1-14 of the
series below have landed. Records on disk are still `evidence/2`, `compose`
still applies v2 rules, and every verdict and exit code the CLI produces is
still v2's; evidence/3 is emitted beside them, read by `compare` and `qualify`,
and decides nothing. §M4 — the authority switch — has not happened and is its
own review point.

## The problem

`verification.ladder.evidence/2` binds a gate result to three things: a gate
**name**, an `execution`/`attestation` **kind**, and a git-derived
**`state_id`**. That was enough to express the first version of the model. It is
not enough to make the completion predicate defensible.

Review of PR #1 established four holes by execution, not by reading:

1. A custom `--gate` command satisfies a policy-required gate that bears no
   relation to it. A CI-only gate is satisfiable by a local no-op.
2. An `execution` row named `diff` satisfies the `diff` judgment rung.
3. CI run and job payloads are accepted without proving they belong together.
4. Evidence does not identify the Ladder implementation that produced it.

Combined, they yield `VERIFICATION COMPLETE TRUE` and exit 0 on a repository
where no gate and no judgment ever occurred.

Comparison with a project-local behavioural verification skill exposes a fifth
dimension the model has no vocabulary for: verification can depend on a
**running target**, and can rest on **proof artifacts whose interpretation is
itself a separate judgment**.

These are not five defects. They are one missing invariant:

> **Evidence is admissible only when the verification definition, the permitted
> authority, the execution provenance, the target state, the proof artifacts,
> and the verifier identity all refer to the same thing.**

Any mismatch fails closed.

## Layering

```
VERIFICATION LADDER          generic procedure, evidence identity, composition
        |
        v
PROJECT POLICY               what must be established
        |
        v
PROJECT VERIFICATION SKILL   how project-specific behaviour is exercised
        |
        v
RUNTIME TARGET               the application or distributed system
        |
        v
PROOF ARTIFACTS              recordings, assertions, logs, traces
        |
        v
HUMAN GATE
```

The Ladder does not learn to click a login button, traverse a desktop
application, or route a distributed request. A project-local verification skill
owns that. The Ladder owns the meta-question: *does the evidence in hand
establish the claim being made, against the state under review, under the
definition that was in force?*

This also preserves token economy. The generic Ladder may auto-trigger before
completion; a large project verification skill must not. A project skill
carrying the equivalent of `disable-model-invocation: true` fits this
architecture exactly — the Ladder reaches a required behavioural gate, resolves
the project-owned procedure, and invokes it deliberately.

---

## A. Schema first

The previous patch order put gate-definition binding first and the schema bump
fifth, while arguing for doing the structural work once. Those contradict. Gate
identity, CI provenance, verifier identity, baseline-policy identity, runtime
state and artifacts **all require new fields**:

| Change | New field(s) | Implementable on v2 |
|---|---|---|
| Gate-definition binding | `definition_sha256`, `permitted_authorities` | No |
| CI provenance chain | `source.{repository,run_attempt,workflow,job,step}` | No |
| Verifier provenance | `verifier` | No |
| Governing-policy binding | `verification_spec.policy_sha256` | No |
| Runtime state | `target_state.runtime` | No |
| Artifact binding | `artifacts`, `artifact_refs` | No |
| Symmetric kind enforcement | — (composer logic only) | Yes |

Only symmetric enforcement is implementable against v2. Landing it first would
mean either a v2.5 nobody wants or writing the rest twice.

**Decision: define `verification.ladder.evidence/3` first, then implement the
rules against it.** The execution-vs-attestation composer bug is independently
fixable and stays in the implementation series that follows this note's review,
not ahead of it.

## B. Verification-specification identity

Moving behavioural semantics into a project-local skill does not close the
self-weakening problem; it relocates it. A gate named `verify-login` may draw
its meaning from `verification.toml`, a project skill, a feature-map file, a
driver configuration, or a referenced journey. If the specification changes from
*exercise login success, error, persistence and logout* to *open the login
screen*, the gate no longer means what it meant, though its name is unchanged.

Three mechanisms were considered and two are rejected:

- **Agent enumerates the sources** — rejected. An agent that weakened a file
  omits it from the list. The hole is unchanged.
- **Hash the whole specification directory** — rejected. An unrelated
  documentation fix would invalidate every behavioural record in flight.
  Operationally intolerable, and people route around instruments that cry wolf.
- **Bounded, project-owned declaration** — adopted.

**Decision.** The policy declares, per behavioural gate, a **specification root**
and the **specification files** that define it. Every recorded specification
file must resolve beneath the declared root; one that does not fails closed.
The gate's `definition_sha256` is computed over the gate's declared
configuration plus the contents of those declared files, in declared order.

**On observing transitive reads, stated plainly:** the current model is Markdown
read by an agent. There is no reliable way to observe which files an agent
actually consulted, and this note does not pretend otherwise. Therefore **the
declared specification files are the authoritative definition.** Incidental
files an agent happened to read are not part of gate identity. A driver that can
report its own reads may record them as `observed_sources` for diagnostics, but
they never contribute to `definition_sha256`.

The cost is honest and should be stated in the template: a behavioural
specification that matters must be *declared*, not merely adjacent.

## C. The artifact chain

Section 9 below establishes that behavioural proof does not need a third
evidence kind. Two further rules are required, and neither is optional.

**C1. Digests are recomputed from disk at compose time.** Comparing a digest
recorded in an execution record against a digest recorded in an attestation is
two strings agreeing with each other; an agent that writes both records passes.
The composer must hash the actual artifact file. A referenced artifact that is
missing or whose recomputed digest differs fails closed.

**C2. The chain must be complete and unbroken:**

```
attestation --> artifact digest --> execution record --> target_state --> current target
```

The execution record an attestation references must be **present in the same
composition** and must bind to the **same `target_state`**. Without this, an
agent can drive the path, capture artifacts, attest over them, then change the
code: the artifacts still sit on disk with unchanged digests and the attestation
still references them validly. Staleness must propagate through the reference,
not stop at the execution record.

Any broken link in that chain fails closed.

## D. `BLOCKED` and state mismatch are different diagnoses

Environment facts become part of `target_state` (section 4), so a stale build
produces a *different target*, not a failed gate. That mechanism presupposes the
runtime can be interrogated. When it cannot — the application is not running,
the CDP port is dead, the cluster is unreachable — there is no manifest to hash
and no target to differ from.

**Decision.**

- Runtime reachable but different → **`target_state` mismatch / STALE**.
  *Your evidence does not apply to this target.*
- Runtime not interrogable at all → **`BLOCKED`**.
  *You have no evidence.*

These must never collapse into one diagnosis. They have different remedies, and
conflating them tells the reader to re-run when they need to start a server.

## E. Verifier compatibility and installation

"Compatible verifier provenance" cannot remain undefined; it is load-bearing in
the completion rules. Exact-match invalidates every in-flight record on each
`git pull`. Anything looser needs a stated policy.

**Decision.**

- Evidence records carry **exact** verifier identity: schema, version, the
  Verification-Ladder commit, and the `verify.py` digest.
- Composition requires the **same evidence-schema major / compatibility
  contract**. Records differing only in patch-level verifier version compose;
  records across a compatibility-contract change do not.
- A release that changes evidence **semantics** requires re-verification, and
  says so in its notes.

**This forces a correction to INSTALL.md.** It currently recommends one shared
clone with symlinks *specifically so* that `git pull` updates every agent at
once. Under verifier binding, that recommendation makes the verifier a moving
part underneath the evidence. The shared clone and symlinks remain correct; the
clone should normally sit on a **pinned release tag** and be upgraded
deliberately. Reproducible verification is not compatible with tracking a
mutable `main`.

## F. Baseline is a distinct record

Baseline binds to the **pre-mutation** state. By construction it can never match
the post-mutation `target_state`, so it cannot be an ordinary composable row,
and it must not be an attestation — attestations are discarded wholesale when
state moves, which is exactly the wrong behaviour for a baseline.

**Decision.** Introduce `verification.ladder.baseline/1`, recording:

- the pre-mutation repository state,
- the governing policy and specification identity captured at that moment,
- the baseline gate result.

The composite **references** the baseline record and reports it. It does not
require the baseline's target state to equal the verification target state.

## G. Deferred, and deliberately named

Four findings from the PR #1 review are not solved by evidence-v3. They are
recorded here so they do not quietly fall out of scope.

**Carried into the v3 implementation series:**

1. **`run --gate` must still load policy.** Today `--gate` bypasses `policy()`
   entirely, so an unconfigured repository emits a `PASS`-shaped record — in
   direct contradiction of the documented rule that a repository with no policy
   is BLOCKED. Custom gates are a diagnostic convenience; they never license a
   record from an ungoverned repository.
2. **Artifact paths under `.verification/` must be excluded from repository
   state by construction.** `command_run` currently excludes exactly one path,
   its own `--output`. Writing artifacts into the checkout during a run adds
   untracked files to the very state being measured. Today a consumer's
   `.gitignore` masks this; under v3 the exclusion must not depend on the
   consumer having remembered.

**Deferred beyond v3, tracked separately:**

3. **Gate timeout.** A hung gate hangs the ladder indefinitely; reproduced with
   a 30-second gate that ran until killed externally. A liveness bug, not an
   admissibility one.
4. **Command execution semantics.** `shlex.split` without a shell contradicts
   the template's "the commands a contributor runs locally, exactly as they
   would type them." Shell semantics, argv arrays, and POSIX bias are one
   separate decision. This note deliberately keeps commands as strings so as not
   to prejudge it.

## H. Findings

The composite currently prints `UNRESOLVED FINDINGS 0`. That is a claim the
program cannot support: those "findings" are derived from missing or failing
evidence, never from findings an agent discovered at the self-review,
requirements or architecture rungs. An agent that finds a real defect, attests
the rung anyway, and reads `UNRESOLVED FINDINGS 0` receives machine-printed
confirmation of something the machine never checked.

**Decision for v3.**

- **Remove** the `UNRESOLVED FINDINGS` instrument.
- Report **`CLEAN CLIMB`** — the property the composer can actually establish:
  every required gate established by permitted authority over one target, every
  judgment rung attested for it, every record admissible.
- Do **not** print `OPEN FINDINGS 0` until a real findings ledger exists.

A findings ledger may be a later feature. Consequently the adversarial test for
"a purported clean result while a tracked finding remains open" is **blocked on
that ledger and is not a v3 acceptance test.**

## I. Target-state projection

The worked example concludes that a moved runtime expires `verify-login` while
`lint` and CI `tests` survive. That conclusion is right, and it proves
`target_state matches` **cannot mean whole-record equality**. Lint has no
runtime; comparing one against the current runtime would expire it for a reason
that cannot affect it.

The prose alone does not give the composer a rule for reaching that result, and
the gap is exploitable: if runtime is optional-by-convention, an agent omits
`runtime` from a behavioural record and freshness silently becomes an assertion
the agent chooses to make.

**Decision.** Each gate **declares the target dimensions it depends on**, and
that declaration is part of the gate's identity.

```toml
[gates.local.lint]
command = "ruff check ."
target  = ["repository"]

[gates.behavioral.verify-login]
target  = ["repository", "runtime"]
```

Composition rules:

1. Evidence **must carry every dimension** the gate definition declares.
   A missing declared dimension is **inadmissible** — never "matched by
   default". This is the rule that closes the omission attack.
2. Dimensions the gate does not declare are **neither required nor compared**.
   Extra dimensions present in a record are ignored for that gate.
3. Each record is compared against the **current projection** of the target onto
   exactly the dimensions its gate declares.
4. An execution and its attestation, for one behavioural gate, must carry the
   **same projection**. A judgment attested against one runtime does not carry
   to another.
5. `target` is folded into `definition_sha256`. Dropping `runtime` from a gate's
   declaration is therefore a definition change that invalidates evidence
   produced under the old declaration, rather than a quiet weakening.
6. A gate declaring `runtime` when the runtime cannot be interrogated is
   **BLOCKED**, per decision D — not a mismatch, and not a pass.

**Consequence for the policy schema.** A gate can only declare a projection if
it is a table, so v3's policy uses per-gate tables for all three gate classes:
`[gates.local.<name>]`, `[gates.ci.<name>]` and `[gates.behavioral.<name>]`.
The v2 forms `[gates.local] name = "command"` and `[ci_steps] name = "step"`
cannot carry `target` and are replaced. The existing refusal of dotted gate
names still applies and matters more here, since the name is now a table key.

## J. The verifier compatibility contract

Decision E says records carry exact verifier identity and compose under "the
same compatibility contract". None of the fields proposed for the `verifier`
block actually **names** that contract, so the implementation would have been
asked to evaluate a concept absent from the record.

**Decision.** Add an explicit `compatibility` field.

```json
{
  "verifier": {
    "schema": "verification.ladder.evidence/3",
    "version": "0.2.0",
    "compatibility": "evidence-v3.1",
    "commit": "<verification-ladder commit>",
    "implementation_sha256": "sha256:..."
  }
}
```

- `commit` and `implementation_sha256` are **provenance**: which build produced
  this record. They are recorded, reported, and never used to decide
  composability.
- `compatibility` is the **contract**: composition requires every record in a
  composition to carry the same value.
- `schema` and `compatibility` are distinct. `schema` is the record's *shape*;
  `compatibility` is the *semantics of the rules applied to it*. A verifier can
  preserve the v3 shape while changing what admissible means, and that must
  invalidate earlier evidence.

A release that changes evidence semantics increments the contract and says so in
its notes; a patch or documentation change that preserves semantics retains it.
This makes "compatible verifier" and the INSTALL.md pinned-release rule
mechanically decidable rather than conventional.

## K. Execution identity for the artifact chain

Decision C requires the chain
`attestation → artifact digest → execution record → target_state`, but the
attestation as proposed carries only artifact digests. Digests identify *bytes*;
they do not identify **which execution record produced them**, so the composer
had no way to walk the second link.

**Decision.** An execution record carries a stable `record_id`, and an
attestation names it.

```json
{
  "kind": "execution",
  "name": "verify-login",
  "record_id": "sha256:E",
  "artifacts": [ { "kind": "recording", "sha256": "sha256:B" } ]
}
```

```json
{
  "kind": "attestation",
  "name": "verify-login",
  "execution_ref": "sha256:E",
  "artifact_refs": ["sha256:B", "sha256:C"]
}
```

`record_id` is the SHA-256 of the record's canonical form — UTF-8, keys sorted,
no insignificant whitespace — computed with the `record_id` field itself
excluded. It is **stored as a locator and recomputed at compose time**; a stored
value that does not match the recomputed one fails closed, on the same principle
as C1.

Composition then proves the whole chain:

```
attestation
    | execution_ref
    v
a specific execution record in this composition
    | artifacts
    v
artifact digests --> rehashed from the actual files
    |
    v
target projection --> current target
```

The referenced execution must be present in the composition, be of kind
`execution`, name the **same gate**, and carry the **same projection** (§I.4).
Artifact digests alone are never responsible for identifying their execution.

## L. Definition changes and bootstrap migration

Everything above defines what happens when a definition changes *accidentally or
maliciously*: evidence produced under the old meaning stops being admissible.
Nothing yet defines how a **legitimate** change to `verification.toml`, a
declared behavioural specification, or the machinery itself gets through.

That gap is not theoretical, and this PR is the first thing it would catch. The
v3 implementation changes the policy shape from the v2 tables to the v3 per-gate
tables (§I), so its baseline definition and its candidate definition necessarily
differ:

```
202bf35 baseline          uses v2 policy syntax
        |
implementation changes verifier + policy to v3
        |
baseline definition != candidate definition
        |
v3 correctly refuses silent substitution
```

**The first correct implementation of v3 rejects itself.** Without an explicit
governance path it would be forced to invent an exception, inside the system
built to eliminate invented exceptions.

### L1. A candidate definition never silently replaces the governing one

The baseline record captures the governing definition identity per gate. When an
input to `definition_sha256` changes during a task, **both identities are
preserved** rather than one overwriting the other:

```
governing_definition   <baseline digest>
candidate_definition   <post-change digest>
```

Evidence produced under the candidate is labelled `definition: candidate`. It
does **not** retroactively satisfy the governing definition merely because it
carries the same gate name. This is the §5 rule — a name is not an identity —
applied across time rather than across authority.

### L2. An intentional definition change is a governance change

A verification-definition change is its own task class, with its own terminal
state.

```
governing == candidate              governing != candidate
   |                                   |
CLEAN CLIMB                         candidate evidence may be collected
   |                                   |
READY FOR HUMAN GATE                DEFINITION CHANGE PENDING
   |                                   |
HUMAN GATE                          HUMAN GATE decides whether the
                                    candidate becomes governing
```

**`CLEAN CLIMB` must not print TRUE for a gate satisfied only under a changed
definition.** The honest machine output is *"I collected evidence under a
definition you have not yet accepted"*, and that is what
`DEFINITION CHANGE PENDING` says.

The human gate may accept the change. Only once accepted and merged does the
candidate become governing **for subsequent tasks**. A definition never governs
its own introduction.

### L3. Definition identity is gate-scoped

A definition change to `verify-login` must not invalidate `lint`. The composite
names exactly which required gates changed definition and leaves the rest
ordinary.

This requires a precision the note has so far left implicit, and L3 does not
hold without it: **`definition_sha256` is computed over the gate's own
declaration subtree and its declared specification files — never over the whole
policy file.** A shared `verification.toml` contributes to every gate's digest
if hashed wholesale, so editing one gate's command would change all of them and
collapse L3 immediately.

`policy_sha256` (the whole file) remains, and is a different instrument: it is
governance provenance, recorded and reported, not the per-gate admissibility
key.

### L4. Where practical, retain evidence under both definitions

When the governing definition is still runnable, collect both:

```
verify-login @ governing g1   PASS / FAIL
verify-login @ candidate  g2   PASS / FAIL
```

This is most useful when a behavioural specification is being **expanded**: the
old coverage still holds and the new coverage is additional. The candidate
result still never substitutes for the governing one automatically.

When the governing definition is intentionally no longer meaningful — the
feature it describes is gone — say so and leave the decision to the human gate.
**Do not invent a machine waiver.** A composer that can excuse a definition it
no longer likes is a composer that can excuse anything.

### L5. The v2 to v3 bootstrap

This repository cannot have captured a `verification.ladder.baseline/1` record
with a v3 verifier before the v3 verifier existed. That is a genuine
chicken-and-egg, and it is resolved by naming one immutable baseline rather than
by relaxing a rule.

**`202bf35` is the migration baseline.** A baseline record may be
*reconstructed* from an immutable committed state when every input is
reproducible:

- the exact commit `202bf35`;
- the policy and specification bytes as committed at that commit;
- push-CI evidence already held for that exact commit — run
  [35491283157](https://github.com/Deep-Sixed/Verification-Ladder/actions/runs/35491283157),
  `push` event;
- **no dirty pre-mutation state is reconstructed.** A reconstruction can only
  describe a clean committed tree; anything else would be fabricating a worktree
  nobody observed.

The reconstructed record carries `origin: "reconstructed"` so it stays visible in
every composite that references it, never laundered into looking captured. It is
valid for the migration task alone and is not a general mechanism.

After v3 exists, ordinary tasks capture baseline before mutation, as §F requires.

### L6. Policy syntax migration is a separate layer

Evidence schema compatibility and policy syntax compatibility are different
questions. **Do not conflate "evidence/v2 is incompatible" with "the v2 policy
file cannot be read."** The first is about records this verifier produced; the
second is about a project's committed configuration.

**Preferred: parse the v2 policy form as legacy input and normalize it to v3
defaults**, then emit only v3 evidence:

```
[gates.local] name = "cmd"   ->  authority local, target ["repository"],
                                 evidence_mode execution
[ci_steps]    name = "step"  ->  authority ci,    target ["repository"],
                                 evidence_mode execution
```

Normalization must be **deterministic**, and the normalized form is what feeds
`definition_sha256`, so that two readers of the same legacy file agree on gate
identity.

One limit is worth stating rather than discovering: a legacy policy **cannot
declare a behavioural gate**, because v2 has no syntax for one. Behavioural
gates require v3 policy. Legacy parsing is a migration affordance for existing
mechanical gates, not a second supported dialect.

**Refined by §M2a.** "Behavioural gates require v3 policy" turned out to mean
"behavioural gates cannot be exercised until v3 policy is authoritative", which
would have deferred their first real exercise to the authority switch. The
resolution is a third position between the two above: v3 policy is accepted in
a **shadow-only layer** that describes how an authoritative v2 execution reads
under evidence/3, and creates no authority of its own. Authoritative policy
remains v2 until M4.

The alternative — a documented one-time migration with no legacy parsing — is
acceptable, provided it does not pretend the new policy governed its own
introduction.

## M. Activation is staged, not a switch

Everything above describes what evidence/3 must establish. It says nothing about
how the verifier comes to apply it, and the series ended at item 11 as though
the last predicate landing were the same event as evidence/3 becoming
authoritative. It is not, and the gap was measured rather than argued.

A trace of every CLI subcommand after items 1-4 found **0 of 20 evidence/3
functions reachable**. The model is exercised by its tests and by nothing else.
A single authority switch would therefore make activation day the first time
that code runs in the product — against records produced by a path that has
never produced one — with the composer's verdict already resting on it.

**Decision.** Activation is four stages, and the last is its own review point.
A fifth section, §M2a, records a prerequisite that M2 forced into the open: the
policy layer the later stages need, accepted without authority.

### M1. Shadow emission

v2 stays authoritative. v3 is emitted beside it, from the same workflow and the
same run, so both representations describe one execution rather than two.

Shadow emission must not alter the authoritative result. It adds a record; it
changes no verdict, no exit code and no gate outcome. A v3 path that cannot
produce a record says so and leaves v2 untouched.

### M2. Comparison

Where v2 and v3 describe the same gate, their outcomes are compared, and any
disagreement is surfaced as a diagnostic.

A disagreement never silently changes the v2 result. It is a finding about the
v3 implementation, reported where someone will read it, and it fails the
activation criteria rather than the task in hand. The point of shadow mode is to
find those disagreements while they are still free.

### M2a. Shadow-only policy acceptance

M2 surfaced a circularity in this staging. The comparison could read no
behavioural gate, because a behavioural gate needs `[gates.behavioral.<name>]`
policy syntax, and the authoritative parser refuses that syntax for a good
reason: read as v2 it misdiagnoses (§L6). So `behavioural_status` and the
runtime half of `target_projection` — two of the predicates carrying the most
novel enforcement semantics in the model — were unreachable through the CLI,
and the artifact chain read `N/A` for every gate that could exist.

Deferring them to M4 would make the authority switch the first end-to-end
exercise of that feature, which is the exact condition shadow mode was
introduced to prevent. So the circularity is cut here rather than carried.

**Decision.** A repository may declare evidence/3 gates in a **shadow-only
policy layer**, under `[shadow]`. Those declarations are read by the shadow
path and by nothing else. This is not policy activation.

#### The authority boundary

A shadow declaration must not, by itself:

- execute a command;
- create an authoritative gate;
- satisfy an authoritative requirement;
- alter a v2 record, `compose`, the v2 verdict, the v2 exit code, or
  `READY FOR HUMAN GATE`.

The authoritative execution still comes from a v2-governed gate. What a shadow
declaration may do is say how **that same execution** is to be read under
evidence/3 — that `verify-login`, which `[gates.local]` actually executes, is a
behavioural gate producing artifacts that a judgment must bind to.

A shadow gate with no authoritative execution behind it does not thereby become
executable. It stays unpaired, and the comparison reports it as not comparable.

#### Parser separation

The boundary is structural rather than a flag. `[shadow]` is a separate subtree
with its own loading and normalization path; the authoritative parser never
reads it, and the refusal of v3 syntax in the authoritative `[gates.*]`
namespace stands exactly as before. Neither reader is weakened to accommodate
the other, because two parsers whose meanings are hard to tell apart is how a
shadow layer stops being one.

A shadow declaration **replaces** a gate's authoritative declarations in the
shadow view rather than merging into them. Merging would give a behavioural
gate two `local` declarations — a command and a driver — to be reconciled by
inference. Replacement keeps one answer to "what does this gate mean under
evidence/3", and keeps authority declared: a shadow gate has `ci` authority
only where `[shadow.gates.ci.<name>]` says so, never because it is behavioural.

A malformed shadow policy produces a **shadow diagnostic**. It does not block
the run, and it does not acquire authority by being unreadable — a layer that
could halt the task by being wrong would already be authoritative.

#### The runtime dimension, without executing anything

A behavioural gate declares `target = ["repository", "runtime"]`, and the
runtime dimension needs facts to bind to. A shadow probe that ran a command to
collect them would be a shadow declaration causing execution, which the
boundary forbids. So the runtime manifest is **read, never produced**:
`[shadow.runtime] facts_file` names a JSON file, and the four cases fall out of
whether that file is there and what it says.

| at record time | at comparison time | reading |
| --- | --- | --- |
| readable | same content | `ADMISSIBLE` |
| readable | different content | `STALE` — the runtime moved |
| readable | absent or unreadable | `BLOCKED` — no target to compare against |
| absent | any | `INADMISSIBLE` — evidence omits a declared dimension |

The third row is the distinction §D exists for, and it must not collapse into
the second: an uninterrogable runtime is absence of evidence, not a mismatch.

### M3. Qualification

**M3 is not "the tests are green."** It is evidence that everything intended to
become authoritative at M4 has already run through a real, non-authoritative
surface, under ordinary and adversarial conditions.

That sets the rule for the next boundary: **M4 may switch authority only over
semantics M3 has already exercised through a non-authoritative real surface.
Anything first exercised at M4 is not qualified, and is therefore not ready for
activation.**

#### The qualification matrix

Before M3 can be called complete, every evidence/3 predicate is classified:

1. **Must be exercised before M4** — it carries enforcement semantics nothing
   else carries.
2. **Aggregator only** — it composes predicates already exercised
   independently and adds no semantics of its own. This has to be *shown*, by
   demonstrating that the aggregate agrees with the conjunction of its parts
   across the cases that would distinguish them, not asserted on inspection.
3. **Not part of M4 authority** — with a concrete reason.

A class-1 predicate is never left unexercised on the grounds that its input
shape does not exist yet. If M4 will depend on it, M3 builds the
non-authoritative surface that exercises it first.

The predicates unreachable after M2 classify as:

| predicate | class | how M3 reaches it |
| --- | --- | --- |
| `behavioural_status` | 1 | shadow behavioural gate over a v2-executed command |
| `target_projection` | 1 | per-gate projection recorded in the shadow row |
| `runtime_identity` | 1 | `[shadow.runtime] facts_file` |
| `baseline_record` | 1 | non-authoritative baseline capture |
| `governing_definitions` | 1 | governance comparison against that baseline |
| `definition_change` | 1 | same |
| `governance_status` | 1 | same |
| `ci_step_status` | 1 | raw step conclusions preserved in the CI shadow |
| `gate_admissibility` | 2 | called by the comparator, and shown equal to its parts |

#### `attest` gets a shadow representation

Not for symmetry. M4 cannot make evidence/3 authoritative for a record class
that M3 never exercised through evidence/3, and judgment is a record class.

An attestation has no execution, and its shadow must not pretend otherwise.
Nothing is re-run to manufacture it: the shadow is derived from the
authoritative attestation already recorded, and that attestation is unchanged
by its existence.

The one thing a v2 attestation cannot supply is the binding §C requires —
*which* execution was judged, over *which* artifacts. That binding is named
explicitly rather than discovered by looking around for a plausible execution,
and naming it affects the shadow alone: the authoritative record is
byte-identical with and without it.

#### `ci_step_status` is in the M4 contract

`ci_provenance_status` establishes that a job belongs to this commit, run,
attempt, workflow and repository. It says nothing about what the step
concluded. `import-ci` reads that conclusion inline today; under evidence/3
`ci_step_status` reads it, and it is the predicate separating a step that
passed from one that was skipped, cancelled, unfinished or absent — which is
the difference between evidence and its absence.

So it is class 1, and the CI shadow preserves the **raw step conclusions** it
was given. The v3 predicate is then exercised against the same payload v2
derived its answer from, rather than against a summary of that answer, which
would only establish that the summary was copied correctly.

#### Baseline and governance are in the M4 contract

`DEFINITION CHANGE PENDING` is a terminal state (§L2), and a terminal state the
product has never reached is not qualified. So M3 introduces a baseline capture
surface.

It is non-authoritative in the strict sense: **a missing baseline is not an
error.** Composition never reads it, no verdict depends on it, and the
governance comparison reports `N/A` in its absence exactly as it did after M2.
Baseline capture must not become a prerequisite by the back door during M3;
whether it becomes one belongs to M4.

#### What qualification produces

M3 produces a **qualification report**, not authority. It distinguishes four
outcomes:

- **qualified** — the predicate was exercised, and evidence/3 read the
  execution as v2 did;
- **disagreement** — it was exercised and the two readings differ;
- **not comparable** — the records could not be paired, so nothing was
  established either way;
- **uncovered prerequisite** — the predicate is in the M4 contract and this
  evidence set exercised it not at all.

The last is the outcome M2 had no way to report, and it is the one that
matters: a predicate nobody evaluated reads exactly like one that passed unless
the report separates them.

A qualification failure may fail the qualification command. It must not rewrite
the authoritative result of the task underneath it, and M3 status is never
routed into the v2 composite or into `READY FOR HUMAN GATE`.

#### Coverage criteria

Qualification holds when all of this holds:

- the shadow path has run the ordinary end-to-end workflow — baseline, local
  gates, attestations, CI import, composition — not a contrived one;
- the adversarial cases gathered in item 11 have been exercised **through the
  shadow surfaces**, not only against the predicates directly, and for each one
  the surface that exercised it is named;
- every class-1 predicate has been exercised, and every class-2 aggregator
  shown equal to its parts;
- the two cases item 11 names and skips are resolved **once their prerequisites
  actually exist** — the findings ledger (decision H) and the end-to-end case
  needing evidence/3 to be authoritative. A skip is not resolved by being
  rewritten into something easier, and the skip count is not the measure;
- v2 and v3 agree on every compared gate, or each disagreement is understood
  and resolved.

A case that cannot be exercised leaves M3 incomplete, unless the feature is
explicitly removed from the M4 authority contract and the removal recorded
here.

**A green suite is not qualification.** The suite was green throughout items
1-11 while the product exercised none of that code, which is precisely the
condition this stage exists to end.

### M4. The authority switch

Only after M3 may evidence/3 become authoritative, and that change is its own
commit and its own review point — never a side effect of a stage that precedes
it, and never reached by implication because the preceding work looked
finished.

`VERSION` moves to `0.2.0` at that commit and not before (§J): the package
version should mean that evidence/3 is emitted and enforced, which is a claim
worth keeping accurate while the model is dormant behind a v2 CLI.

### Invariants that hold across all four stages

- The CLI emits `verification.ladder.evidence/2` and composes under v2 rules
  until M4. Items 1-11 are complete and none of them changed that.
- A behavioural gate gains CI authority only where the policy declares CI for
  it — `[gates.ci.<name>]`, or `[shadow.gates.ci.<name>]` in the shadow layer.
  It is never inferred from the gate being behavioural.
- The `[shadow]` policy layer (§M2a) creates no authority. It never executes a
  command, never satisfies a requirement, and never alters a v2 record, verdict
  or exit code. v3 syntax in the authoritative `[gates.*]` namespace is still
  refused.
- `VERSION` stays `0.1.0` until M4.
- §L5's migration baseline stays pinned at `202bf35`. Later commits in the
  migration task do not move the anchor; that is what naming an immutable one
  was for.
- Gate timeout and command execution semantics stay as deferred in §G3 and §G4.
  Shadow mode does not reopen them.

---

## The schema

### Target state

`state_id` over HEAD plus worktree deviations is too narrow in two independent
ways, both demonstrated:

- **Index collision.** Two checkouts with identical HEAD, identical status codes
  and identical worktree bytes but different staged contents produce byte-equal
  state ids while `git diff --cached` differs. Reproduced:
  `sha256:9a05ae7b...e78c` for both.
- **Runtime.** The same checkout connected to a different build, instance,
  feature-flag set or service baseline verifies differently.

```json
{
  "target_state": {
    "repository": {
      "head": "<sha>",
      "index_state": "sha256:...",
      "worktree_state": "sha256:..."
    },
    "runtime": {
      "manifest_sha256": "sha256:...",
      "facts": {
        "build": "...",
        "instance": "...",
        "feature_flags": ["..."]
      }
    }
  }
}
```

`runtime` is **optional** and absent for checks whose result cannot depend on a
running system. A lint gate has no runtime; a behavioural gate must have one.
For a distributed target the manifest resolves service identity to immutable
image digests rather than naming a single build.

The documentation must stop claiming that equal repository state means two
checkouts "would verify identically." The accurate claim: *`target_state`
identifies the code and the target system covered by the verifier.*

### Gate identity

A name is not an identity. Policy declares, per gate:

```json
{
  "gate": "tests-py311",
  "definition_sha256": "sha256:...",
  "permitted_authorities": ["ci"],
  "evidence_mode": "execution",
  "target": ["repository"]
}
```

Composition requires **all** of: name matches, definition digest matches,
authority is permitted, evidence kind is permitted. `--gate` remains useful for
diagnostics and for additional checks; it never satisfies a policy-required gate
by borrowing its name.

### Verification specification

```json
{
  "verification_spec": {
    "policy_sha256": "sha256:...",
    "gate_definition_sha256": "sha256:...",
    "spec_root": "verification/features",
    "sources": [
      { "path": "verification/features/sign-in.md", "sha256": "sha256:..." }
    ]
  }
}
```

Per decision B: `sources` are the **declared** specification files, each
required to resolve beneath `spec_root`.

### Two evidence kinds, and artifacts

Behavioural verification looks like it needs a third kind, because it combines
machine execution with interpretation. **It does not, and adding one would be a
mistake** — it would create a category in which an execution's authority and a
judgment's fallibility are silently blended.

```
behavioural execution
    |
    +-- production path exercised
    +-- recording  -> sha256:B
    +-- DOM proof  -> sha256:C
             |
             v
        attestation
        "B and C establish the required behaviour"
```

The execution establishes that the real path ran. The attestation establishes
the interpretation. Neither absorbs the other.

```json
{
  "kind": "execution",
  "name": "verify-login",
  "record_id": "sha256:E",
  "artifacts": [
    { "kind": "recording", "path": ".verification/artifacts/login.webm", "sha256": "sha256:B" },
    { "kind": "dom-proof", "path": ".verification/artifacts/login-dom.json", "sha256": "sha256:C" }
  ]
}
```

```json
{
  "kind": "attestation",
  "name": "verify-login",
  "execution_ref": "sha256:E",
  "artifact_refs": ["sha256:B", "sha256:C"],
  "note": "logged in via the real form; session survived reload"
}
```

The digest is authoritative; the path is only a locator. An attestation that
claims "the screenshot looked correct" without identifying which bytes it judged
establishes nothing composable.

### CI provenance is a chain

Binding `run.head_sha == expected` is not enough. Bind the whole chain:

```
repository -> run -> run attempt -> workflow -> job -> step -> commit
```

The importer rejects a job from another run, a run from another repository, a
different workflow or job than the gate requires, the wrong commit, and a
`pull_request` synthetic-merge run where branch-tip evidence is required. The
existing push/pull_request distinction stays; it is one of the stronger parts of
the current implementation and it is what the merge-run trap is made of.

### Verifier

```json
{
  "verifier": {
    "schema": "verification.ladder.evidence/3",
    "version": "0.2.0",
    "compatibility": "evidence-v3.1",
    "commit": "<verification-ladder commit>",
    "implementation_sha256": "sha256:..."
  }
}
```

### Mixing

v2 and v3 records do **not** mix. A v2 record encountered during composition
requires re-verification. Shadow emission (§M1) is not an exception to this: it
produces two records describing one execution, composed separately under their
own rules, and never one composite drawing on both. (`load_record` already refuses a foreign schema with
BLOCKED; that behaviour is correct and is retained.)

## Completion rules

Throughout, *definition matches* means the record's `definition_sha256` equals
the **governing** definition for that gate — the one captured at baseline (§F,
§L1). Evidence matching only a candidate definition is admissible evidence
*about the candidate*, and never satisfies the governing requirement (§L2).

Throughout, *target projection matches* means: the record carries every target
dimension the gate definition declares, and each of those dimensions equals the
current target's value for it (§I). Undeclared dimensions are not compared.

**Mechanical executable gate:**

```
definition matches                      (incl. declared target dimensions)
AND authority permitted
AND kind == execution
AND target projection matches
AND verifier compatibility equal across the composition
AND status == PASS
```

**Behavioural gate:**

```
all of the above
AND an attestation exists whose execution_ref resolves to this execution
AND that execution's recomputed record_id equals its stored record_id
AND every declared artifact exists and rehashes to its recorded digest
AND the attestation references exactly those digests
AND the attestation carries the same gate name and the same projection
```

**Judgment rung:**

```
definition matches
AND kind == attestation
AND target projection matches
AND verifier compatibility equal across the composition
```

Symmetry is the point: an execution cannot impersonate a judgment, and a
judgment cannot impersonate an execution. Any mismatch fails closed.

## Terminal state

The machine establishes readiness for human acceptance. It does not redefine
verification as acceptance.

```
BASELINE  (pre-mutation)
   |
IMPLEMENT
   |
RUNGS 1-9
   |
CLEAN CLIMB
   |
READY FOR HUMAN GATE
   |
HUMAN GATE
```

Baseline is a **precondition, not a tenth rung**. The original model had nine
rungs beginning after implementation; Baseline was added later and created a
temporal contradiction — the skill says to enter after implementation while
Rung 0 asks whether the system was green before it. Nine rungs, with baseline
captured before mutation, removes it. README's "nine rungs" was right; the
numbering drifted around it.

```
CLEAN CLIMB               TRUE
STATE MATCH               TRUE
EVIDENCE ADMISSIBLE       TRUE
DEFINITION CHANGE         NONE
READY FOR HUMAN GATE      TRUE
```

On a definition-changing task the same block reports the governance state
instead, and `CLEAN CLIMB` does not read TRUE on the strength of candidate
evidence (§L2):

```
CLEAN CLIMB               FALSE     (verify-login: candidate definition only)
STATE MATCH               TRUE
EVIDENCE ADMISSIBLE       TRUE
DEFINITION CHANGE         PENDING   verify-login  g1 -> g2
READY FOR HUMAN GATE      TRUE      as a definition change, not as a clean climb
```

`OPEN FINDINGS` joins that block only when a ledger exists to support it.

---

## Worked example: a login change

A project with a web UI, a `verify-login` behavioural gate, CI-only test gates,
and a driver it owns. Policy (`verification.toml`, committed):

```toml
required_gates   = ["lint", "tests", "verify-login"]
judgment_rungs   = ["task", "diff", "self-review", "requirements", "architecture", "fresh-review"]
ci_head_events   = ["push"]

[gates.local.lint]
command       = "ruff check ."
target        = ["repository"]

[gates.ci.tests]
step          = "Run python -m pytest -q"
target        = ["repository"]

[gates.behavioral.verify-login]
authorities   = ["local"]
evidence_mode = "execution+attestation"
target        = ["repository", "runtime"]
spec_root     = "verification/features"
spec_files    = ["verification/features/sign-in.md"]
driver        = "node verification/drive.mjs verify-login"
artifacts     = ["recording", "dom-proof"]
```

### The clean climb

**1 — Baseline, before touching anything.** `verify.py baseline` records
`verification.ladder.baseline/1`: repository state `sha256:aa..`, governing
`policy_sha256 = sha256:p1`, `sign-in.md` at `sha256:s1`, and the baseline gate
result. This is the only evidence captured pre-mutation.

**2 — Implement.** Login behaviour changes. Repository state moves to
`sha256:bb..`. Every baseline row is now about a different target, as intended.

**3 — Mechanical local gates.** `lint` runs, PASS, authority `local`, kind
`execution`, `definition_sha256` over its declared command **and its declared
projection** `["repository"]`. The record carries a repository dimension and no
runtime, which is exactly what its declaration requires — not an omission.

**4 — Resolve the behavioural specification.** `verify-login` declares
`spec_root = verification/features` and `spec_files = [sign-in.md]`.
`sign-in.md` hashes to `sha256:s1`, resolves beneath the root, and folds into
`definition_sha256 = sha256:g1` together with the gate's declared driver,
authorities, artifact list and projection `["repository", "runtime"]`. This is the step decision B exists for: the gate's
meaning is those declared bytes, not whatever the agent happened to read.

**5 — Interrogate the runtime.** The driver reports build `2026.09.20-1a2b`,
instance `local-9222`, flags `["auth_v2"]`. These hash to
`manifest_sha256 = sha256:r1`. `target_state` is now
`{repository: bb.., runtime: r1}`.

**6 — Drive the production path.** The real form, the real submit, the real
session RPC. Not an internal handler invoked directly.

**7-8 — Artifacts and the behavioural execution.** `login.webm` →
`sha256:B`; `login-dom.json` (post-reload session assertion) → `sha256:C`. The
execution record carries both digests, `definition_sha256: g1`, authority
`local`, kind `execution`, projection `{repository: bb.., runtime: r1}`, and its
own `record_id = sha256:E` over its canonical form.

**9 — Attest over exactly those bytes, naming the execution.**
`execution_ref: sha256:E`, `artifact_refs: [B, C]`, kind `attestation`, same
gate name, same projection.

**10 — Import CI.** The `push` run for commit `bb..` in **this** repository,
attempt 1, workflow `ci.yml`, job `validate`, step `Run python -m pytest -q`,
conclusion `success` → the `tests` gate, authority `ci`, `target_state`
`{repository: bb.., runtime: absent}`.

**11 — Compose.** The composer resolves `execution_ref: E` to the behavioural
execution in this composition and recomputes its `record_id` (§K); rehashes
`login.webm` and `login-dom.json` from disk and confirms B and C (C1); confirms
the attestation names the same gate under the same projection (C2, §I.4);
re-interrogates the runtime and confirms `r1`; recomputes repository state and
confirms `bb..`; checks each gate against **its own declared projection** — so
`lint` and `tests` are compared on repository alone and `verify-login` on
repository and runtime (§I); checks each `definition_sha256` against the
governing policy from the baseline record (§F); and confirms every record
carries the same verifier `compatibility` (§J).

**12-14 —**

```
CLEAN CLIMB               TRUE
STATE MATCH               TRUE
EVIDENCE ADMISSIBLE       TRUE
READY FOR HUMAN GATE      TRUE

baseline                  sha256:aa..  PASS  (pre-mutation)
--> HUMAN GATE
```

The machine stops here. A person accepts or does not.

### Four ways the same trace fails closed

**V1 — the specification changed after execution.** Someone edits `sign-in.md`
to drop the persistence case. It now hashes `sha256:s2`, so the gate's
definition is `sha256:g2`. The execution record says `g1`.

```
verify-login   INADMISSIBLE   gate definition changed since execution
                              (recorded g1, policy now g2)
```

Not a FAIL — the gate did pass, under a definition no longer in force.

**V2 — an artifact was replaced after attestation.** `login.webm` is
re-recorded. The composer rehashes it: `sha256:B2`. The attestation references
`B`.

```
verify-login   INADMISSIBLE   artifact sha256:B not found on disk;
                              login.webm rehashes to sha256:B2
```

This is the case that only C1 catches. Comparing recorded strings would have
passed it.

**V3 — the runtime baseline moved.** A dependency redeploys; the driver now
reports build `2026.09.20-9f8e`, manifest `sha256:r2`.

```
STATE MATCH    FALSE          evidence binds to runtime sha256:r1,
                              target is sha256:r2
verify-login   STALE
```

`lint` and `tests` survive — they declare `target = ["repository"]`, so the
composer never compares them against a runtime they cannot depend on, and their
repository projection still matches. Only the behavioural gate expires. That
granularity is what §I's per-gate projection buys; a global freshness flag would
have expired all three.

Note the omission attack this closes. An agent that re-recorded `verify-login`
evidence *without* a runtime dimension does not thereby survive the move: the
gate declares `runtime`, so a record lacking it is **inadmissible** rather than
trivially matching (§I.1). And editing the declaration to drop `runtime` changes
`definition_sha256`, which fails as V1 does.

Had the driver been unable to reach the runtime at all, this would instead be
**BLOCKED** (decision D) — no target, not a different one.

**V4 — CI evidence from the wrong run.** A job payload from run `222222` is
paired with run metadata from `111111`.

```
BLOCKED  job.run_id 222222 does not belong to run 111111
```

This one fails at **import time**. No record is written, so nothing reaches
composition. Failing closed at the earliest point that can detect the break is
the general rule.

---

## Implementation series

Written before any of it began, and kept as written. Items 1-14 have landed;
item 15 is §M4 and has not. No authoritative evidence behaviour changed in any
of items 1-14.

1. **Schema-v3 data model**, parsing and validation — including legacy v2
   policy normalization and the migration baseline (§L5, §L6).
2. **Target projections** plus repository/index identity.
3. **Gate definition and authority identity.**
4. **Symmetric execution/attestation enforcement.**
5. **Execution record ids and the artifact chain** (§K, C1, C2).
6. **CI provenance chain** — repository → run → attempt → workflow → job → step
   → commit.
7. **Verifier compatibility** (§J), plus the INSTALL.md pinned-release
   correction (§E).
8. **Governing baseline policy and specification binding** (§F), plus
   definition-change governance and `DEFINITION CHANGE PENDING` (§L1-§L4).
9. **Output and terminology** — `CLEAN CLIMB`, remove `UNRESOLVED FINDINGS`, add
   `READY FOR HUMAN GATE`; correct the `state_id` claim.
10. **G1 and G2** — `run --gate` must load policy; artifact paths excluded from
    repository state by construction.
11. **Adversarial regression suite.**

Items 1-11 land the model and the predicates. They do not activate anything, and
the series does not end with them — activation is four further stages (§M), the
last of which is its own review point:

12. **Shadow emission** (§M1) — v3 emitted beside v2, v2 still authoritative,
    no verdict or exit code changed.
13. **Comparison** (§M2) — v2 and v3 outcomes compared per gate, disagreements
    surfaced diagnostically and never applied.
14. **Qualification** (§M3), in the order its prerequisites appear:
    a. the shadow-only v3 policy layer (§M2a), with behavioural gates, declared
       artifacts and the runtime dimension — no authority, no execution;
    b. the attestation shadow representation, with its execution binding named
       rather than discovered;
    c. the baseline capture and CI step provenance the governance and
       `ci_step_status` predicates need, both non-authoritative;
    d. the qualification report itself — qualified, disagreement, not
       comparable, uncovered prerequisite — and item 11's adversarial cases
       driven through the shadow surfaces rather than against the predicates.
    A green suite is not qualification.
15. **Authority switch** (§M4) — evidence/3 becomes authoritative, `VERSION`
    moves to `0.2.0`, in a commit of its own.

## Adversarial acceptance tests

Write these failing first. Composition must reject:

1. a custom command impersonating a required policy gate;
2. a local command impersonating a CI-only gate;
3. an execution impersonating `diff` or another judgment rung;
4. an attestation impersonating executable evidence;
5. a CI job paired with another run;
6. CI evidence from another repository;
7. CI evidence from the wrong workflow, job or step;
8. evidence whose verifier `compatibility` differs from the rest of the
   composition;
9. two repository states with identical worktree bytes but different index
   contents;
10. composition under a weakened working-tree policy;
11. evidence produced before a declared specification file changed;
12. a declared specification file resolving outside its `spec_root`;
13. a referenced artifact that is missing, or that rehashes differently;
14. **a gate whose declared runtime projection differs from the current
    runtime** — and, in the same composition, a `target = ["repository"]` gate
    that correctly **survives** that same move. Both directions are the test;
    asserting only the expiry would pass a global freshness flag;
15. evidence omitting a target dimension its gate definition declares;
16. an attestation whose `execution_ref` resolves to no execution record in the
    composition;
17. an attestation whose `execution_ref` names an execution for a different
    gate, or carrying a different projection;
18. an execution record whose recomputed `record_id` differs from the stored
    one;
19. a behavioural execution with no corresponding artifact-bound attestation;
20. a changed declared gate specification yielding a silent PASS rather than
    `DEFINITION CHANGE PENDING`;
21. candidate-definition evidence satisfying the governing definition;
22. a definition change to one gate invalidating an unrelated unchanged gate —
    `lint` must remain admissible while `verify-login` changes definition (§L3);
23. a candidate definition failing to govern normally once it is the *next*
    task's baseline;
24. non-deterministic normalization of the v2 policy form, if legacy parsing is
    adopted (§L6);
25. a reconstructed baseline that is not marked `origin: "reconstructed"`, or
    one reconstructed from a dirty rather than a committed state (§L5).

Distinguished from the above, and asserted separately: an unreachable runtime
yields **BLOCKED**, not a state mismatch.

The combined spoofing reproduction from the PR #1 review becomes a permanent
test. It must be impossible to produce `READY FOR HUMAN GATE TRUE` when the
required policy gates and judgment rungs did not actually occur.

**Not yet an acceptance test:** "a purported clean result while a tracked finding
remains open" — blocked on a findings ledger (decision H).
