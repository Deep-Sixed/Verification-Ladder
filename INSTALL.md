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

## Codex

Codex discovers skills from its skills directory the same way. Link the same
directory — never a second copy:

```sh
mkdir -p ~/.codex/skills
ln -s ~/src/verification-ladder/skills/verification-ladder ~/.codex/skills/verification-ladder
```

Verify the path against your Codex version's documentation; if it differs, link
into whatever directory it reads. The requirement is only that both agents
resolve to this one directory.

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
checkout cannot run. Add `.verification/` to its `.gitignore` — evidence records
are operational, not repository content.

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
