"""The negative matrix for the installer and the install checker.

Three review rounds found the same shape of defect: a check that passes over a
broken install. Each case below is one that was reproduced against the scripts
before it was fixed, so the suite is a record of what actually went wrong
rather than a guess at what might.

Every negative asserts its *specific* failure reason, not merely a nonzero
exit. A wrong exit code with the right message is a passing test that proves
nothing: an early version of the graft check aborted on clean clones for an
unrelated reason, and only the message distinguished it from working.

Most of this suite runs offline: the template clone is built from this
repository's own history, so the pin needs no fetch. The cases that reach
`git fetch origin` against the real GitHub origin are marked `network` and are
the ones that fail without it. Run the offline subset with:

    python -m pytest -m "not network"

The split was measured, not assumed: under `unshare -rn` with commit signing
disabled, `-m "not network"` passes 36/36 and `-m network` fails exactly the
three marked cases, each at `git fetch origin`. That measurement belongs to the
state that produced it; `test_network_surface_has_not_moved` below fails if the
scripts grow another network call, which is the signal to re-measure.
"""

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "scripts" / "install-ladder.sh"
CHECKER = REPO / "scripts" / "check-install.sh"
PIN = "760977f69d6b74b9888be4c9cbb404f7726bea73"
ORIGIN = "https://github.com/Deep-Sixed/Verification-Ladder"


def _git(*args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True)


def _run(script, home, *args, codex_home=None, xdg=None, path_prefix=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CODEX_HOME"] = str(codex_home or Path(home) / ".codex")
    env["XDG_DATA_HOME"] = str(xdg or Path(home) / ".local" / "share")
    if path_prefix:
        env["PATH"] = str(path_prefix) + os.pathsep + env["PATH"]
    return subprocess.run(["sh", str(script), *args], env=env, check=False,
                          capture_output=True, text=True)


@pytest.fixture(scope="session")
def template(tmp_path_factory):
    """A conforming clone, built offline from this repository's own history.

    The pin is in this repo's history, so no network is needed. origin is then
    set to the GitHub URL the scripts accept, which is what a real clone has.
    """
    root = tmp_path_factory.mktemp("template")
    clone = root / "verification-ladder"
    _git("clone", "--quiet", str(REPO), str(clone), cwd=REPO)
    _git("remote", "set-url", "origin", ORIGIN, cwd=clone)
    _git("checkout", "--quiet", "--detach", PIN, cwd=clone)
    return clone


@pytest.fixture
def home(tmp_path, template):
    """A fake HOME holding a conforming install: pinned clone plus symlink."""
    h = tmp_path / "home"
    (h / "src").mkdir(parents=True)
    clone = h / "src" / "verification-ladder"
    shutil.copytree(template, clone, symlinks=True)
    skills = h / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "verification-ladder").symlink_to(clone / "skills" / "verification-ladder")
    invariant = subprocess.run(
        ["sh", "-c",
         "awk '/^## The invariant/{f=1} f&&/^```markdown$/{g=1;next} g&&/^```$/{exit} g{print}' "
         + str(clone / "INSTALL.md")],
        capture_output=True, text=True, check=True).stdout
    (h / ".claude" / "CLAUDE.md").write_text("# Verification\n\n" + invariant)
    return h


# --------------------------------------------------------------------------
# The checker: each row is a broken install a plausible check would pass.
# --------------------------------------------------------------------------

def test_conforming_install_passes(home):
    r = _run(CHECKER, home)
    assert r.returncode == 0, r.stdout
    assert "INSTALL CONFORMS" in r.stdout


def test_branch_is_not_a_pin(home):
    """rev-parse reports the same SHA detached or on a branch sitting on it."""
    clone = home / "src" / "verification-ladder"
    _git("checkout", "--quiet", "-B", "main", cwd=clone)
    r = _run(CHECKER, home)
    assert r.returncode == 1
    assert "ON A BRANCH" in r.stdout
    assert "clone at " + PIN in r.stdout  # the SHA check still passes


def test_copy_is_not_a_symlink(home):
    """readlink -f exits 0 and prints a path for a copy too."""
    link = home / ".claude" / "skills" / "verification-ladder"
    target = link.resolve()
    link.unlink()
    shutil.copytree(target, link)
    r = _run(CHECKER, home)
    assert r.returncode == 1
    assert "claude missing or is a copy" in r.stdout


