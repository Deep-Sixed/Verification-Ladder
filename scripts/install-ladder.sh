#!/bin/sh
# Verification Ladder installer - steps 1-5 of the Thanatos sequence.
# Idempotent. Never overwrites existing global instructions.
# Usage: install-ladder.sh [--codex] [--pin SHA] [--dry-run] [--force-link]
set -u

PIN=760977f69d6b74b9888be4c9cbb404f7726bea73
REPO=https://github.com/Deep-Sixed/Verification-Ladder
SLUG=deep-sixed/verification-ladder
# sha256 of the invariant block as it stands at $PIN. Changing the pin without
# revalidating this makes the installer fail closed rather than paste unknown text.
INV_SHA=282f21173aad9815a22a6ece35ad0fdd42653dd7a001dc6d85e6d51189aa5699
CLONE=$HOME/src/verification-ladder
DO_CODEX=0; DRY=0; FORCE=0

while [ $# -gt 0 ]; do
  case $1 in
    --codex) DO_CODEX=1 ;;
    --pin) PIN=$2; shift ;;
    --dry-run) DRY=1 ;;
    --force-link) FORCE=1 ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
  shift
done

say()  { echo "  $*"; }
step() { echo; echo "== $*"; }
die()  { echo "ABORT: $*" >&2; exit 1; }
run()  { if [ $DRY -eq 1 ]; then echo "  [dry-run] $*"; else eval "$@"; fi; }

# ---- step 1: inspect before touching anything -------------------------
step "1. Inspecting current state"
for p in "$CLONE" "$HOME/.claude/skills/verification-ladder" "$HOME/.claude/CLAUDE.md" \
         "$HOME/.codex/skills/verification-ladder"; do
  if   [ -L "$p" ]; then say "symlink  $p -> $(readlink "$p")"
  elif [ -d "$p" ]; then say "dir      $p"
  elif [ -e "$p" ]; then say "file     $p ($(wc -l <"$p") lines)"
  else                   say "absent   $p"
  fi
done

# ---- step 2-3: clone / update, then detach at the pin -----------------
step "2-3. Canonical checkout at $PIN"
if [ -d "$CLONE/.git" ]; then
  o=$(git -C "$CLONE" config --get remote.origin.url 2>/dev/null)
  n=$(printf '%s' "$o" | tr 'A-Z' 'a-z' | sed -e 's/\.git$//' -e 's#.*[/:]\([^/]*/[^/]*\)$#\1#')
  [ "$n" = "$SLUG" ] || die "$CLONE origin is '${o:-<none>}', not $SLUG - refusing to touch it"
  [ -n "$(git -C "$CLONE" status --porcelain)" ] \
    && die "$CLONE has local modifications; resolve them first (not overwriting your work)"
  run "git -C '$CLONE' fetch --quiet origin"
else
  run "mkdir -p '$(dirname "$CLONE")'"
  run "git clone --quiet '$REPO' '$CLONE'"
fi
if [ $DRY -eq 0 ]; then
  git -C "$CLONE" cat-file -e "$PIN^{commit}" 2>/dev/null || die "commit $PIN not found in $CLONE"
  git -C "$CLONE" checkout --quiet --detach "$PIN" || die "could not detach at $PIN"
  say "detached at $(git -C "$CLONE" rev-parse HEAD)"
fi

# ---- step 4: symlink, never a copy ------------------------------------
link_skill() {
  root=$1; link=$root/verification-ladder; target=$CLONE/skills/verification-ladder
  if [ -L "$link" ]; then
    [ "$(readlink -f "$link")" = "$target" ] && { say "already linked correctly: $link"; return; }
    say "relinking $link (was -> $(readlink "$link"))"; run "rm '$link'"
  elif [ -e "$link" ]; then
    [ $FORCE -eq 0 ] && die "$link exists and is NOT a symlink (a copy). Re-run with --force-link to back it up and replace."
    say "backing up copy -> $link.bak.$$"; run "mv '$link' '$link.bak.$$'"
  fi
  run "mkdir -p '$root'"
  run "ln -s '$target' '$link'"
  say "linked $link"
}
step "4. Skill symlinks"
link_skill "$HOME/.claude/skills"
[ $DO_CODEX -eq 1 ] && link_skill "$HOME/.codex/skills"

# ---- step 5: append invariant, sourced from the pin, never overwrite --
install_invariant() {
  c=$1
  inv=$(awk '/^## The invariant/{f=1} f&&/^```markdown$/{g=1;next} g&&/^```$/{exit} g{print}' \
          "$CLONE/INSTALL.md")
  got=$(printf '%s\n' "$inv" | sha256sum | cut -d' ' -f1)
  if [ "$got" != "$INV_SHA" ]; then
    echo "  REFUSING: invariant block at this pin does not match INV_SHA." >&2
    echo "    expected $INV_SHA" >&2
    echo "    got      $got" >&2
    echo "    Read INSTALL.md at this revision, confirm the block, update INV_SHA." >&2
    return 1
  fi
  say "invariant extracted and checksum-verified ($INV_SHA)" 
  first=$(echo "$inv" | head -1)
  last="you hold for the current state."
  if [ -e "$c" ]; then
    if grep -qF "$first" "$c" && grep -qF "$last" "$c"; then
      say "invariant already complete in $c - nothing to do"; return
    fi
    if grep -qF "$first" "$c" || grep -qF "$last" "$c"; then
      echo "  PARTIAL invariant already in $c."
      echo "  Refusing to append - that would duplicate or contradict it."
      echo "  Merge by hand, then re-run. (This is a judgment call, not automatable.)"
      return 1
    fi
    say "backing up -> $c.bak.$$"; run "cp '$c' '$c.bak.$$'"
    say "appending invariant to existing $c"
  else
    run "mkdir -p '$(dirname "$c")'"; say "creating $c"
  fi
  if [ $DRY -eq 1 ]; then echo "  [dry-run] append 16-line invariant to $c"; return; fi
  { [ -s "$c" ] && printf '\n'; printf '# Verification\n\n%s\n' "$inv"; } >> "$c"
}
step "5. Global invariant (appended, never overwritten)"
INV_RC=0
install_invariant "$HOME/.claude/CLAUDE.md" || INV_RC=1
[ $DO_CODEX -eq 1 ] && { say "Codex: add the same invariant to your Codex global instructions by hand"; }

echo
if [ $INV_RC -ne 0 ]; then
  echo "INCOMPLETE: skill is linked, but the global invariant was NOT installed."
  echo "Without it the agent has no instruction to invoke the Ladder."
  echo "Resolve the reason printed above, then re-run."
  exit 1
fi
echo "Steps 1-5 done. Next: check-install.sh, then the discovery test,"
echo "then the agent-level policy-less BLOCKED test (steps 6-8)."
