#!/bin/sh
# Verification Ladder installer - steps 1-5 of the install sequence.
# POSIX sh SYNTAX, targeting GNU/Linux: uses readlink -f, mktemp and python3.
# Idempotent. Never overwrites existing global instructions.
# Usage: install-ladder.sh [--codex] [--dry-run] [--force-link]
#
# There is deliberately no --pin. Changing the pinned revision means changing
# PIN and INV_SHA below, which is a reviewed change to this file rather than a
# command-line argument that no reviewer sees. CHECKER_SHA binds this installer
# to the checker it was reviewed with, for the same reason.
set -u

PIN=c86bf47e70d7a3aa18c968011b487190e2becbea
REPO=https://github.com/Deep-Sixed/Verification-Ladder
# sha256 of the invariant block in INSTALL.md at $PIN. Moving the pin without
# revalidating this makes the installer fail closed rather than paste unknown
# text into global agent instructions.
INV_SHA=282f21173aad9815a22a6ece35ad0fdd42653dd7a001dc6d85e6d51189aa5699
CLONE=$HOME/src/verification-ladder
CODEX_HOME=${CODEX_HOME:-$HOME/.codex}
# Bootstrap tooling lives OUTSIDE the pinned checkout. Detaching $CLONE to $PIN
# replaces this pair with $PIN's own copy - an older pair, reviewed with an
# older pin - including this file while it is running. They are copied here
# first so that the pair reviewed with THIS pin is the one that survives.
BOOTSTRAP_DIR=${XDG_DATA_HOME:-$HOME/.local/share}/verification-ladder
# sha256 of the check-install.sh this installer was reviewed with. The two are
# reviewed as a pair and preserved as a pair, so requiring the checker merely to
# EXIST beside the installer accepts a new installer carrying an older, weaker
# checker - both are then copied out and the weaker one is what anyone runs
# afterwards. Changing check-install.sh means changing this constant in the same
# commit; a mismatch is refused rather than reported, because the checker is the
# only thing that would have caught it.
CHECKER_SHA=d7b922510547b8f9d856a897bd493a4a5db3785f2d2fd15fc259e542dcf6ce07
SELF=$(readlink -f "$0" 2>/dev/null || echo "$0")
SELF_DIR=$(dirname "$SELF")
DO_CODEX=0; DRY=0; FORCE=0

while [ $# -gt 0 ]; do
  case $1 in
    --codex) DO_CODEX=1 ;;
    --dry-run) DRY=1 ;;
    --force-link) FORCE=1 ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
  shift
done

say()  { echo "  $*"; }
step() { echo; echo "== $*"; }
die()  { echo "ABORT: $*" >&2; exit 1; }

# argv execution, no eval: a failing command aborts instead of being ignored.
run() {
  if [ "$DRY" -eq 1 ]; then
    printf '  [dry-run]'; for a in "$@"; do printf ' %s' "$a"; done; printf '\n'
    return 0
  fi
  "$@" || die "command failed: $*"
}

# Accept only explicit GitHub origins for this repository. Reducing an
# arbitrary URL to its last two path components would accept any host.
# get-url, not `config --get`: the latter ignores url.*.insteadOf rewriting,
# so the stored URL can differ from the one git actually fetches.
origin_ok() {
  case "$(printf '%s' "${1:-}" | tr 'A-Z' 'a-z')" in
    https://github.com/deep-sixed/verification-ladder|\
    https://github.com/deep-sixed/verification-ladder.git|\
    git@github.com:deep-sixed/verification-ladder|\
    git@github.com:deep-sixed/verification-ladder.git|\
    ssh://git@github.com/deep-sixed/verification-ladder|\
    ssh://git@github.com/deep-sixed/verification-ladder.git) return 0 ;;
    *) return 1 ;;
  esac
}
verify_origin() {
  u=$(git -C "$CLONE" remote get-url origin 2>/dev/null) \
    || die "cannot read origin of $CLONE"
  origin_ok "$u" || die "$CLONE origin is '${u:-<none>}', not this repository"
  say "origin verified: $u"
}