def test_divergent_second_checkout(home, template, tmp_path):
    """A link into another clone defeats one-canonical-checkout."""
    other = tmp_path / "other"
    shutil.copytree(template, other, symlinks=True)
    link = home / ".claude" / "skills" / "verification-ladder"
    link.unlink()
    link.symlink_to(other / "skills" / "verification-ladder")
    r = _run(CHECKER, home)
    assert r.returncode == 1
    assert "DIVERGENT COPIES" in r.stdout


def test_ignored_content_in_skill_tree(home):
    """git status --porcelain cannot see it; for a python verifier it is live."""
    clone = home / "src" / "verification-ladder"
    cache = clone / "skills" / "verification-ladder" / "__pycache__"
    cache.mkdir()
    (cache / "verify.cpython-311.pyc").write_text("x")
    assert _git("status", "--porcelain", cwd=clone).stdout == ""  # invisible
    r = _run(CHECKER, home)
    assert r.returncode == 1
    assert "ignored content inside skills/" in r.stdout


def test_tracked_file_edited_under_the_pin(home):
    clone = home / "src" / "verification-ladder"
    skill = clone / "skills" / "verification-ladder" / "SKILL.md"
    skill.write_text(skill.read_text() + "\ntampered\n")
    r = _run(CHECKER, home)
    assert r.returncode == 1
    assert "tracked files differ from " + PIN in r.stdout


def test_invariant_truncated_to_anchor_lines(home):
    """A file of just the anchor lines passed every earlier grep-based check."""
    (home / ".claude" / "CLAUDE.md").write_text(
        "For every task that modifies repository state\n"
        "BLOCKED, not verified\n"
        "without evidence\n")
    r = _run(CHECKER, home)
    assert r.returncode == 1
    assert "claude invariant PARTIAL or MODIFIED" in r.stdout


def test_invariant_with_one_clause_deleted(home):
    c = home / ".claude" / "CLAUDE.md"
    c.write_text("\n".join(ln for ln in c.read_text().splitlines()
                           if "5. Do not report completion" not in ln) + "\n")
    r = _run(CHECKER, home)
    assert r.returncode == 1
    assert "claude invariant PARTIAL or MODIFIED" in r.stdout


def test_invariant_absent(home):
    (home / ".claude" / "CLAUDE.md").write_text("# unrelated preferences\n")
    r = _run(CHECKER, home)
    assert r.returncode == 1
    assert "claude invariant absent" in r.stdout


@pytest.mark.parametrize("url", [
    "https://evil.example/deep-sixed/verification-ladder.git",   # same last two path components
    "https://github.com/someone-else/verification-ladder",
    "/tmp/deep-sixed/verification-ladder",
])
def test_origin_must_be_this_repository(home, url):
    _git("remote", "set-url", "origin", url, cwd=home / "src" / "verification-ladder")
    r = _run(CHECKER, home)
    assert r.returncode == 1
    assert "not an accepted GitHub URL" in r.stdout


@pytest.mark.parametrize("url", [
    "https://github.com/Deep-Sixed/Verification-Ladder",
    "https://github.com/deep-sixed/verification-ladder.git",
    "git@github.com:Deep-Sixed/Verification-Ladder.git",
    "ssh://git@github.com/Deep-Sixed/Verification-Ladder",
])
def test_accepted_origin_spellings(home, url):
    _git("remote", "set-url", "origin", url, cwd=home / "src" / "verification-ladder")
    r = _run(CHECKER, home)
    assert r.returncode == 0, r.stdout


def test_insteadof_rewrite_is_caught(home):
    """config --get reports the stored URL; git fetches the rewritten one."""
    clone = home / "src" / "verification-ladder"
    _git("config", "url.https://evil.example/.insteadOf", "https://github.com/", cwd=clone)
    assert "github.com" in _git("config", "--get", "remote.origin.url", cwd=clone).stdout
    r = _run(CHECKER, home)
    assert r.returncode == 1
    assert "evil.example" in r.stdout


