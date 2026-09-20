# Verification Ladder

A portable verification procedure for coding agents: implement → verify → review
→ repair → **re-verify**, ending in evidence bound to the exact repository state
that produced it, rather than in an agent's assurance that it checked.

The problem it solves is narrow and specific. An agent that reports "done" after
implementing is asserting something it has not established, and the usual remedy
is a person carrying results to a reviewer and findings back. That person is a
message bus. This replaces the bus, not the human judgment at the end of it.

```
                  TASK
                    │
                    ▼
                IMPLEMENT
                    │
                    ▼
    ┌──── VERIFICATION LADDER ◄───┐
    │            │                │
    │         finding             │
    │            │                │
    │          REPAIR ────────────┘
    │
    ▼
 LOCAL EXECUTION ─────┐
 CI EXECUTION ────────┼──► COMPOSE ──► COMPLETE = TRUE ──► HUMAN
 AGENT ATTESTATIONS ──┘
```

## What it is

- **A skill** (`skills/verification-ladder/`) an agent reads per task: nine rungs
  from baseline through a fresh-reviewer pass, with defined invalidation,
  re-entry, escalation and termination.
- **Evidence machinery** (`skills/verification-ladder/bin/verify.py`): runs a
  project's gates, records what ran, and binds each result to a `state_id` taken
  over HEAD plus every deviation from it. Standard library and `git` only.
- **A composition protocol**: local execution, CI execution and agent
  attestations combine into one completion predicate over one state.

## The two rules

**A verification result belongs to one repository state.** Change the state and
every earlier result is stale. Repair is a state change, so a repair expires the
PASS it was meant to earn. `check` and `compose` enforce this; attestations are
discarded outright when the tree moves, because "I reviewed the diff" cannot
survive a change to the diff.

**PASS is a claim about evidence you hold.** A gate that could not run is
BLOCKED, never PASS. A green gate over a tree that moved while it ran establishes
nothing about either state. A required gate nobody ran reads `MISSING` and counts
against completion — absence of evidence is not evidence of success.

## Execution is not attestation

Every row carries its kind. `container-build PASS execution` and
`self-review PASS attestation` are different claims and print differently; a
required gate holding only an attestation reads `attested, never executed` and
fails the predicate. This is the property that keeps a composite from laundering
an agent's self-assessment into a fact.

```
VERIFICATION STATE: 7272888 + sha256:3130b1f9…

  * lint               PASS    execution    local, ci run 35486373054
  * container-build    PASS    execution    ci run 35486373054
    diff               PASS    attestation  agent
  --------------------------------------------------------------
  BLOCKED REQUIRED GATES   0
  STALE EVIDENCE           0
  STATE MATCH              TRUE
  CLEAN CLIMB              TRUE
  --------------------------------------------------------------
  READY FOR HUMAN GATE     TRUE
```

## Procedure here, policy there

The ladder is the same everywhere. What counts as verified is not: each project
declares that in its own committed `verification.toml`, which names its required
gates, its local commands and the CI step that establishes each CI-only gate.
Changing what "complete" means is then a reviewed change to that project.

**A repository with no policy is BLOCKED, not verified.** Silence is not
consent; an unconfigured project must never read as a passing one.

See [INSTALL.md](INSTALL.md) to install it and onboard a repository. The Claude
Code install path is verified; the Codex one is written but not yet tested, and
is marked as such rather than claimed.

## Status

Early. The procedure and the machinery were developed and dogfooded against a
real project ([Cerberus](https://github.com/Deep-Sixed/Cerberus)), which is now a
consumer of this repository rather than the owner of the skill. This repository
verifies itself with its own ladder on every change.

The evidence model is being revised. [`docs/design/evidence-v3.md`](docs/design/evidence-v3.md) fixes the shape of
`verification.ladder.evidence/3`, under which a result is admissible only when
its definition, permitted authority, execution provenance, target state, proof
artifacts and verifier identity all refer to the same thing.

**The model is implemented; it is not authoritative.** Records on disk are
`evidence/2`, `compose` applies v2 rules, and every verdict and exit code the
CLI produces is v2's. Beside each authoritative record the CLI writes an
evidence/3 shadow, and `compare` and `qualify` report how the v3 reading of the
same execution differs from it. A disagreement there is a finding about the
verifier, never about the change. Activation is staged (§M) and the switch is
its own review point.

MIT licensed; see `LICENSE`.