# git replace substitutes object content beneath every ordinary check:
# rev-parse reports the pin, the worktree holds another commit's bytes, and
# `status` and `diff $PIN` both read clean. Grafts rewrite ancestry similarly.
# Refuse either outright - the verifier installed here also invokes git.
refuse_history_rewrites() {
  reps=$(git -C "$CLONE" replace -l 2>/dev/null) \
    || die "could not inspect replacement refs in $CLONE"
  [ -z "$reps" ] \
    && say "no replacement refs" \
    || die "$CLONE contains git replacement refs ($(printf '%s' "$reps" | tr '\n' ' ')); refusing canonical install"
  # --absolute-git-dir, because --git-path returns a path relative to the
  # repository and testing it would resolve against the caller's cwd instead.
  gd=$(git -C "$CLONE" rev-parse --absolute-git-dir 2>/dev/null) \
    || die "could not locate the git directory of $CLONE"
  [ -s "$gd/info/grafts" ] \
    && die "$CLONE has a non-empty grafts file ($gd/info/grafts); refusing canonical install"
  say "no grafts"
}

CANON=$(mktemp "${TMPDIR:-/tmp}/ladder-invariant.XXXXXX") \
  || { echo "ABORT: could not create temporary file" >&2; exit 1; }
cleanup() { rm -f "$CANON"; }
trap 'cleanup' EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

invariant_extract() {
  python3 - "$CLONE/INSTALL.md" "$INV_SHA" "$CANON" <<'PY'
import sys, hashlib
src, want, out = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    text = open(src, encoding="utf-8").read()
except OSError as exc:
    sys.exit("cannot read %s: %s" % (src, exc))
block, in_section, in_fence, closed = [], False, False, False
for line in text.splitlines():
    if line.startswith("## The invariant"):
        in_section = True
        continue
    if in_section and not in_fence and line.strip() == "```markdown":
        in_fence = True
        continue
    if in_fence:
        if line.strip() == "```":
            closed = True
            break
        block.append(line)
if not block or not closed:
    sys.exit("invariant block not found or fence unclosed in %s" % src)
body = "\n".join(block) + "\n"
got = hashlib.sha256(body.encode("utf-8")).hexdigest()
if got != want:
    sys.exit("INV_SHA mismatch\n    expected %s\n    got      %s\n"
             "    Read INSTALL.md at this revision, confirm the block, update INV_SHA."
             % (want, got))
open(out, "w", encoding="utf-8").write(body)
PY
}

# exact | partial | absent - exact requires the whole contiguous block,
# not a set of anchor lines that a gutted file would also satisfy.
invariant_state() {
  python3 - "$CANON" "$1" <<'PY'
import sys, os
canon = open(sys.argv[1], encoding="utf-8").read().strip()
target = sys.argv[2]
if not os.path.exists(target):
    print("absent"); raise SystemExit
try:
    text = open(target, encoding="utf-8").read()
except OSError:
    print("unreadable"); raise SystemExit
if canon in text:
    print("exact")
elif ("For every task that modifies repository state" in text
      or "you hold for the current state." in text):
    print("partial")
else:
    print("absent")
PY
}

# ---- step 1: inspect before touching anything -------------------------
step "1. Inspecting current state"
for p in "$CLONE" "$HOME/.claude/skills/verification-ladder" "$HOME/.claude/CLAUDE.md" \
         "$CODEX_HOME/skills/verification-ladder" "$CODEX_HOME/AGENTS.override.md" \
         "$CODEX_HOME/AGENTS.md"; do
  if   [ -L "$p" ]; then say "symlink  $p -> $(readlink "$p")"
  elif [ -d "$p" ]; then say "dir      $p"
  elif [ -e "$p" ]; then say "file     $p ($(wc -l <"$p") lines)"
  else                   say "absent   $p"
  fi
done