def test_git_replace_substitutes_the_tree(home):
    """rev-parse reports the pin, status is clean, diff $PIN exits 0."""
    clone = home / "src" / "verification-ladder"
    skill = clone / "skills" / "verification-ladder" / "SKILL.md"
    skill.write_text(skill.read_text() + "\nSUBVERTED\n")
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "evil", cwd=clone)
    evil = _git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    _git("replace", PIN, evil, cwd=clone)
    _git("checkout", "-f", "--quiet", "--detach", PIN, cwd=clone)

    assert _git("rev-parse", "HEAD", cwd=clone).stdout.strip() == PIN
    assert _git("status", "--porcelain", cwd=clone).stdout == ""
    assert "SUBVERTED" in skill.read_text()
    assert _git("diff", "--quiet", PIN, "--", cwd=clone, check=False).returncode == 0

    r = _run(CHECKER, home)
    assert r.returncode == 1
    assert "git replacement refs present" in r.stdout
    # Defence in depth, asserted on the checker itself rather than on git:
    # the tree comparison must run replacement-free, so it reports the
    # substitution independently of the refusal above. Without
    # GIT_NO_REPLACE_OBJECTS this line reads ok and only the refusal remains.
    assert "tracked files differ from " + PIN in r.stdout


def test_replacement_free_diff_is_independent(home):
    """Defence in depth: the tree check catches it without the refusal."""
    clone = home / "src" / "verification-ladder"
    skill = clone / "skills" / "verification-ladder" / "SKILL.md"
    skill.write_text(skill.read_text() + "\nSUBVERTED\n")
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "evil", cwd=clone)
    evil = _git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    _git("replace", PIN, evil, cwd=clone)
    _git("checkout", "-f", "--quiet", "--detach", PIN, cwd=clone)

    plain = _git("diff", "--quiet", PIN, "--", cwd=clone, check=False).returncode
    noreplace = subprocess.run(
        ["git", "-C", str(clone), "diff", "--quiet", PIN, "--"], check=False,
        env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}).returncode
    assert plain == 0, "plain diff should be deceived"
    assert noreplace != 0, "replacement-free diff must not be"


def test_grafts_are_refused(home):
    """--git-path is repo-relative: an early version resolved it against cwd."""
    clone = home / "src" / "verification-ladder"
    gitdir = Path(_git("rev-parse", "--absolute-git-dir", cwd=clone).stdout.strip())
    (gitdir / "info").mkdir(exist_ok=True)
    (gitdir / "info" / "grafts").write_text(PIN + "\n")
    r = _run(CHECKER, home)
    assert r.returncode == 1
    assert "non-empty grafts file" in r.stdout


def test_clean_clone_reports_no_grafts(home):
    """The companion to the above: no false positive on a clean clone."""
    r = _run(CHECKER, home)
    assert "ok    no grafts" in r.stdout


def test_failed_probe_is_not_positive_evidence(tmp_path):
    """A git invocation that could not run must never read as a clean tree."""
    h = tmp_path / "empty"
    (h / ".claude" / "skills").mkdir(parents=True)
    r = _run(CHECKER, h)
    assert r.returncode == 1
    assert "could not inspect repository status" in r.stdout


def test_checker_rejects_unknown_arguments(home):
    """A typo must not silently narrow the scope and still exit 0."""
    r = _run(CHECKER, home, "--codez")
    assert r.returncode == 64
    assert "unknown arg" in r.stderr


def test_codex_scope_is_reported(home):
    assert "scope: claude only" in _run(CHECKER, home).stdout


def test_codex_invariant_is_checked_not_assumed(home):
    """--codex linking the skill while Codex has no invariant must not CONFORM."""
    codex = home / ".codex"
    (codex / "skills").mkdir(parents=True)
    (codex / "skills" / "verification-ladder").symlink_to(
        home / "src" / "verification-ladder" / "skills" / "verification-ladder")
    r = _run(CHECKER, home, "--codex")
    assert r.returncode == 1
    assert "codex invariant absent" in r.stdout
    assert "scope: claude + codex" in r.stdout


# --------------------------------------------------------------------------
# The installer: cases that abort before any network access.
# --------------------------------------------------------------------------

def test_installer_rejects_unknown_arguments(tmp_path):
    r = _run(INSTALLER, tmp_path, "--pin", PIN)
    assert r.returncode == 64
    assert "unknown arg" in r.stderr


def test_installer_refuses_missing_sibling(tmp_path):
    """$PIN does not contain check-install.sh, so it cannot be recovered later."""
    solo = tmp_path / "bin"
    solo.mkdir()
    shutil.copy(INSTALLER, solo / "install-ladder.sh")
    r = _run(solo / "install-ladder.sh", tmp_path)
    assert r.returncode == 1
    assert "check-install.sh is not beside" in r.stderr
    assert not (tmp_path / ".local" / "share" / "verification-ladder").exists(), \
        "must abort before creating the bootstrap directory"


