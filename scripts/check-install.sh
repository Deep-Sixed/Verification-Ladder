#!/bin/sh
# Verification Ladder install check. Run on the target machine.
# POSIX sh SYNTAX, targeting GNU/Linux: uses readlink -f, mktemp and python3.
# Usage: check-install.sh [--codex]
#
# Establishes INSTALLATION INTEGRITY only. It does not establish that an agent
# invokes the Ladder during ordinary work; that is a behavioral test against a
# repository with no verification policy.
set -u

PIN=760977f69d6b74b9888be4c9cbb404f7726bea73
INV_SHA=282f21173aad9815a22a6ece35ad0fdd42653dd7a001dc6d85e6d51189aa5699
CLONE=$HOME/src/verification-ladder
LINK=$HOME/.claude/skills/verification-ladder
CODEX_HOME=${CODEX_HOME:-$HOME/.codex}
CHECK_CODEX=0

# Strict parsing: a typo must not silently narrow the scope of the check and
# still exit 0, which automation reading only the status would take as a pass.
while [ $# -gt 0 ]; do
  case $1 in
    --codex) CHECK_CODEX=1 ;;
    *) echo "unknown arg: $1" >&2; echo "usage: check-install.sh [--codex]" >&2; exit 64 ;;
  esac
  shift
done

rc=0
fail() { echo "FAIL  $1"; rc=1; }
ok()   { echo "ok    $1"; }

CANON=$(mktemp "${TMPDIR:-/tmp}/ladder-invariant-check.XXXXXX") \
  || { echo "could not create temporary file" >&2; exit 1; }
cleanup() { rm -f "$CANON"; }
trap 'cleanup' EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

HAVE_GIT=0
git -C "$CLONE" rev-parse --git-dir >/dev/null 2>&1 && HAVE_GIT=1

# --- property 1a: identity of the checkout -----------------------------
# get-url, not `config --get`: the latter ignores url.*.insteadOf rewriting.
ORIGIN=$(git -C "$CLONE" remote get-url origin 2>/dev/null) || ORIGIN=""
case "$(printf '%s' "$ORIGIN" | tr 'A-Z' 'a-z')" in
  https://github.com/deep-sixed/verification-ladder|\
  https://github.com/deep-sixed/verification-ladder.git|\
  git@github.com:deep-sixed/verification-ladder|\
  git@github.com:deep-sixed/verification-ladder.git|\
  ssh://git@github.com/deep-sixed/verification-ladder|\
  ssh://git@github.com/deep-sixed/verification-ladder.git)
    ok "origin is this repository ($ORIGIN)" ;;
  *) fail "origin is '${ORIGIN:-<none>}', not an accepted GitHub URL for this repository" ;;
esac

# --- property 1a': no object substitution ------------------------------
# A replacement ref makes rev-parse report the pin while the worktree holds
# another commit's bytes, with `status` and `diff $PIN` both reading clean.
if [ "$HAVE_GIT" -eq 1 ]; then
  if REPS=$(git -C "$CLONE" replace -l 2>/dev/null); then
    [ -z "$REPS" ] && ok "no git replacement refs" \
      || fail "git replacement refs present ($(printf '%s' "$REPS" | tr '\n' ' ')) - the pin can name one tree while git serves another"
  else
    fail "could not inspect replacement refs"
  fi
  # --absolute-git-dir, because --git-path returns a path relative to the
  # repository and testing it would resolve against the caller's cwd instead.
  if GITDIR=$(git -C "$CLONE" rev-parse --absolute-git-dir 2>/dev/null); then
    [ -s "$GITDIR/info/grafts" ] \
      && fail "non-empty grafts file ($GITDIR/info/grafts) - ancestry is rewritten" \
      || ok "no grafts"
  else
    fail "could not locate the git directory of $CLONE"
  fi
else
  fail "no git repository at $CLONE"
fi

HEAD_SHA=$(git -C "$CLONE" rev-parse HEAD 2>/dev/null) || HEAD_SHA=""
[ "$HEAD_SHA" = "$PIN" ] && ok "clone at $PIN" || fail "clone at '${HEAD_SHA:-<none>}', expected $PIN"

if [ "$HAVE_GIT" -eq 1 ]; then
  git -C "$CLONE" symbolic-ref -q HEAD >/dev/null 2>&1 \
    && fail "clone is ON A BRANCH ($(git -C "$CLONE" symbolic-ref --short HEAD)) - not a pin" \
    || ok "clone detached"
fi

