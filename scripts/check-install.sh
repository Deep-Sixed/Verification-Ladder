#!/bin/sh
# Verification Ladder install check. Run on the target machine.
# Usage: check-install.sh [--codex]
PIN=760977f69d6b74b9888be4c9cbb404f7726bea73
SLUG=deep-sixed/verification-ladder          # expected origin, case-insensitive
CLONE=$HOME/src/verification-ladder
LINK=$HOME/.claude/skills/verification-ladder
CHECK_CODEX=0; [ "${1:-}" = "--codex" ] && CHECK_CODEX=1
rc=0
fail() { echo "FAIL  $1"; rc=1; }
ok()   { echo "ok    $1"; }

# --- property 1a: identity of the checkout -----------------------------
ORIGIN=$(git -C "$CLONE" config --get remote.origin.url 2>/dev/null)
NORM=$(printf '%s' "$ORIGIN" | tr 'A-Z' 'a-z' | sed -e 's/\.git$//' -e 's#.*[/:]\([^/]*/[^/]*\)$#\1#')
[ "$NORM" = "$SLUG" ] && ok "origin is $SLUG" \
  || fail "origin is '${ORIGIN:-<none>}', expected $SLUG"

[ "$(git -C "$CLONE" rev-parse HEAD 2>/dev/null)" = "$PIN" ] \
  && ok "clone at $PIN" || fail "clone not at $PIN"

git -C "$CLONE" symbolic-ref -q HEAD >/dev/null 2>&1 \
  && fail "clone is ON A BRANCH ($(git -C "$CLONE" symbolic-ref --short HEAD)) - not a pin" \
  || ok "clone detached"

# --- property 1b: the pinned TREE, not just the pinned HEAD ------------
git -C "$CLONE" diff --quiet "$PIN" -- 2>/dev/null \
  && ok "tracked tree identical to pin" || fail "tracked files differ from $PIN"

[ -z "$(git -C "$CLONE" status --porcelain 2>/dev/null)" ] \
  && ok "no untracked/modified files" || fail "untracked or modified files present"

# ignored content is invisible to --porcelain but is LIVE for a python verifier
STOWAWAY=$(git -C "$CLONE" status --porcelain --ignored -- skills/ 2>/dev/null)
[ -z "$STOWAWAY" ] && ok "skill tree free of ignored content" \
  || fail "ignored content inside skills/: $(printf '%s' "$STOWAWAY" | tr '\n' ' ')"

# --- property 1c: link topology ----------------------------------------
check_link() {
  lbl=$1; l=$2
  [ -L "$l" ] && ok "$lbl is a symlink" || { fail "$lbl missing or is a copy"; return; }
  [ "$(readlink -f "$l")" = "$CLONE/skills/verification-ladder" ] \
    && ok "$lbl resolves into the pinned clone" \
    || fail "$lbl resolves elsewhere ($(readlink -f "$l")) - DIVERGENT COPIES"
}
check_link "claude" "$LINK"
[ $CHECK_CODEX -eq 1 ] && check_link "codex" "$HOME/.codex/skills/verification-ladder"

# --- property 1d: invariant present AND complete ------------------------
C=$HOME/.claude/CLAUDE.md
for pat in "For every task that modifies repository state" \
           "BLOCKED, not verified" \
           "without evidence"; do
  grep -qF "$pat" "$C" 2>/dev/null && ok "invariant: \"$pat\"" || fail "invariant missing: \"$pat\""
done

python3 "$LINK/bin/verify.py" --help >/dev/null 2>&1 \
  && ok "verify.py executes via link" || fail "verify.py not runnable via link"

echo; [ $rc -eq 0 ] && echo "INSTALL CONFORMS" || echo "INSTALL DOES NOT CONFORM"
exit $rc