def test_installer_refuses_foreign_origin(home):
    _git("remote", "set-url", "origin", "https://evil.example/deep-sixed/verification-ladder",
         cwd=home / "src" / "verification-ladder")
    r = _run(INSTALLER, home)
    assert r.returncode == 1
    assert "not this repository" in r.stderr


def test_installer_refuses_replacement_refs(home):
    clone = home / "src" / "verification-ladder"
    head = _git("rev-parse", "HEAD~1", cwd=clone).stdout.strip()
    _git("replace", PIN, head, cwd=clone)
    r = _run(INSTALLER, home)
    assert r.returncode == 1
    assert "replacement refs" in r.stderr


def test_installer_refuses_dirty_clone(home):
    clone = home / "src" / "verification-ladder"
    skill = clone / "skills" / "verification-ladder" / "SKILL.md"
    skill.write_text(skill.read_text() + "\nlocal edit\n")
    r = _run(INSTALLER, home)
    assert r.returncode == 1
    assert "local modifications" in r.stderr
    assert "not overwriting your work" in r.stderr


@pytest.mark.network
def test_installer_refuses_copy_instead_of_symlink(home):
    """Without --force-link a copied skill tree is never silently replaced."""
    link = home / ".claude" / "skills" / "verification-ladder"
    target = link.resolve()
    link.unlink()
    shutil.copytree(target, link)
    r = _run(INSTALLER, home)
    assert r.returncode == 1
    assert "NOT a symlink" in r.stderr


def test_dry_run_without_a_clone_is_honest(tmp_path):
    """It cannot validate the invariant, and says so rather than failing vaguely."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(INSTALLER, bin_dir / "install-ladder.sh")
    shutil.copy(CHECKER, bin_dir / "check-install.sh")
    r = _run(bin_dir / "install-ladder.sh", tmp_path, "--dry-run")
    assert r.returncode == 2
    assert "DRY-RUN INCOMPLETE" in r.stdout + r.stderr


@pytest.mark.network
def test_invariant_checksum_gate_fails_closed(home, tmp_path):
    """A pin moved without revalidating INV_SHA must not paste unreviewed text.

    Simulated by corrupting INV_SHA rather than by drifting INSTALL.md at a new
    commit: the ancestry check refuses an unreachable pin first, so that route
    never reaches this gate. From the gate's side the two are the same — the
    extracted block does not hash to what the reviewed installer expects.
    """
    (home / ".claude" / "CLAUDE.md").write_text("# pre-existing, no invariant\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    src = INSTALLER.read_text().replace("INV_SHA=282f21173aad9815a22a6ece35ad0fdd42653dd7a001dc6d85e6d51189aa5699",
                                        "INV_SHA=" + "0" * 64)
    assert "INV_SHA=" + "0" * 64 in src, "INV_SHA literal not found to corrupt"
    (bin_dir / "install-ladder.sh").write_text(src)
    shutil.copy(CHECKER, bin_dir / "check-install.sh")

    r = _run(bin_dir / "install-ladder.sh", home)
    assert r.returncode == 1
    assert "INV_SHA mismatch" in r.stderr
    assert "INCOMPLETE" in r.stdout
    text = (home / ".claude" / "CLAUDE.md").read_text()
    assert "For every task that modifies repository state" not in text, \
        "refusing must leave global instructions untouched"


@pytest.mark.network
def test_unreachable_pin_is_refused_before_anything_is_written(home, tmp_path):
    """A stray local commit cannot stand in for the pin."""
    clone = home / "src" / "verification-ladder"
    (clone / "STRAY.md").write_text("stray\n")
    _git("add", "STRAY.md", cwd=clone)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "stray", cwd=clone)
    stray = _git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    _git("checkout", "--quiet", "--detach", PIN, cwd=clone)
    (clone / "STRAY.md").unlink(missing_ok=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "install-ladder.sh").write_text(
        INSTALLER.read_text().replace("PIN=" + PIN, "PIN=" + stray))
    shutil.copy(CHECKER, bin_dir / "check-install.sh")

    r = _run(bin_dir / "install-ladder.sh", home)
    assert r.returncode == 1
    assert "not an ancestor of origin/main" in r.stderr


# --------------------------------------------------------------------------
# The pair: an installer and the checker it was reviewed with.
# --------------------------------------------------------------------------

def _checker_sha_in_installer():
    m = re.search(r"^CHECKER_SHA=([0-9a-f]{64})$", INSTALLER.read_text(), re.MULTILINE)
    assert m, "CHECKER_SHA constant not found in the installer"
    return m.group(1)


def test_checker_digest_constant_is_current():
    """The constant is only worth anything while it names the committed checker.

    Without this, editing check-install.sh and forgetting CHECKER_SHA leaves a
    green suite and an installer that refuses its own repository's checker.
    """
    assert _checker_sha_in_installer() == hashlib.sha256(CHECKER.read_bytes()).hexdigest()


def test_installer_refuses_a_checker_it_was_not_reviewed_with(tmp_path):
    """Existence is not identity: a newer installer must not preserve an older checker.

    Reproduced before the fix by pairing this installer with the checker from
    05eb041 - 61 lines against 207 - which installed cleanly, exit 0. Both were
    then copied to the bootstrap directory and the weaker one is what anyone
    would have run afterwards.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(INSTALLER, bin_dir / "install-ladder.sh")
    (bin_dir / "check-install.sh").write_text(CHECKER.read_text() + "# drifted\n")
    drifted = hashlib.sha256((bin_dir / "check-install.sh").read_bytes()).hexdigest()

    r = _run(bin_dir / "install-ladder.sh", tmp_path, "--dry-run")
    assert r.returncode == 1
    assert drifted in r.stderr
    assert _checker_sha_in_installer() in r.stderr
    assert "one reviewed pair" in r.stderr
    assert not (tmp_path / ".local" / "share" / "verification-ladder").exists(), \
        "must abort before creating the bootstrap directory"


