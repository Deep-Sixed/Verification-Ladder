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
- A pipeline's `$?` is the last stage's, not `verify.py`'s. Measure the exit code
  on its own line.
- `compose` needs every record to agree on one state. Two state ids for one
  unchanged commit almost always means an untracked file appeared mid-workflow.