# --- property 1b: the pinned TREE, not just the pinned HEAD ------------
# GIT_NO_REPLACE_OBJECTS so this compares real objects, not substituted ones.
if [ "$HAVE_GIT" -eq 1 ]; then
  GIT_NO_REPLACE_OBJECTS=1 git -C "$CLONE" diff --quiet "$PIN" -- 2>/dev/null \
    && ok "tracked tree identical to pin (replacement-free)" \
    || fail "tracked files differ from $PIN"
else
  fail "cannot diff tree: no git repository at $CLONE"
fi

# A probe that could not run is never positive evidence.
if STATE=$(git -C "$CLONE" status --porcelain 2>/dev/null); then
  [ -z "$STATE" ] && ok "no untracked/modified files" \
                  || fail "untracked or modified files present"
else
  fail "could not inspect repository status"
fi

# Ignored content is invisible to --porcelain but is LIVE for a python verifier.
if STOWAWAY=$(git -C "$CLONE" status --porcelain --ignored -- skills/ 2>/dev/null); then
  [ -z "$STOWAWAY" ] && ok "skill tree free of ignored content" \
    || fail "ignored content inside skills/: $(printf '%s' "$STOWAWAY" | tr '\n' ' ')"
else
  fail "could not inspect ignored content under skills/"
fi

# --- property 1c: link topology ----------------------------------------
check_link() {
  lbl=$1; l=$2
  [ -L "$l" ] && ok "$lbl is a symlink" || { fail "$lbl missing or is a copy"; return; }
  [ "$(readlink -f "$l")" = "$CLONE/skills/verification-ladder" ] \
    && ok "$lbl resolves into the pinned clone" \
    || fail "$lbl resolves elsewhere ($(readlink -f "$l")) - DIVERGENT COPIES"
}
check_link "claude" "$LINK"
[ "$CHECK_CODEX" -eq 1 ] && check_link "codex" "$CODEX_HOME/skills/verification-ladder"

# --- property 1d: the COMPLETE canonical invariant ----------------------
# Anchor greps pass on a file holding only the anchor lines. Require the whole
# contiguous block, extracted from the pinned INSTALL.md and checksum-verified.
if python3 - "$CLONE/INSTALL.md" "$INV_SHA" "$CANON" <<'PY' 2>/dev/null
import sys, hashlib
src, want, out = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(src, encoding="utf-8").read()
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
    sys.exit(1)
body = "\n".join(block) + "\n"
if hashlib.sha256(body.encode("utf-8")).hexdigest() != want:
    sys.exit(1)
open(out, "w", encoding="utf-8").write(body)
PY
then
  ok "canonical invariant extracted from pin and checksum-verified"
  check_invariant() {
    lbl=$1; f=$2
    state=$(python3 - "$CANON" "$f" <<'PY'
import sys, os
canon = open(sys.argv[1], encoding="utf-8").read().strip()
t = sys.argv[2]
if not os.path.exists(t):
    print("absent"); raise SystemExit
try:
    text = open(t, encoding="utf-8").read()
except OSError:
    print("unreadable"); raise SystemExit
if canon in text: print("exact")
elif ("For every task that modifies repository state" in text
      or "you hold for the current state." in text): print("partial")
else: print("absent")
PY
)
    case $state in
      exact)   ok "$lbl invariant complete and verbatim ($f)" ;;
      partial) fail "$lbl invariant PARTIAL or MODIFIED in $f - not the canonical block" ;;
      absent)  fail "$lbl invariant absent from $f" ;;
      *)       fail "$lbl invariant could not be read from $f" ;;
    esac
  }
  check_invariant "claude" "$HOME/.claude/CLAUDE.md"
  if [ "$CHECK_CODEX" -eq 1 ]; then
    if [ -e "$CODEX_HOME/AGENTS.override.md" ]; then CODEX_INSTR=$CODEX_HOME/AGENTS.override.md
    else CODEX_INSTR=$CODEX_HOME/AGENTS.md; fi
    check_invariant "codex" "$CODEX_INSTR"
  fi
else
  fail "could not extract/verify the canonical invariant from $CLONE/INSTALL.md"
  fail "invariant presence therefore UNCHECKED - not passing"
fi

python3 "$LINK/bin/verify.py" --help >/dev/null 2>&1 \
  && ok "verify.py executes via link" || fail "verify.py not runnable via link"

echo
[ "$CHECK_CODEX" -eq 1 ] && echo "(scope: claude + codex)" || echo "(scope: claude only - codex not checked)"
[ $rc -eq 0 ] && echo "INSTALL CONFORMS" || echo "INSTALL DOES NOT CONFORM"
exit $rc