def test_matching_pair_passes_the_digest_check(tmp_path):
    """The refusal above must be about the digest, not about running from a copy."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(INSTALLER, bin_dir / "install-ladder.sh")
    shutil.copy(CHECKER, bin_dir / "check-install.sh")
    r = _run(bin_dir / "install-ladder.sh", tmp_path, "--dry-run")
    assert "checker matches the reviewed pair" in r.stdout
    assert "one reviewed pair" not in r.stderr


@pytest.mark.parametrize("stub, why", [
    ("#!/bin/sh\nexit 1\n", "the tool fails"),
    ("#!/bin/sh\nexit 0\n", "the tool succeeds but prints nothing"),
])
def test_digest_read_failure_is_not_a_mismatch(tmp_path, stub, why):
    """A probe that could not run is not evidence, in either direction.

    Reading the digest through `sha256sum | cut` would bind `|| die` to `cut`,
    which exits 0 on empty input: a failed digest would then be compared as an
    empty string and reported as a *mismatch*, blaming the checker for the
    machine. Both modes must abort with the same honest reason instead.

    Driven through PATH rather than by making the file unreadable, because the
    suite may run as root, and root reads a chmod 000 file.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(INSTALLER, bin_dir / "install-ladder.sh")
    shutil.copy(CHECKER, bin_dir / "check-install.sh")
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "sha256sum").write_text(stub)
    (stub_dir / "sha256sum").chmod(0o755)

    r = _run(bin_dir / "install-ladder.sh", tmp_path, "--dry-run", path_prefix=stub_dir)
    assert r.returncode == 1, why
    assert "could not digest" in r.stderr, why
    assert "one reviewed pair" not in r.stderr, "a failed probe must not read as a mismatch"
    assert not (tmp_path / ".local" / "share" / "verification-ladder").exists()


def test_network_surface_has_not_moved():
    """Guard the assumption the markers rest on, not the markers themselves.

    This cannot prove a marked case still needs the network - only a run under
    denial does that, and it is recorded in the module docstring. What it can do
    is fail when the scripts grow a second way to reach the network, which is
    when that recorded measurement stops applying.
    """
    src = INSTALLER.read_text()
    assert src.count("fetch --quiet origin") == 1, \
        "the network surface moved; re-measure which cases reach it"
    assert "git clone --quiet" in src


def test_installer_and_checker_are_posix_sh(tmp_path):
    for script in (INSTALLER, CHECKER):
        assert subprocess.run(["sh", "-n", str(script)], check=False).returncode == 0, script