# ---- step 1b: preserve bootstrap tooling outside the pinned checkout ---
step "1b. Preserving bootstrap tooling in $BOOTSTRAP_DIR"
preserve_bootstrap() {
  # Fail closed: the only checker reviewed with this installer is the one beside
  # it. $PIN's own copy is an older checker, reviewed with an older pin, so if
  # this one is not here now it cannot be recovered later, and the closing
  # message would name a checker that was never preserved.
  [ -f "$SELF_DIR/check-install.sh" ] \
    || die "check-install.sh is not beside $SELF; refusing because $PIN carries only an older checker"
  # Identity, not existence. Checked before the early return below, so a re-run
  # from $BOOTSTRAP_DIR re-establishes the pair rather than trusting what an
  # earlier run left there.
  # Not piped into `cut`: `|| die` would then test the pipeline's last command,
  # which exits 0 on empty input, and a failed digest would read as a mismatch
  # or worse. Strip the filename with parameter expansion instead.
  got=$(sha256sum "$SELF_DIR/check-install.sh" 2>/dev/null) \
    || die "could not digest $SELF_DIR/check-install.sh"
  got=${got%% *}
  [ ${#got} -eq 64 ] || die "could not digest $SELF_DIR/check-install.sh"
  [ "$got" = "$CHECKER_SHA" ] || die "check-install.sh beside $SELF is sha256:$got,
    but this installer was reviewed against sha256:$CHECKER_SHA.
    The installer and checker are one reviewed pair; install them from one revision."
  say "checker matches the reviewed pair (sha256:$CHECKER_SHA)"
  if [ "$SELF_DIR" = "$BOOTSTRAP_DIR" ]; then
    say "already running from the bootstrap directory - nothing to copy"
    return 0
  fi
  run mkdir -p "$BOOTSTRAP_DIR"
  run cp "$SELF" "$BOOTSTRAP_DIR/install-ladder.sh"
  run chmod +x "$BOOTSTRAP_DIR/install-ladder.sh"
  run cp "$SELF_DIR/check-install.sh" "$BOOTSTRAP_DIR/check-install.sh"
  run chmod +x "$BOOTSTRAP_DIR/check-install.sh"
  [ "$DRY" -eq 1 ] && return 0
  # Provenance: once copied out, these files are detached from the commit that
  # produced them and would otherwise have no identity at all.
  src_rev=$(git -C "$SELF_DIR" rev-parse HEAD 2>/dev/null) || src_rev="not a git checkout"
  {
    echo "installed-at:    $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "source-dir:      $SELF_DIR"
    echo "source-revision: $src_rev"
    echo "ladder-pin:      $PIN"
    echo "checker-sha:     sha256:$CHECKER_SHA"
    for f in install-ladder.sh check-install.sh; do
      [ -f "$BOOTSTRAP_DIR/$f" ] && echo "$f: sha256:$(sha256sum "$BOOTSTRAP_DIR/$f" | cut -d" " -f1)"
    done
  } > "$BOOTSTRAP_DIR/PROVENANCE" || die "could not write $BOOTSTRAP_DIR/PROVENANCE"
  say "recorded provenance (source revision $src_rev)"
}
preserve_bootstrap

# ---- step 2-3: clone / update, then detach at the pin -----------------
step "2-3. Canonical checkout at $PIN"
if [ -d "$CLONE/.git" ]; then
  verify_origin
  refuse_history_rewrites
  status=$(git -C "$CLONE" status --porcelain 2>/dev/null) \
    || die "cannot inspect $CLONE status"
  [ -n "$status" ] && die "$CLONE has local modifications; resolve them first (not overwriting your work)"
  run git -C "$CLONE" fetch --quiet origin
else
  run mkdir -p "$(dirname "$CLONE")"
  run git clone --quiet "$REPO" "$CLONE"
fi

if [ "$DRY" -eq 0 ]; then
  # Unconditional: a fresh clone can be redirected by url.*.insteadOf, so the
  # effective origin must be checked after cloning too, not only before.
  verify_origin
  refuse_history_rewrites
  # GIT_NO_REPLACE_OBJECTS so these read real objects, not substituted ones.
  GIT_NO_REPLACE_OBJECTS=1 git -C "$CLONE" cat-file -e "$PIN^{commit}" 2>/dev/null \
    || die "commit $PIN not found in $CLONE"
  GIT_NO_REPLACE_OBJECTS=1 git -C "$CLONE" merge-base --is-ancestor "$PIN" refs/remotes/origin/main 2>/dev/null \
    || die "$PIN is not an ancestor of origin/main - refusing to install an unreachable revision"
  say "pin reachable from origin/main"
  run git -C "$CLONE" checkout --quiet --detach "$PIN"
  say "detached at $(git -C "$CLONE" rev-parse HEAD)"
  GIT_NO_REPLACE_OBJECTS=1 git -C "$CLONE" diff --quiet "$PIN" -- 2>/dev/null \
    || die "checked-out tree differs from $PIN"
  # The checker treats ignored content under skills/ as non-conforming; do not
  # link a tree this installer's own acceptance definition would reject.
  stow=$(git -C "$CLONE" status --porcelain --ignored -- skills/ 2>/dev/null) \
    || die "could not inspect ignored content under skills/"
  [ -n "$stow" ] && die "ignored content inside skills/ ($(printf '%s' "$stow" | tr '\n' ' ')); refusing to link it"
  say "skill tree free of ignored content"
fi

# ---- step 4: symlink, never a copy ------------------------------------
link_skill() {
  root=$1; link=$root/verification-ladder; target=$CLONE/skills/verification-ladder
  if [ -L "$link" ]; then
    if [ "$(readlink -f "$link")" = "$target" ]; then say "already linked correctly: $link"; return 0; fi
    say "relinking $link (was -> $(readlink "$link"))"; run rm "$link"
  elif [ -e "$link" ]; then
    [ "$FORCE" -eq 0 ] && die "$link exists and is NOT a symlink (a copy). Re-run with --force-link to back it up and replace."
    say "backing up copy -> $link.bak.$$"; run mv "$link" "$link.bak.$$"
  fi
  run mkdir -p "$root"
  run ln -s "$target" "$link"
  say "linked $link"
}
step "4. Skill symlinks"
link_skill "$HOME/.claude/skills"
[ "$DO_CODEX" -eq 1 ] && link_skill "$CODEX_HOME/skills"

# ---- step 5: append invariant, sourced from the pin, never overwrite --
step "5. Global invariant (appended, never overwritten)"
INV_RC=0
if [ "$DRY" -eq 1 ] && [ ! -e "$CLONE/INSTALL.md" ]; then
  echo "  DRY-RUN INCOMPLETE: no clone yet, so the invariant cannot be extracted" >&2
  echo "    or checksum-verified. --dry-run only validates step 5 when \$CLONE exists." >&2
  INV_RC=2
elif ! invariant_extract; then
  INV_RC=1
else
  say "invariant extracted and checksum-verified ($INV_SHA)"
fi

install_invariant() {
  c=$1; label=$2
  state=$(invariant_state "$c")
  case $state in
    exact)  say "$label: canonical invariant already present in $c - nothing to do"; return 0 ;;
    partial)
      echo "  $label: PARTIAL or MODIFIED invariant in $c." >&2
      echo "    The canonical block is not present verbatim, but invariant-like text is." >&2
      echo "    Refusing to append - that would duplicate or contradict it. Merge by hand." >&2
      return 1 ;;
    unreadable) echo "  $label: cannot read $c" >&2; return 1 ;;
  esac
  if [ -e "$c" ]; then
    say "$label: backing up -> $c.bak.$$"; run cp "$c" "$c.bak.$$"
    say "$label: appending invariant to existing $c"
  else
    run mkdir -p "$(dirname "$c")"; say "$label: creating $c"
  fi
  [ "$DRY" -eq 1 ] && { echo "  [dry-run] append canonical invariant to $c"; return 0; }
  { [ -s "$c" ] && printf '\n'; printf '# Verification\n\n'; cat "$CANON"; } >> "$c" \
    || { echo "  $label: append to $c failed" >&2; return 1; }
  return 0
}

