---
name: verify
description: Build, launch and drive the Verification Ladder CLI to observe a change at its surface. Use when verifying a change to verify.py, the skill documents or the policy templates.
---

# Verifying the Verification Ladder

The surface is the CLI: `skills/verification-ladder/bin/verify.py`. Standard
library and `git` only, Python 3.11+, so a checkout is a working installation
and there is nothing to build.

## Tools the gates need

The policy's gates run under `uv`; this repository's own `[gates.local]` uses
pinned versions. To drive the CLI directly without `uv`:

```sh
pip install -q pytest==9.1.1 ruff==0.16.8
```

Those are the versions `verification.toml` and `.github/workflows/ci.yml` pin.
An equivalent command is **not** the declared one — under the gate-definition
binding this project is moving to, running `ruff` directly is a smoke check and
not policy evidence. Say which you did.

## A consumer repository to drive it against

Never drive it against this checkout: the gates would run over the tool's own
tree. Build a throwaway consumer instead.

```sh
R=$(mktemp -d); cd "$R" && git init -q -b main .
cp <ladder>/templates/verification.toml .
# the ignore set INSTALL.md specifies — without it, the pytest gate's own
# __pycache__ lands in the tree and every run reports BLOCKED on drift
printf '.verification/\n__pycache__/\n*.py[cod]\n.pytest_cache/\n.ruff_cache/\n' > .gitignore
```

The template's `tests` gate exits 5 ("no tests collected") on a repository with
no tests, which reads as FAIL. That is correct, not a bug — add a test or tailor
the policy.

## The full workflow

```sh
V=<ladder>/skills/verification-ladder/bin/verify.py
python3 "$V" run --output .verification/local.json
python3 "$V" attest --rung diff --note "read every hunk"
mkdir -p .verification/import   # payloads go in a subdirectory, see below
python3 "$V" import-ci --run .verification/import/github-run.json \
                       --job .verification/import/github-job.json \
                       --output .verification/ci.json
python3 "$V" compose .verification/*.json    # 0 COMPLETE, 1 INCOMPLETE, 2 STALE
python3 "$V" check .verification/local.json
```

CI payloads belong under `.verification/import/`, not at the repository root and
not directly in `.verification/`. At the root they are untracked files that move
the state being verified, and the composite reports `STATE MATCH FALSE` against
evidence just produced honestly. Directly in `.verification/` they get swept into
`compose .verification/*.json` as if they were evidence records.

Minimal payloads that satisfy the template's `[ci_steps]`:

```sh
SHA=$(git rev-parse HEAD)
printf '{"id":42,"head_sha":"%s","event":"push","conclusion":"success","head_branch":"main"}\n' "$SHA" \
  > .verification/import/github-run.json
printf '{"id":7,"run_id":42,"steps":[{"name":"Run ruff check .","conclusion":"success"},{"name":"Run python -m pytest -q","conclusion":"success"}]}\n' \
  > .verification/import/github-job.json
```

The event must be `push`. A `pull_request` run checks out a synthetic merge and
`import-ci` refuses it by design.

## Flows worth driving

- **State binding.** Compose to COMPLETE, edit a tracked file, then `check` —
  expect STALE exit 2 and the composite `STATE MATCH FALSE`.
- **Drift.** A gate that writes into the tree reports BLOCKED and names the
  paths that moved. Both cases matter: disposable tool output (fix with
  `.gitignore`) and a real source mutation (BLOCKED is the right answer).
- **Onboarding refusal.** A repository with no policy is BLOCKED, and a policy
  that exists but does not parse reports as broken rather than absent.
- **Malformed input.** `--gate lint=`, `--gate noequals`, a missing record, a
  non-repository `--repo` — each has its own message and exits 2.

## The evidence/3 shadow surfaces

`run`, `attest` and `import-ci` each write an evidence/3 shadow beside their
authoritative record, under `.verification/shadow/`. Three further commands read
them and decide nothing:

```sh
python3 "$V" baseline                                    # definitions, before the task
python3 "$V" attest --rung verify-login --note "watched it"     --execution .verification/local.json                 # binds judgment to execution
python3 "$V" compare .verification/local.json     --attestations .verification/attestations.json
python3 "$V" qualify .verification/*.json
```

`compare` exits 0 agree / 1 disagree / 2 not comparable; `qualify` adds
`UNCOVERED` for a predicate nothing reached. Neither touches a verdict.

To exercise behavioural gates, the runtime dimension or artifacts, the consumer
policy needs a `[shadow]` layer - `templates/verification.toml` carries a
commented example of all three. Two traps worth knowing:

- A gate named under `[shadow.gates.*]` is described ENTIRELY there. Its
  `[gates.local]` entry is dropped from the evidence/3 view, so declaring the
  same gate under two shadow blocks means repeating `target` and
  `evidence_mode` identically in both, or normalization refuses it.
- `--gate NAME=COMMAND` forges nothing when the command it runs is the one the
  policy declares. `--gate tests=true` against `[gates.local] tests = "true"`
  is admissible and correctly so. Use a command that differs.

## Measuring what the CLI actually reaches

Much of `verify.py` is the evidence/3 model, which is deliberately not wired to
any subcommand yet. To check whether a function is reachable from the CLI rather
than assuming:

```sh
python3 -m trace --listfuncs "$V" run --output /dev/null 2>&1 | grep 'funcname: <name>$'
```

The output format is `filename: …, modulename: …, funcname: X` on one line — a
`^funcname:` anchor matches nothing and will quietly report everything as
unreachable. That mistake has been made twice; check the format before trusting
a reachability result.

## Gotchas

- Shell loops that build TOML with `printf "$var\n"` mangle it, and the run then
  takes a path you did not intend. Write policy files with a quoted heredoc.
- **When verifying an exit code, capture the subject command's status
  immediately.** Never infer it from a surrounding pipeline, shell wrapper
  (`bash -c`), loop body, command group, cleanup sequence, or any later command:
  `$?` reports whatever executed last, which in all of those is something other
  than the thing being measured. Run the subject on its own line and read `$?` on
  the next one. Three verification passes have produced three measurement errors
  and this was one of them, through a `bash -c` wrapper whose final statement was
  a `cp` restoring a file.
- **Before treating a probe result as evidence, establish that the probe could
  have failed.** A probe whose failure mechanism is inert in this environment
  proves nothing by succeeding: `chmod 500` to test a write refusal establishes
  nothing when the process is root, and a green result there means the check
  never ran, not that the property holds. Where practical, drive the control path
  first — make the probe fail on purpose, see it fail, then test the real case.
  This is a different failure class from the exit-status rule above: that one
  captures the wrong status, this one captures the right status from a check with
  no discriminating power.
- `compose` needs every record to agree on one state. Two state ids for one
  unchanged commit almost always means an untracked file appeared mid-workflow.
