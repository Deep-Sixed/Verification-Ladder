# Installing the Verification Ladder

The skill is files an agent reads and runs. There is no build step and no
runtime dependency beyond Python 3.11+ and `git`, so a checkout of this
repository is a working installation.

Install it **once per machine**, not once per project. Divergent copies are the
failure this repository exists to prevent: if two agents read different ladders,
the composite means nothing.

```sh
git clone https://github.com/Deep-Sixed/verification-ladder ~/src/verification-ladder
```

## Claude Code

Personal skills live in `~/.claude/skills/<name>/`. Link, do not copy:

```sh
mkdir -p ~/.claude/skills
ln -s ~/src/verification-ladder/skills/verification-ladder ~/.claude/skills/verification-ladder
```

A linked skill is available in every repository, and `git pull` in the clone
updates every agent at once. The skill's own directory is reported to the agent
when the skill loads, which is how it finds `bin/verify.py`.

## Codex — not yet verified

**This section is untested.** The Claude Code path above was checked against a
running installation; the Codex equivalent has not been. Do not treat "both
agents use the same installed skill" as established until you have confirmed it
on your own Codex environment.

The requirement is only that Codex resolves to *this* directory rather than to a
copy. If Codex reads a skills directory the way Claude Code does, a link is
enough:

```sh
mkdir -p ~/.codex/skills
ln -s ~/src/verification-ladder/skills/verification-ladder ~/.codex/skills/verification-ladder
```

Check that path against your Codex version's documentation before relying on it.
Then run two different tests, because they establish different things and only
one of them is decisive.

**Discovery** — can the agent find and read the skill?

> Ask Codex to read the Verification Ladder skill and report how many rungs it
> has. The answer is nine.

A correct answer proves the files are reachable. It proves nothing about whether
they will be used.

**Invocation** — does the agent actually climb the ladder during ordinary work?

> In a repository with **no** `verification.toml`, give Codex a task that
> modifies repository state. It should stop at BLOCKED and say the project has
> no verification policy. If it reports the work complete, the integration has
> failed — whatever it answered about rung counts.

The negative case is what makes this test worth running: an unconfigured
repository is the one situation where a silent bypass is invisible in the
output, because there is nothing to compare against and the work may well look
finished. An agent that sails past it is an agent that will sail past a failing
gate too.

Until invocation passes, do not describe the Ladder as governing Codex. A
repository-local `AGENTS.md` naming the procedure is the reliable way to reach
it in the meantime.

## The invariant

Installing the skill does not make an agent invoke it. Something has to say that
verification is mandatory, and that belongs in your **global** agent
instructions, so it governs every project without each project restating it:

```markdown
For every task that modifies repository state:

1. Perform the requested work.
2. Before declaring the task complete, read the `verification-ladder` skill and
   execute its procedure against the resulting repository state. Read it per
   task, not once per session.
3. If verification produces a finding, repair it.
4. After a repair, re-enter verification at the rung the repair invalidated. A
   verification result belongs to the state that produced it; changing the state
   discards it.
5. Do not report completion while an open finding remains.
6. A repository with no verification policy is BLOCKED, not verified: say so and
   offer to onboard it. Never treat a missing policy as a pass.

Do not claim PASS, "tests pass", "the build works" or "complete" without evidence
you hold for the current state.
```

Put that in `~/.claude/CLAUDE.md` and the Codex equivalent. A repository may add
stricter requirements of its own; it should not need to repeat this.

## Onboarding a repository

```sh
cp ~/src/verification-ladder/templates/verification.toml <repo>/verification.toml
```

Then make it true for that project: its required gates, the commands a
contributor actually runs, and the CI step that establishes each gate the
checkout cannot run.

Then extend its `.gitignore`. Evidence records are operational rather than
repository content, and so is whatever the gates themselves write:

```
.verification/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
```

The last four are what the shipped `ruff` / `pytest` template produces. They
matter because a gate's own output lands in the tree the gate is being measured
against: `state_id` moves between the start and the end of the run, and
`verify.py run` reports **BLOCKED — drift** with both gates passing. A project
whose gates use other tooling ignores that tooling's caches instead.

**Ignore disposable tool output, never project-significant output.** If a gate
reformats source, updates a lockfile or regenerates checked-in code, that change
is repository state and drift is the correct answer — the result binds to no
single state and BLOCKED is what it should say. Widening `.gitignore` to make
such a run go green hides a real change to get a PASS, which is the failure this
tool exists to prevent.

Commit it as its own reviewed change. What "complete" means for a project is a
decision that deserves review, not something an agent improvises mid-task.

A Python project that prefers one config file may put the same tables under
`[tool.verification]` in `pyproject.toml`; `verification.toml` wins if both
exist.

## Checking the install

From any repository with a policy:

```sh
python ~/src/verification-ladder/skills/verification-ladder/bin/verify.py --repo . run
```

A repository with no policy should exit 2 and tell you so. That is the install
working, not failing.
