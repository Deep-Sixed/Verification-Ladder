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

The sequence below is the one to read; [The scripted install](#the-scripted-install)
runs the same steps and checks the result.

## Claude Code

Personal skills live in `~/.claude/skills/<name>/`. Link, do not copy:

```sh
mkdir -p ~/.claude/skills
ln -s ~/src/verification-ladder/skills/verification-ladder ~/.claude/skills/verification-ladder
```

A linked skill is available in every repository, and `git pull` in the clone
updates every agent at once.

The skill's own directory is reported to the agent when the skill loads, which
is how it finds `bin/verify.py`.

### Pin the revision you install

**Do not leave the clone tracking a branch.** Evidence records will carry the
verifier's compatibility contract, and composition will require every record in
one composite to share it. A clone that moves under the agent changes the rules
mid-task: records taken before a `git pull` and after it can stop composing, for
a reason that has nothing to do with the code under verification.

**There are no release tags yet**, so pin the commit you intend to run:

```sh
git -C ~/src/verification-ladder fetch origin
git -C ~/src/verification-ladder checkout --detach <commit>
git -C ~/src/verification-ladder rev-parse HEAD    # record this; evidence will name it
```

When releases are cut, a tag becomes the thing to pin and this reads
`checkout v<version>` instead. Until then a branch name is not a pin, and
`main` is not a stable one — it is simply a branch that has moved less.

Upgrading stays deliberate: fetch, read what changed, check out the new commit,
and re-verify anything in flight if evidence semantics moved.

## Codex

Codex Desktop was verified on 2026-09-20 with a linked user skill under
`~/.codex/skills/<name>/`. The requirement is that Codex resolves to *this*
directory rather than to a copy:

```sh
mkdir -p ~/.codex/skills
ln -s ~/src/verification-ladder/skills/verification-ladder ~/.codex/skills/verification-ladder
```

The verified installation resolved:

```text
~/.codex/skills/verification-ladder
  -> ~/src/verification-ladder/skills/verification-ladder
```

Codex loaded the linked `SKILL.md` from that path, read the nine-rung ladder,
resolved `bin/verify.py` beside the skill, and ran the verifier from the linked
tree. OpenAI's Codex documentation also says local skills may be symlinked; if
your Codex build reports a different user-skill root, use that root with the
same symlink target and run the checks below.

Run two different tests, because they establish different things and only one of
them is decisive.

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

## The scripted install

`scripts/install-ladder.sh` performs everything above - the clone, the detached
pin, the symlinks and the invariant - and `scripts/check-install.sh` reports
whether the result conforms. It works against an established workstation: it
never overwrites existing global instructions, and it refuses rather than
guesses when it finds something it did not put there.

```sh
git clone https://github.com/Deep-Sixed/verification-ladder /tmp/ladder-bootstrap
sh /tmp/ladder-bootstrap/scripts/install-ladder.sh      # --codex to install for Codex too
```

`--dry-run` prints the sequence without performing it. Note that it can only
validate the invariant step once `~/src/verification-ladder` exists, and says so
rather than reporting a success it did not establish.

### Where the bootstrap tooling lives

Neither script is in the pinned checkout. The pin predates `scripts/`, so
detaching `~/src/verification-ladder` to it *removes both scripts from that
directory* - including `install-ladder.sh` while it is running. The installer
therefore copies the pair out first, to

```
${XDG_DATA_HOME:-~/.local/share}/verification-ladder/
    install-ladder.sh
    check-install.sh
    PROVENANCE
```

and names that path in its closing message. Run the checker from there:

```sh
~/.local/share/verification-ladder/check-install.sh     # --codex to check Codex too
```

`PROVENANCE` records where the pair was copied from, the source revision, the
ladder pin, and a sha256 for each script. Once copied out of a checkout these
files have no other identity, which is what that file exists to supply.

This is the deliberate split. The **Ladder runtime** is a pinned, immutable
checkout at `~/src/verification-ladder`. The **installer and checker** are
bootstrap tooling from a newer reviewed revision, versioned independently and
kept outside it. They are nonetheless one reviewed *pair*: the installer carries
the expected sha256 of its checker and refuses to run beside a different one, so
a newer installer cannot quietly preserve an older, weaker checker for everyone
to run afterwards.

`check-install.sh` establishes installation integrity and nothing more. It does
not establish that an agent invokes the Ladder during ordinary work - that is
the policy-less `BLOCKED` test, and it is the property that can regress silently
after a good install.
