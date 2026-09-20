# Evidence v3 — admissibility as a single invariant

**Status: design note. Documentation only; no implementation accompanies it.**
Nothing in this note is built. It fixes the shape of
`verification.ladder.evidence/3` so that the enforcement work can be implemented
against a settled schema rather than discovered during it.

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
  "evidence_mode": "execution"
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
    "commit": "<verification-ladder commit>",
    "implementation_sha256": "sha256:..."
  }
}
```

### Mixing

v2 and v3 records do **not** mix. A v2 record encountered during composition
requires re-verification. (`load_record` already refuses a foreign schema with
BLOCKED; that behaviour is correct and is retained.)

## Completion rules

**Mechanical executable gate:**

```
definition matches
AND authority permitted
AND kind == execution
AND target_state matches
AND verifier compatible
AND status == PASS
```

**Behavioural gate:**

```
all of the above
AND every declared artifact exists and rehashes to its recorded digest
AND a required attestation exists
AND that attestation references exactly those digests
AND the referenced execution is in this composition, on this target_state
```

**Judgment rung:**

```
definition matches
AND kind == attestation
AND target_state matches
AND verifier compatible
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
READY FOR HUMAN GATE      TRUE
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

[gates.local]
lint = "ruff check ."

[gates.behavioral.verify-login]
authorities     = ["local"]
evidence_mode   = "execution+attestation"
spec_root       = "verification/features"
spec_files      = ["verification/features/sign-in.md"]
driver          = "node verification/drive.mjs verify-login"
artifacts       = ["recording", "dom-proof"]

[ci_steps]
tests = "Run python -m pytest -q"
```

### The clean climb

**1 — Baseline, before touching anything.** `verify.py baseline` records
`verification.ladder.baseline/1`: repository state `sha256:aa..`, governing
`policy_sha256 = sha256:p1`, `sign-in.md` at `sha256:s1`, and the baseline gate
result. This is the only evidence captured pre-mutation.

**2 — Implement.** Login behaviour changes. Repository state moves to
`sha256:bb..`. Every baseline row is now about a different target, as intended.

**3 — Mechanical local gates.** `lint` runs, PASS, authority `local`, kind
`execution`, `definition_sha256` over its declared command. No `runtime` block:
lint cannot depend on a running system.

**4 — Resolve the behavioural specification.** `verify-login` declares
`spec_root = verification/features` and `spec_files = [sign-in.md]`.
`sign-in.md` hashes to `sha256:s1`, resolves beneath the root, and folds into
`definition_sha256 = sha256:g1` together with the gate's declared driver,
authorities and artifact list. This is the step decision B exists for: the gate's
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
`local`, kind `execution`, and `target_state {bb.., r1}`.

**9 — Attest over exactly those bytes.** `artifact_refs: [B, C]`, kind
`attestation`, same `target_state`.

**10 — Import CI.** The `push` run for commit `bb..` in **this** repository,
attempt 1, workflow `ci.yml`, job `validate`, step `Run python -m pytest -q`,
conclusion `success` → the `tests` gate, authority `ci`, `target_state`
`{repository: bb.., runtime: absent}`.

**11 — Compose.** The composer rehashes `login.webm` and `login-dom.json` from
disk (C1), confirms they match B and C, confirms the attestation's referenced
execution is present in this composition on target `{bb.., r1}` (C2),
re-interrogates the runtime and confirms `r1`, recomputes repository state and
confirms `bb..`, checks each gate's `definition_sha256` against the governing
policy from the baseline record (§F, §11), and checks verifier compatibility.

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

`lint` and `tests` survive — they carry no runtime and their repository state
still matches. Only the behavioural gate expires. That granularity is the
reason runtime lives in `target_state` rather than in a global freshness flag.

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

## Implementation series (after this note is reviewed)

1. **Evidence schema v3** — target state incl. index, runtime manifest,
   gate-definition identity, verification-spec digests, artifact digests,
   artifact-bound attestations, verifier block, baseline record type.
2. **Gate-definition and authority binding** — close name-based impersonation.
3. **Symmetric execution/attestation enforcement** — close judgment-rung
   impersonation.
4. **CI provenance chain** — repository → run → attempt → workflow → job → step
   → commit.
5. **Verifier provenance and compatibility** — plus the INSTALL.md pinned-release
   correction (decision E).
6. **Governing-policy binding** — compose against the policy captured at
   baseline, not the mutable working-tree copy.
7. **Artifact rehashing and chain enforcement** — decisions C1 and C2.
8. **Honest output** — `CLEAN CLIMB`, remove `UNRESOLVED FINDINGS`, add
   `READY FOR HUMAN GATE`.
9. **Documentation model** — baseline precondition, nine rungs, human gate;
   correct the `state_id` claim.
10. **Deferred items G1 and G2** — `--gate` must load policy; artifact paths
    excluded from state by construction.
11. **Adversarial regression suite.**

## Adversarial acceptance tests

Write these failing first. Composition must reject:

1. a custom command impersonating a required policy gate;
2. a local command impersonating a CI-only gate;
3. an execution impersonating `diff` or another judgment rung;
4. an attestation impersonating executable evidence;
5. a CI job paired with another run;
6. CI evidence from another repository;
7. CI evidence from the wrong workflow, job or step;
8. evidence from an incompatible verifier version;
9. two repository states with identical worktree bytes but different index
   contents;
10. composition under a weakened working-tree policy;
11. evidence produced before a declared specification file changed;
12. a behavioural attestation referencing artifacts from an earlier execution;
13. a referenced artifact that is missing, or that rehashes differently;
14. local and behavioural evidence whose runtime manifests differ;
15. a declared specification file resolving outside its `spec_root`;
16. a behavioural execution with no corresponding artifact-bound attestation.

Distinguished from the above, and asserted separately: an unreachable runtime
yields **BLOCKED**, not a state mismatch.

The combined spoofing reproduction from the PR #1 review becomes a permanent
test. It must be impossible to produce `READY FOR HUMAN GATE TRUE` when the
required policy gates and judgment rungs did not actually occur.

**Not yet an acceptance test:** "a purported clean result while a tracked finding
remains open" — blocked on a findings ledger (decision H).