# Codex reads its global instructions from AGENTS.override.md when present,
# otherwise AGENTS.md, both under CODEX_HOME.
codex_instruction_file() {
  if [ -e "$CODEX_HOME/AGENTS.override.md" ]; then echo "$CODEX_HOME/AGENTS.override.md"
  else echo "$CODEX_HOME/AGENTS.md"; fi
}

if [ "$INV_RC" -eq 0 ]; then
  install_invariant "$HOME/.claude/CLAUDE.md" "claude" || INV_RC=1
  if [ "$DO_CODEX" -eq 1 ]; then
    install_invariant "$(codex_instruction_file)" "codex" || INV_RC=1
  fi
fi

echo
if [ "$INV_RC" -eq 2 ]; then
  echo "DRY-RUN INCOMPLETE: step 5 was not validated (see above)."
  exit 2
fi
if [ "$INV_RC" -ne 0 ]; then
  echo "INCOMPLETE: skill is linked, but the global invariant was NOT installed."
  echo "Without it the agent has no instruction to invoke the Ladder."
  echo "Resolve the reason printed above, then re-run."
  exit 1
fi
echo "Steps 1-5 done. Next:"
[ "$DO_CODEX" -eq 1 ] && CODEX_ARG=" --codex" || CODEX_ARG=""
echo "  $BOOTSTRAP_DIR/check-install.sh$CODEX_ARG"
echo "then the discovery test, then the agent-level policy-less BLOCKED test."
echo "(Run the pair in $BOOTSTRAP_DIR. \$CLONE is pinned at $PIN, and the"
echo " scripts/ inside it are that revision's older pair, reviewed with an older pin.)"
