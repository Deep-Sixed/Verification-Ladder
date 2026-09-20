"""Bind verification gate results to the exact repository state that produced them.

The Verification Ladder (`../SKILL.md`) tells an agent how to verify. This records
what was actually verified, and composes records from the authorities that can
establish anything - this checkout, CI, and the agent's own judgment - into one
completion predicate over a single repository state.

Nothing here knows any particular project. What a repository must satisfy, and the
commands that satisfy it, come from that repository's own committed policy.

    python scripts/verify.py run [--gate NAME=COMMAND]... [--output PATH]
    python scripts/verify.py attest --rung NAME --note TEXT [--output PATH]
    python scripts/verify.py import-ci --run RUN.json --job JOB.json [--output PATH]
    python scripts/verify.py compose PATH... [--output PATH]
    python scripts/verify.py check PATH
    python scripts/verify.py compare PATH          # diagnostic; decides nothing

A gate result is evidence for one repository state and nothing else, so every
record carries a state id taken over HEAD plus the working tree. `check` refuses
a record whose state has moved, and `compose` refuses to combine records that do
not agree on the state they describe.

Exit codes: 0 PASS or COMPLETE, 1 FAIL or INCOMPLETE, 2 BLOCKED or STALE. Records
are operational evidence, not repository content; write them under
`.verification/` and do not commit them.
"""
import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "verification.ladder.evidence/2"
COMPOSITE = "verification.ladder.composite/2"
# The schema the model below describes. Nothing emits it yet: this is the data
# model, its parsing and its validation, landed ahead of the rules that will use
# it so the enforcement work is written against a settled shape. Records on disk
# are still evidence/2 until those rules land, because a record stamped v3 while
# the composer applies v2 semantics would be worse than either.
SCHEMA_V3 = "verification.ladder.evidence/3"
BASELINE_SCHEMA = "verification.ladder.baseline/1"
# Provenance says which build produced a record; the contract says which records
# may compose. They are separate: a verifier can keep the v3 record shape and
# change what admissible means, and that must invalidate earlier evidence.
COMPATIBILITY = "evidence-v3.1"
VERSION = "0.1.0"  # tracks pyproject's version; tests/test_schema_v3.py holds them equal
# A gate declares the target dimensions it depends on. Evidence must carry every
# dimension its gate declares, and dimensions it does not declare are never
# compared - so a moved runtime expires a behavioural gate while a lint gate,
# which cannot depend on one, correctly survives.
TARGET_DIMENSIONS = ("repository", "runtime")
# Which authority a declaration block CONTRIBUTES - never a ceiling on what
# authorities a gate may have. A behavioural gate declared in [gates.behavioral]
# contributes `local`, and gains `ci` as well when the policy also declares
# [gates.ci.<name>] for it. CI permission is declared or the gate does not have
# it; it is never inferred from the kind of gate. Last entry is the legacy v2
# [ci_steps] table, which normalizes to the same thing.
GATE_BLOCKS = ((("gates", "local"), "local", "command"),
               (("gates", "ci"), "ci", "step"),
               (("gates", "behavioral"), "local", "driver"),
               (("ci_steps",), "ci", "step"))
EVIDENCE_MODES = ("execution", "execution+attestation")
# How a record's target projection stands against the target in front of us.
# INADMISSIBLE is not a failure of the change: it says the evidence cannot be
# read at all, because it does not carry a dimension its own gate declares.
ADMISSIBLE, INADMISSIBLE = "ADMISSIBLE", "INADMISSIBLE"
# A task that changes a verification definition is its own class, with its own
# terminal state. A definition never governs its own introduction.
DEFINITION_UNCHANGED, DEFINITION_PENDING = "NONE", "PENDING"
# §M2 comparison outcomes. Four, not two: whether the evidence/3 reading agrees
# with the authoritative execution is a different question from whether the two
# could be compared at all, and collapsing them would hide the cases that matter
# most - a predicate nobody could evaluate reads exactly like one that passed.
AGREE, DISAGREE, NOT_COMPARABLE, NOT_APPLICABLE = "AGREE", "DISAGREE", "NOT COMPARABLE", "N/A"
# An index holding exactly what HEAD holds. Named, not digested: see `index_state`.
CLEAN_INDEX = "index:clean"
# Keys that only an evidence/3 per-gate declaration carries. Their presence is
# what separates "this policy is written in the v3 form" from "this gate name
# has a dot in it", which read identically once TOML has parsed them.
V3_DECLARATION_KEYS = frozenset({"command", "step", "driver", "target", "evidence_mode",
                                 "spec_root", "spec_files", "artifacts", "authorities"})
# The shadow-only evidence/3 policy layer (§M2a). Its own subtree, read by the
# shadow path and by nothing else: the authoritative parser never looks here, and
# a declaration here executes nothing, satisfies nothing and blocks nothing.
SHADOW_POLICY = "shadow"
SHADOW_BLOCKS = ("local", "ci", "behavioral")
# Where a repository declares what "verified" means for it. The first file that
# carries a policy wins; pyproject.toml lets a Python project keep one config file.
POLICY_FILES = (("verification.toml", ()), ("pyproject.toml", ("tool", "verification")))
ONBOARDING = (
    "no verification policy found (verification.toml, or [tool.verification] in "
    "pyproject.toml). An unconfigured repository is BLOCKED, never silently verified: "
    "copy templates/verification.toml from the Verification Ladder and declare what "
    "this project requires."
)
OUTPUT_TAIL_BYTES = 4000

PASS, FAIL, BLOCKED, STALE = "PASS", "FAIL", "BLOCKED", "STALE"
COMPLETE, INCOMPLETE = "COMPLETE", "INCOMPLETE"
EXECUTION, ATTESTATION = "execution", "attestation"
EXIT = {PASS: 0, COMPLETE: 0, FAIL: 1, INCOMPLETE: 1, BLOCKED: 2, STALE: 2}
# How an Actions step conclusion reads as a gate result. Anything else - skipped,
# cancelled, still running, absent - establishes nothing.
CI_CONCLUSIONS = {"success": PASS, "failure": FAIL, "timed_out": FAIL}


def git(repo: Path, *args: str) -> bytes:
    """Run a read-only git command in `repo`, returning raw stdout."""
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)
    return result.stdout


def _status_entries(raw: bytes):
    """Yield (status, path) from `git status --porcelain=v1 -z` output.

    Rename and copy entries carry a second NUL-terminated field holding the
    original path; both sides are reported so a rename changes the state id.
    """
    fields = raw.split(b"\0")
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        status, _, path = field[:2], field[2:3], field[3:]
        yield status.decode(), path.decode()
        if (status[:1] in (b"R", b"C") or status[1:2] in (b"R", b"C")) and index < len(fields):
            origin = fields[index]
            index += 1
            if origin:
                yield status.decode() + "<-", origin.decode()


def _hash_path(digest: "hashlib._Hash", repo: Path, relative: str) -> None:
    """Fold one worktree path's content into `digest`, directories included."""
    target = repo / relative
    if target.is_symlink():
        # A symlink's identity is where it points; following it would fold in
        # content that lives outside the state being identified.
        digest.update(b"symlink:" + os.readlink(target).encode())
    elif target.is_dir():
        for child in sorted(p for p in target.rglob("*") if p.is_file()):
            digest.update(child.relative_to(repo).as_posix().encode())
            digest.update(hashlib.sha256(child.read_bytes()).digest())
    elif target.is_file():
        digest.update(hashlib.sha256(target.read_bytes()).digest())
    else:
        digest.update(b"absent")


def state_id(repo: Path, exclude: frozenset[str] = frozenset()) -> str:
    """Identify the exact state under verification: HEAD plus every deviation from it.

    This identifies the git-visible worktree state covered by the verifier, and
    that is narrower than "two checkouts that share it would verify identically".
    It does not cover the index - two trees whose `git diff --cached` differs can
    share one - nor ignored files, environment, toolchain, or any running system a
    gate talks to. `repository_identity` adds the index; a runtime dimension
    covers the rest, for gates that declare one. Ignored
    paths are invisible to `git status` and so do not perturb it; `exclude` drops
    the evidence files themselves, which are written after the state is captured.
    """
    digest = hashlib.sha256()
    digest.update(head_sha(repo).encode())
    for status, path in _status_entries(git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")):
        if path in exclude:
            continue
        digest.update(status.encode())
        digest.update(path.encode())
        _hash_path(digest, repo, path)
    return "sha256:" + digest.hexdigest()


def clean_state_id(sha: str) -> str:
    """The state id of a clean checkout of `sha`, computed without one.

    This is what lets CI evidence compose with local evidence: CI verifies a
    pristine checkout, so its state id is a function of the commit alone, and it
    matches a local record only when the local tree is clean at that same commit.
    """
    return "sha256:" + hashlib.sha256(sha.encode()).hexdigest()


def head_sha(repo: Path) -> str:
    try:
        return git(repo, "rev-parse", "HEAD").decode().strip()
    except subprocess.CalledProcessError:
        return "unborn"


def branch(repo: Path) -> str:
    try:
        return git(repo, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()
    except subprocess.CalledProcessError:
        return "unborn"


def require_repository(repo: Path) -> Path:
    """Fail clearly rather than with a git traceback when pointed at a non-repository."""
    try:
        git(repo, "rev-parse", "--git-dir")
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
        print(f"{repo}: not a git repository; verification needs a state to bind to", file=sys.stderr)
        raise SystemExit(EXIT[BLOCKED]) from None
    return repo


def state_paths(repo: Path, exclude: frozenset[str] = frozenset()) -> set[tuple[str, str]]:
    """The (status, path) pairs `state_id` folds in - for diagnostics only.

    A state id is a digest and so cannot say what moved. Comparing these across a
    run names the paths that appeared, vanished or changed status, which is nearly
    always a gate writing its own cache into the tree it is being measured against.
    Content that changes without changing status is invisible here; drift is still
    reported, just without the path list.
    """
    entries = _status_entries(git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"))
    return {(status, path) for status, path in entries if path not in exclude}


def is_dirty(repo: Path, exclude: frozenset[str] = frozenset()) -> bool:
    entries = _status_entries(git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"))
    return any(path not in exclude for _, path in entries)


def blocked(message: str) -> "SystemExit":
    """Build the exit for evidence that could not be established - never a failure of the code."""
    print(message, file=sys.stderr)
    return SystemExit(EXIT[BLOCKED])


def policy(repo: Path) -> dict:
    """Read the repository's committed completion policy.

    What "complete" means lives in the repository under verification, not in this
    script and not in the agent, so that changing it is a reviewed change to that
    project. A repository that has declared nothing is BLOCKED: silence is not
    consent, and an unconfigured project must not read as a verified one.
    """
    for name, path in POLICY_FILES:
        config = repo / name
        try:
            text = config.read_text()
        except OSError:
            continue
        try:
            declared = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            # A policy that exists but does not parse is broken, not absent. Saying
            # "no policy found" would send someone to onboard an onboarded project.
            raise blocked(f"{config}: policy does not parse ({error})") from None
        for key in path:
            declared = declared.get(key) if isinstance(declared, dict) else None
        if isinstance(declared, dict) and declared:
            refuse_inactive_v3(declared, config)
            return declared
    raise blocked(f"{repo}: {ONBOARDING}")


def refuse_inactive_v3(declared: dict, config: Path) -> None:
    """Name evidence/3 policy syntax for what it is, rather than misreading it.

    `docs/design/evidence-v3.md` documents per-gate tables and this release acts
    on the v2 form only, so read as v2 the documented syntax produces two wrong
    answers. `[gates.local.lint]` is a table where a command string belongs, and
    the reader blames a dotted gate name - for a gate with no dot in it.
    `[gates.ci.x]` and `[gates.behavioral.x]` are not read at all, so a required
    gate declared there silently reads MISSING. Both fail closed, and both
    misdiagnose a policy that said clearly what it meant.

    This is diagnosis only. It does not act on the declaration.
    """
    gates = declared.get("gates")
    if not isinstance(gates, dict):
        return
    found = set()
    for block in ("ci", "behavioral"):
        if isinstance(gates.get(block), dict) and gates[block]:
            found.add(f"[gates.{block}.*]")
    local = gates.get("local")
    if isinstance(local, dict):
        for name, body in local.items():
            if isinstance(body, dict) and V3_DECLARATION_KEYS & set(body):
                found.add(f"[gates.local.{name}]")
    if found:
        raise blocked(
            f"{config}: {', '.join(sorted(found))} is {SCHEMA_V3} policy syntax, which this release "
            f"parses but does not act on - evidence/3 is implemented and not yet authoritative (see "
            f"docs/design/evidence-v3.md). Declare gates in the form this release reads: "
            f'[gates.local] name = "command", and [ci_steps] name = "step" for gates only CI can run.'
        )


def gate_map(declared: dict, *path: str) -> dict[str, str]:
    """Read one gate-name -> string table from the policy, rejecting dotted names.

    A bare TOML key carrying a dot is a table path, not a name: `tests-3.11 = "x"`
    parses as table `tests-3` holding `11`, and the gate would quietly read
    MISSING. Say so instead.
    """
    table = declared
    for key in path:
        table = table.get(key, {}) if isinstance(table, dict) else {}
    for name, value in table.items():
        if not isinstance(value, str):
            raise blocked(
                f"policy [{'.'.join(path)}] entry {name!r} is a table, not a command. A gate name "
                f"containing '.' splits into TOML tables - quote the key or rename the gate."
            )
    return dict(table)


def local_gates(declared: dict) -> list[tuple[str, str]]:
    """The gates this checkout can execute, named by the repository's policy.

    Read from the authoritative policy alone. `[shadow]` is not consulted here
    and must never be: a shadow declaration that could put a command in this
    list would be creating execution, which is the one thing §M2a forbids it.
    """
    gates = gate_map(declared, "gates", "local")
    if not gates:
        raise blocked("policy declares no [gates.local]; nothing can be executed here")
    return list(gates.items())


def shadow_declaration(declared: dict) -> dict:
    """The evidence/3 view of this policy: the v2 gates, overlaid by `[shadow.gates]`.

    §M2a. The overlay **replaces** a gate's authoritative declarations rather
    than merging into them. Merging would give a behavioural gate two `local`
    declarations - the command v2 runs and the driver v3 describes - and the two
    meanings would have to be reconciled by inference. Replacing keeps one answer
    to what a gate means under evidence/3, and keeps authority declared: a shadow
    gate has `ci` authority only where `[shadow.gates.ci.<name>]` says so.

    Nothing here is authoritative. This view never reaches `local_gates`,
    `gate_map`, `compose` or any verdict; it decides only how an execution that
    already happened is read under evidence/3.
    """
    shadow = declared.get(SHADOW_POLICY)
    overlay = (shadow or {}).get("gates") if isinstance(shadow, dict) else None
    overlay = overlay if isinstance(overlay, dict) else {}
    spoken_for = {name for block in SHADOW_BLOCKS if isinstance(overlay.get(block), dict)
                  for name in overlay[block]}

    view: dict = {"gates": {}}
    base = declared.get("gates")
    authoritative = ((base.get("local") if isinstance(base, dict) else None) or {},
                     declared.get("ci_steps") or {})
    for table, into in zip(authoritative, ("local", "ci_steps"), strict=True):
        for name, body in (table.items() if isinstance(table, dict) else ()):
            if name in spoken_for:
                continue
            if into == "local":
                view["gates"].setdefault("local", {})[name] = body
            else:
                view.setdefault("ci_steps", {})[name] = body
    for block in SHADOW_BLOCKS:
        table = overlay.get(block)
        if isinstance(table, dict) and table:
            view["gates"][block] = dict(table)
    return view


def shadow_runtime(repo: Path, declared: dict) -> dict | None:
    """The runtime dimension, read from a declared manifest rather than probed.

    A shadow probe that ran a command to collect runtime facts would be a shadow
    declaration causing execution, which §M2a forbids. So the manifest is read:
    `[shadow.runtime] facts_file` names a JSON object, and its absence is not an
    error here. It means the runtime cannot be interrogated, which `projection_status`
    reads as BLOCKED at comparison time and as a missing dimension at record time -
    two different diagnoses that must not collapse into one.
    """
    shadow = declared.get(SHADOW_POLICY)
    source = (shadow or {}).get("runtime") if isinstance(shadow, dict) else None
    name = source.get("facts_file") if isinstance(source, dict) else None
    if not isinstance(name, str) or not name:
        return None
    try:
        facts = json.loads((repo / name).read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(facts, dict) or not facts:
        return None
    return runtime_identity(facts)


def shadow_target(repo: Path, declared: dict, repository: dict) -> dict:
    """The target a shadow record binds to: always repository, runtime when readable."""
    runtime = shadow_runtime(repo, declared)
    return {"repository": repository, **({"runtime": runtime} if runtime else {})}


# --- verification.ladder.evidence/3 model -------------------------------------
#
# Parsing, validation and identity for the v3 schema. Nothing here changes what
# run/attest/import-ci/compose emit or enforce; those follow in the rules series.


def canonical(value) -> bytes:
    """The one byte string a digest may be taken over: UTF-8, sorted keys, no slack.

    Two readers must agree on a definition digest or gate identity means nothing,
    so the encoding is pinned rather than left to json.dumps' defaults.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest_of(value) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def verifier_identity() -> dict:
    """Which build produced a record, and which records it may compose with.

    `commit` and `implementation_sha256` are provenance and never decide
    composability; `compatibility` is the contract that does.
    """
    source = Path(__file__).resolve()
    identity = {
        "schema": SCHEMA_V3,
        "version": VERSION,
        "compatibility": COMPATIBILITY,
        "implementation_sha256": "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    try:
        identity["commit"] = git(source.parent, "rev-parse", "HEAD").decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
        # An installation copied rather than cloned has no commit to report. Say
        # nothing rather than guessing; the implementation digest still pins it.
        identity["commit"] = None
    return identity


def _declaration_error(name: str, detail: str) -> "SystemExit":
    return blocked(f"policy gate {name!r}: {detail}")


def _normalize_one(name: str, authority: str, declared, key: str) -> dict:
    """One gate declaration, in either the v3 table form or the legacy string form.

    The legacy form carries no target and no mode, so it normalizes to the only
    thing a v2 gate could ever have meant: an execution over the repository. The
    mapping is fixed rather than inferred, so two readers of one legacy file agree.
    """
    if isinstance(declared, str):
        if not declared:
            raise _declaration_error(name, f"empty {key} for authority {authority!r}")
        return {"target": ["repository"], "evidence_mode": "execution", key: declared}
    if not isinstance(declared, dict):
        raise _declaration_error(name, f"declaration must be a table or a string, got {type(declared).__name__}")
    if key not in declared:
        if declared and all(isinstance(v, dict) for v in declared.values()):
            raise _declaration_error(
                name,
                f"no {key!r} and every entry is a table - a gate name containing '.' splits into "
                f"TOML tables. Quote the key or rename the gate.",
            )
        raise _declaration_error(name, f"declaration for authority {authority!r} has no {key!r}")
    entry = {key: declared[key]}
    if not isinstance(entry[key], str) or not entry[key]:
        raise _declaration_error(name, f"{key!r} must be a non-empty string")
    target = declared.get("target", ["repository"])
    if not isinstance(target, list) or not target or any(d not in TARGET_DIMENSIONS for d in target):
        raise _declaration_error(name, f"target must be a non-empty list drawn from {list(TARGET_DIMENSIONS)}")
    entry["target"] = sorted(set(target))
    mode = declared.get("evidence_mode", "execution")
    if mode not in EVIDENCE_MODES:
        raise _declaration_error(name, f"evidence_mode must be one of {list(EVIDENCE_MODES)}, got {mode!r}")
    entry["evidence_mode"] = mode
    for optional in ("spec_root", "spec_files", "artifacts"):
        if optional in declared:
            entry[optional] = declared[optional]
    return entry


def normalize_policy(declared: dict) -> dict[str, dict]:
    """Every gate the policy declares, in one shape, whatever form it was written in.

    A gate may be establishable by more than one authority - this repository's own
    `lint` is both a local command and a CI step - so authorities are collected
    under the gate rather than splitting it into two gates. Target and evidence
    mode belong to the gate itself and must agree across its authorities.
    """
    gates: dict[str, dict] = {}
    for path, authority, key in GATE_BLOCKS:
        table = declared
        for step in path:
            table = (table or {}).get(step) or {}
        if not isinstance(table, dict):
            raise blocked(f"policy: gate table for authority {authority!r} is not a table")
        for name, body in table.items():
            entry = _normalize_one(name, authority, body, key)
            gate = gates.setdefault(name, {"target": entry["target"],
                                           "evidence_mode": entry["evidence_mode"],
                                           "authorities": {}})
            for field in ("target", "evidence_mode"):
                if gate[field] != entry[field]:
                    raise _declaration_error(
                        name, f"{field} differs between its authorities ({gate[field]!r} and {entry[field]!r}); "
                              f"it belongs to the gate, not to one way of establishing it")
            for optional in ("spec_root", "spec_files", "artifacts"):
                if optional in entry:
                    gate[optional] = entry[optional]
            if authority in gate["authorities"]:
                raise _declaration_error(name, f"declared twice for authority {authority!r}")
            gate["authorities"][authority] = {k: v for k, v in entry.items()
                                              if k not in ("target", "evidence_mode")}
    return gates


def spec_sources(repo: Path, name: str, gate: dict) -> list[dict]:
    """The declared specification files that give a behavioural gate its meaning.

    Declared, never observed: an agent reading Markdown cannot be reliably watched,
    so what a driver happened to open is not part of identity. Every file must
    resolve beneath the declared root, or a gate could reach outside the
    specification it claims to be bound by.
    """
    files = gate.get("spec_files") or []
    if not files:
        return []
    root_name = gate.get("spec_root")
    if not isinstance(root_name, str) or not root_name:
        raise _declaration_error(name, "declares spec_files without a spec_root to bound them")
    root = (repo / root_name).resolve()
    sources = []
    for entry in sorted(files):
        if not isinstance(entry, str):
            raise _declaration_error(name, "every spec_files entry must be a path string")
        resolved = (repo / entry).resolve()
        if not resolved.is_relative_to(root):
            raise _declaration_error(name, f"specification {entry!r} resolves outside spec_root {root_name!r}")
        try:
            body = resolved.read_bytes()
        except OSError:
            raise _declaration_error(name, f"declared specification {entry!r} is unreadable") from None
        sources.append({"path": entry, "sha256": "sha256:" + hashlib.sha256(body).hexdigest()})
    return sources


def gate_definition(repo: Path, name: str, gate: dict) -> dict:
    """A gate's identity: its own declaration and its declared specification bytes.

    Taken over the gate's subtree rather than the whole policy file. A shared
    verification.toml contributes to every gate if hashed wholesale, and editing
    one gate's command would then invalidate all of them - which would defeat the
    rule that an unrelated gate survives a definition change elsewhere.
    """
    sources = spec_sources(repo, name, gate)
    declaration = {k: v for k, v in gate.items() if k != "spec_files"}
    return {"gate": name,
            "target": gate["target"],
            "evidence_mode": gate["evidence_mode"],
            "permitted_authorities": sorted(gate["authorities"]),
            # Declared artifact paths, carried so a record can digest what this
            # gate said it would produce. Paths are part of `declaration` and so
            # already fold into the digest below; repeating them here is a
            # convenience for the recorder, not a second definition.
            "artifacts": list(gate.get("artifacts") or []),
            "sources": sources,
            "definition_sha256": digest_of({"declaration": declaration, "sources": sources})}


def gate_artifacts(repo: Path, definition: dict) -> list[dict]:
    """Digest the artifacts a behavioural gate declared, from disk, after it ran.

    An artifact that is not there is recorded as unreadable rather than raising.
    The chain predicate is the thing that decides what a missing artifact means,
    and losing the whole record to an exception would report that as "no shadow
    was constructed" - absence of a comparison where there should be a failing one.
    """
    entries = []
    for relative in sorted(definition.get("artifacts") or []):
        try:
            body = (repo / relative).read_bytes()
        except OSError:
            entries.append({"path": relative, "sha256": None})
        else:
            entries.append({"path": relative, "sha256": "sha256:" + hashlib.sha256(body).hexdigest()})
    return entries


def gate_definitions(repo: Path, declared: dict) -> dict[str, dict]:
    gates = normalize_policy(declared)
    return {name: gate_definition(repo, name, gate) for name, gate in gates.items()}


def baseline_record(repo: Path, declared: dict, gates: list[dict], *, origin: str = "captured",
                    exclude: frozenset[str] = frozenset()) -> dict:
    """The pre-mutation state, and the definitions that governed it.

    Baseline binds to the state before the change, so it can never match the state
    under review and is not an ordinary composable row. `origin` stays in the
    record: a baseline reconstructed from an immutable commit during migration must
    never read as one that was captured.
    """
    if origin not in ("captured", "reconstructed"):
        raise blocked(f"baseline origin must be 'captured' or 'reconstructed', got {origin!r}")
    return {
        "schema": BASELINE_SCHEMA,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "origin": origin,
        "verifier": verifier_identity(),
        "target_state": {"repository": repository_identity(repo, exclude)},
        "governing": {"policy_sha256": digest_of(declared),
                      "gates": gate_definitions(repo, declared)},
        "gates": gates,
    }


def index_state(repo: Path) -> str:
    """Identify the staged tree, which the worktree digest cannot see.

    Two checkouts can share HEAD, share every working-tree byte and report the
    same `git status` codes while holding different content in the index - the
    worktree digest collides and `git diff --cached` differs. The ladder asks an
    agent to read the diff, so the index is part of the state being verified.
    `ls-files --stage` names mode, blob and path for every entry, which is that
    content by identity rather than by re-reading it.

    An index that matches HEAD is named rather than digested. Its content is a
    function of the commit, so every clean checkout of one commit holds the same
    index - but the digest is taken over local object paths and only a checkout
    can produce it, so a CI record, which describes a pristine tree it does not
    hand us, could never carry a matching one. Two records of one clean commit
    would then describe two targets. `diff-index --cached` answers the question
    exactly, and a staged difference of any kind still takes the digest.
    """
    try:
        git(repo, "diff-index", "--cached", "--quiet", "HEAD")
    except subprocess.CalledProcessError:
        return "sha256:" + hashlib.sha256(git(repo, "ls-files", "--stage", "-z")).hexdigest()
    return CLEAN_INDEX


def repository_identity(repo: Path, exclude: frozenset[str] = frozenset()) -> dict:
    """The repository dimension of a target: HEAD, the index, and the worktree."""
    return {"head": head_sha(repo),
            "index_state": index_state(repo),
            "worktree_state": state_id(repo, exclude)}


def clean_repository_identity(sha: str) -> dict:
    """The repository dimension CI can establish: a pristine checkout of one commit.

    Stated in the same vocabulary a local clean checkout produces, so the two
    compare. CI never has a dirty tree or a staged change, by construction.
    """
    return {"head": sha, "index_state": CLEAN_INDEX, "worktree_state": clean_state_id(sha)}


def runtime_identity(facts: dict) -> dict:
    """The runtime dimension: the facts that decide a behavioural result, by digest.

    For a single application these are build, instance and flag state; for a
    distributed target the same shape carries service identity resolved to image
    digests. The verifier does not interpret them - it binds to them.
    """
    if not isinstance(facts, dict) or not facts:
        raise blocked("runtime identity needs a non-empty table of facts to bind to")
    return {"manifest_sha256": digest_of(facts), "facts": facts}


def target_projection(target_state: dict, dimensions) -> dict:
    """A target narrowed to the dimensions one gate declares.

    Comparing whole target states would expire a lint gate because a runtime it
    cannot depend on moved. Comparing nothing would let a behavioural gate survive
    a runtime change. Each gate is compared on exactly what it declared.
    """
    return {dimension: target_state[dimension] for dimension in dimensions if dimension in target_state}


def projection_status(record_target: dict, dimensions, current_target: dict) -> tuple[str, str | None]:
    """Whether one record's target still describes the target in front of us.

    Three outcomes, and they are not the same news. A dimension the gate declares
    but the record omits is INADMISSIBLE - the evidence cannot be read, and must
    never pass by defaulting to "matched". A dimension the record carries that
    cannot be interrogated now is BLOCKED: no target, not a different one. A
    dimension that differs is STALE: the evidence is about another target.
    """
    for dimension in dimensions:
        if dimension not in record_target:
            return INADMISSIBLE, (f"evidence omits the {dimension!r} dimension its gate declares; "
                                  f"a missing dimension is never a matching one")
        if dimension not in current_target:
            return BLOCKED, (f"the {dimension!r} dimension cannot be interrogated here, so there is "
                             f"no target to compare against - this is absence of evidence, not a mismatch")
        if record_target[dimension] != current_target[dimension]:
            return STALE, f"evidence binds to another {dimension!r}; re-verify against this one"
    return ADMISSIBLE, None


def definition_status(claimed: dict, governing: dict) -> tuple[str, str | None]:
    """Whether a record's gate identity is the one currently in force.

    A gate name is not a gate identity. Evidence carries the digest of the
    definition it was produced under; if the governing definition has moved, the
    gate did pass, under a meaning no longer in force. That is INADMISSIBLE
    rather than a failure - nothing is wrong with the code, and nothing about it
    has been established either.
    """
    name = claimed.get("gate")
    if name not in governing:
        return INADMISSIBLE, f"{name!r} is not a gate this policy declares"
    current = governing[name]
    if claimed.get("definition_sha256") != current["definition_sha256"]:
        return INADMISSIBLE, (f"{name!r} was established under definition "
                              f"{str(claimed.get('definition_sha256'))[:19]}…; the definition governing "
                              f"this task is {current['definition_sha256'][:19]}…. Re-verify under the "
                              f"one in force, or this is a definition change (§L2)")
    return ADMISSIBLE, None


def authority_status(name: str, authority: str, governing: dict) -> tuple[str, str | None]:
    """Whether this authority is one the policy lets establish this gate.

    The gate whose only permitted authority is `ci` is the one a local command
    must never satisfy: a container build or a second interpreter that this
    checkout cannot run is exactly what a CI-only gate is for, and a local no-op
    wearing its name establishes nothing.
    """
    if name not in governing:
        return INADMISSIBLE, f"{name!r} is not a gate this policy declares"
    permitted = governing[name]["permitted_authorities"]
    if authority not in permitted:
        return INADMISSIBLE, (f"{name!r} may be established by {', '.join(permitted)}, not by "
                              f"{authority!r}; authority is declared, never assumed")
    return ADMISSIBLE, None


def gate_admissibility(row: dict, governing: dict, current_target: dict) -> tuple[str, str | None]:
    """Everything that must agree before a row is read as evidence about this target.

    Definition, then authority, then projection - in that order, because a row
    whose definition has moved is not evidence whose authority is worth checking.
    Each fails closed with what to do about it.
    """
    for status, reason in (definition_status(row, governing),
                           authority_status(row.get("gate"), row.get("authority"), governing)):
        if status != ADMISSIBLE:
            return status, reason
    return projection_status(row.get("target_projection") or row.get("target_state") or {},
                             governing[row["gate"]]["target"], current_target)


def kind_status(row: dict, governing: dict, judgment_rungs) -> tuple[str, str | None]:
    """What kind of evidence a requirement takes, enforced in both directions.

    v2 enforced one direction only: an attestation could not satisfy a required
    gate. The converse was unguarded, so an execution named `diff` satisfied the
    diff judgment rung - `--gate diff="true"` was a passing judgment. A machine
    cannot hold an opinion about a diff, and an agent cannot run a container
    build. Neither may stand in for the other.
    """
    name, kind = row.get("gate"), row.get("kind")
    if name in judgment_rungs:
        if kind != ATTESTATION:
            return INADMISSIBLE, (f"{name!r} is a judgment rung and takes an attestation; an "
                                  f"{kind} establishes that something ran, not that anyone read it")
        return ADMISSIBLE, None
    if name not in governing:
        return INADMISSIBLE, f"{name!r} is not a gate this policy declares"
    if kind == ATTESTATION and governing[name]["evidence_mode"] == "execution+attestation":
        # The judgment half of a behavioural gate. Admissible as a row, and
        # nothing more: `behavioural_status` is what decides whether it binds to
        # an execution, and the gate is unsatisfied without one either way.
        return ADMISSIBLE, None
    if kind != EXECUTION:
        return INADMISSIBLE, (f"{name!r} must be established by execution; attested, never executed")
    return ADMISSIBLE, None


def behavioural_status(row: dict, governing: dict, rows: list[dict], repo: Path) -> tuple[str, str | None]:
    """An `execution+attestation` gate needs both halves, bound to the same bytes.

    The execution establishes that the real path ran; the attestation establishes
    that someone read what it produced. Neither absorbs the other, so the
    attestation names the execution it judged and the artifacts it judged, and
    every digest is recomputed from the file rather than compared against another
    record's copy of it. Two strings agreeing with each other is not a check.
    """
    name = row["gate"]
    if governing[name]["evidence_mode"] != "execution+attestation":
        return ADMISSIBLE, None
    if row.get("kind") != EXECUTION:
        return ADMISSIBLE, None
    artifacts = row.get("artifacts") or []
    if not artifacts:
        return INADMISSIBLE, f"{name!r} is a behavioural gate and established no artifacts to be judged"
    for artifact in artifacts:
        location = repo / artifact["path"]
        try:
            body = location.read_bytes()
        except OSError:
            return INADMISSIBLE, f"{name!r} references {artifact['path']!r}, which is not on disk"
        if "sha256:" + hashlib.sha256(body).hexdigest() != artifact["sha256"]:
            return INADMISSIBLE, (f"{name!r} references {artifact['path']!r}, whose bytes have changed "
                                  f"since the execution recorded them")
    recomputed = record_id(row)
    if row.get("record_id") != recomputed:
        return INADMISSIBLE, f"{name!r}: stored record_id does not match the record it identifies"
    digests = {artifact["sha256"] for artifact in artifacts}
    for candidate in rows:
        if candidate.get("kind") != ATTESTATION or candidate.get("gate") != name:
            continue
        if candidate.get("execution_ref") != recomputed:
            continue
        if set(candidate.get("artifact_refs") or []) != digests:
            return INADMISSIBLE, (f"{name!r}: its attestation judged a different set of artifacts "
                                  f"than the execution produced")
        if candidate.get("target_projection") != row.get("target_projection"):
            return INADMISSIBLE, f"{name!r}: its attestation was made against a different target"
        return ADMISSIBLE, None
    return INADMISSIBLE, (f"{name!r} ran and produced artifacts, but nothing attests to what they show; "
                          f"an execution establishes that a path ran, not that it was correct")


def record_id(row: dict) -> str:
    """A row's identity, over its canonical form with the stored id left out.

    Stored as a locator and recomputed wherever it is used: an attestation that
    names an execution must name one that exists as recorded, or the reference
    identifies nothing.
    """
    return digest_of({k: v for k, v in row.items() if k != "record_id"})


def ci_provenance_status(run: dict, job: dict, expected: dict) -> tuple[str, str | None]:
    """Prove a CI result belongs to this commit, in this repository, from one run.

    `head_sha` alone establishes that a run NAMED the commit. It does not
    establish that the job whose steps are being read belongs to that run, that
    the run belongs to this repository, or that the workflow and job are the ones
    the gate rests on. A job payload from another run composes with this run's
    metadata unless every link is checked, so the whole chain is:

        repository -> run -> attempt -> workflow -> job -> step -> commit

    Fields the payloads do not carry are not invented: a link that cannot be
    checked is reported as unverifiable rather than passed over.
    """
    sha = run.get("head_sha")
    if not sha:
        return INADMISSIBLE, "run payload carries no head_sha; there is no commit to bind to"
    if sha != expected["head_sha"]:
        return INADMISSIBLE, (f"run is for {sha[:12]}, the commit under verification is "
                              f"{expected['head_sha'][:12]}")
    repository = (run.get("repository") or {}).get("full_name")
    if expected.get("repository"):
        if not repository:
            return INADMISSIBLE, ("run payload names no repository, so it cannot be told from a fork's "
                                  "run on the same commit")
        if repository != expected["repository"]:
            return INADMISSIBLE, (f"run belongs to {repository!r}, the repository under verification is "
                                  f"{expected['repository']!r}")
    if job.get("run_id") is None or run.get("id") is None:
        return INADMISSIBLE, "cannot bind job to run: one of them carries no run id"
    if job["run_id"] != run["id"]:
        return INADMISSIBLE, (f"job belongs to run {job['run_id']}, the metadata is from run "
                              f"{run['id']}; a job from another run establishes nothing here")
    attempts = (run.get("run_attempt"), job.get("run_attempt"))
    if all(a is not None for a in attempts) and attempts[0] != attempts[1]:
        return INADMISSIBLE, (f"job is from attempt {attempts[1]}, the run metadata is attempt "
                              f"{attempts[0]}")
    if job.get("head_sha") and job["head_sha"] != sha:
        return INADMISSIBLE, f"job ran {job['head_sha'][:12]}, the run names {sha[:12]}"
    if expected.get("workflow") and run.get("path") != expected["workflow"]:
        return INADMISSIBLE, (f"run is workflow {run.get('path')!r}, the gate rests on "
                              f"{expected['workflow']!r}")
    if expected.get("job") and job.get("name") != expected["job"]:
        return INADMISSIBLE, f"job is {job.get('name')!r}, the gate rests on {expected['job']!r}"
    accepted = expected.get("events") or ["push"]
    if run.get("event") not in accepted:
        return INADMISSIBLE, (f"a {run.get('event')} run checks out a merge of {sha[:12]} into its "
                              f"base, so its results describe that merge; import the "
                              f"{'/'.join(accepted)} run for this commit")
    return ADMISSIBLE, None


def ci_step_status(job: dict, step_name: str) -> tuple[str, str | None]:
    """One workflow step's conclusion, read as a gate result.

    A step that was skipped, cancelled, is still running or is absent from the
    job establishes nothing - it is BLOCKED rather than a pass or a failure,
    because nothing ran to have an opinion about.
    """
    conclusions = {step.get("name"): step.get("conclusion") for step in job.get("steps") or []}
    if step_name not in conclusions:
        return BLOCKED, f"step {step_name!r} is absent from this job; the gate rests on a step nobody ran"
    verdict = CI_CONCLUSIONS.get(conclusions[step_name])
    if verdict is None:
        return BLOCKED, f"step {step_name!r} is {conclusions[step_name] or 'unfinished'}, which establishes nothing"
    return verdict, None


def verifier_status(records: list[dict]) -> tuple[str, str | None]:
    """Whether these records were produced under one set of admissibility rules.

    Provenance and contract are different questions. `commit` and
    `implementation_sha256` say which build wrote a record and are reported, never
    compared - two builds of the same contract compose. `compatibility` is the
    contract, and records carrying different contracts do not compose, because a
    verifier can keep the v3 record shape while changing what admissible means.
    Composing across that change would let a record established under weaker
    rules stand beside one established under stronger ones.
    """
    contracts = {}
    for record in records:
        verifier = record.get("verifier")
        if not isinstance(verifier, dict) or not verifier.get("compatibility"):
            return INADMISSIBLE, ("a record carries no verifier compatibility contract, so there is no "
                                  "way to tell which rules produced it")
        contracts.setdefault(verifier["compatibility"], []).append(verifier.get("commit"))
    if len(contracts) > 1:
        return INADMISSIBLE, ("records were produced under different verifier contracts (" +
                              ", ".join(sorted(contracts)) + "); re-verify under one")
    contract = next(iter(contracts))
    if contract != COMPATIBILITY:
        return INADMISSIBLE, (f"records were produced under contract {contract!r}, this verifier "
                              f"applies {COMPATIBILITY!r}; re-verify under the rules in force")
    return ADMISSIBLE, None


def governing_definitions(baseline: dict) -> dict:
    """The gate definitions in force for this task: the ones captured at baseline.

    Not the working tree's. A policy read from the tree at composition time is one
    the task could have edited, so a change could weaken the rule that judges it -
    and would not even need committing to do so.
    """
    if baseline.get("schema") != BASELINE_SCHEMA:
        raise blocked(f"governing definitions need a {BASELINE_SCHEMA} record, got {baseline.get('schema')!r}")
    return baseline["governing"]["gates"]


def definition_change(baseline: dict, candidate: dict) -> tuple[str, list[str]]:
    """Which gates mean something different now from what they meant at baseline.

    Gate-scoped on purpose. A shared policy file hashed wholesale would fold into
    every gate's identity, so editing one gate's command would report every other
    gate as changed and the distinction would be useless.
    """
    governing = governing_definitions(baseline)
    changed = [name for name, definition in candidate.items()
               if name in governing and definition["definition_sha256"] != governing[name]["definition_sha256"]]
    changed += [f"{name} (added)" for name in candidate if name not in governing]
    changed += [f"{name} (removed)" for name in governing if name not in candidate]
    return (DEFINITION_PENDING if changed else DEFINITION_UNCHANGED), sorted(changed)


def governance_status(baseline: dict, candidate: dict, rows: list[dict]) -> tuple[str, list[str]]:
    """Whether this task may report a clean climb, or is a governance change.

    A task that changes what a gate means may collect evidence under the new
    definition - that is how a verification change gets reviewed at all - but the
    evidence is about the candidate, and CLEAN CLIMB must not read TRUE on the
    strength of it. The human gate decides whether the candidate becomes
    governing, and only then for subsequent tasks.
    """
    state, changed = definition_change(baseline, candidate)
    if state == DEFINITION_UNCHANGED:
        return DEFINITION_UNCHANGED, []
    governing = governing_definitions(baseline)
    under_candidate = sorted({
        row["gate"] for row in rows
        if row.get("gate") in governing
        and row.get("definition_sha256") != governing[row["gate"]]["definition_sha256"]})
    return DEFINITION_PENDING, [f"{name}: candidate definition only" for name in under_candidate] or changed


def shadow_gate(repo: Path, result: dict, definitions: dict, target_state: dict,
                judgments=()) -> dict:
    """One gate's evidence/3 row, built from the v2 execution that already happened.

    From the result, never from a second run. Re-executing to manufacture the v3
    half would make the two records describe two executions, and shadow mode would
    then be comparing the verifier against itself on different evidence.

    The row carries its own target projection rather than deferring to the
    record's whole target: §I compares each gate on exactly the dimensions it
    declared, and a row that carried the whole target would expire a lint gate
    because a runtime it cannot depend on moved.
    """
    name = result["name"]
    row = {"gate": name, "kind": result["kind"], "status": result["status"]}
    known = definitions.get(name)
    if known:
        row |= {"definition_sha256": known["definition_sha256"],
                "permitted_authorities": known["permitted_authorities"],
                "target": known["target"], "evidence_mode": known["evidence_mode"],
                "target_projection": target_projection(target_state, known["target"])}
        if known["artifacts"] and result["kind"] == EXECUTION:
            row["artifacts"] = gate_artifacts(repo, known)
    elif name in judgments:
        # A judgment rung is not a gate and has no gate definition. Saying
        # "undeclared" here would read as a custom gate sneaking past the policy,
        # which is a different thing entirely.
        row["definition"] = "judgment rung"
    else:
        # A gate the policy does not declare - a custom --gate. It gets no
        # definition identity, because it has none: that is the point of §5.
        row["definition"] = "undeclared"
    for field in ("exit_code", "reason", "step", "note", "at", "execution_ref", "artifact_refs"):
        if field in result:
            row[field] = result[field]
    row["record_id"] = record_id(row)
    return row


def shadow_record(repo: Path, declared: dict, results: list[dict], authority: str,
                  target_state: dict, **extra) -> dict:
    """The evidence/3 view of an execution that has already been recorded as v2.

    §M1: this record has no authority. It is written so the construction path runs
    against real executions before anything depends on it, and nothing reads it
    back - there is no v3 composer until §M2.
    """
    view = shadow_declaration(declared)
    definitions = gate_definitions(repo, view)
    judgments = declared.get("judgment_rungs") or []
    executed = {result["name"] for result in results if result.get("kind") == EXECUTION}
    return {
        "schema": SCHEMA_V3,
        "shadow": True,
        "authority": authority,
        # Gates the shadow layer declares that no authoritative gate executed.
        # Named rather than dropped: a v3-only declaration does not become
        # executable by being written down, and the comparison has to be able to
        # say that it stayed unpaired rather than quietly covering fewer gates.
        # Only a record that claims to establish gates has this to answer for; an
        # agent record holds judgments and establishes none by design.
        "unpaired": [] if authority == "agent" else sorted(
            name for name in definitions if name not in executed),
        # Overridden by `recorded_at` in `extra`: the shadow describes the SAME
        # execution, so it carries that execution's recording time rather than
        # taking its own. Two now() calls a few milliseconds apart can straddle a
        # second boundary, and a clock-dependent identity is not an identity.
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "verifier": verifier_identity(),
        "target_state": target_state,
        "verification_spec": {"policy_sha256": digest_of(declared)},
        "gates": [shadow_gate(repo, result, definitions, target_state, judgments)
                  for result in results],
        **extra,
    }


def execution_bindings(record_path: Path) -> dict[str, dict]:
    """What each gate's execution in that record produced, keyed by gate name.

    The one thing a v2 attestation cannot supply is §C's binding: which execution
    was judged, over which artifacts. It is named - the operator points at the
    record - rather than discovered by scanning the evidence directory for a
    plausible execution, which is the pairing mistake `shadow_path_for` exists to
    make impossible.

    This reads the shadow beside an authoritative record and affects the shadow
    attestation alone. The authoritative attestation is byte-identical with it
    and without it.
    """
    shadow = load_shadow_record(shadow_path_for(record_path))
    return {row["gate"]: {"execution_ref": row["record_id"],
                          "artifact_refs": [a["sha256"] for a in row.get("artifacts") or []]}
            for row in shadow.get("gates") or [] if row.get("kind") == EXECUTION}


def emit_shadow(repo: Path, output: Path | None, build) -> None:
    """Write a shadow record beside an authoritative one, or say why it could not be.

    Every failure here is contained. A task whose v2 gates passed does not fail
    because the v3 construction path could not build a record for them - that
    would make shadow mode more dangerous than the thing it exists to de-risk -
    but it is never silent either, because an unbuilt shadow record that nobody
    notices defeats the whole stage.
    """
    destination = (output.parent if output else repo / EVIDENCE_DIR) / SHADOW_DIR
    name = (output.stem if output else "record") + ".v3.json"
    try:
        record = build()
        destination.mkdir(parents=True, exist_ok=True)
        (destination / name).write_text(json.dumps(record, indent=2) + "\n")
    except SystemExit as refusal:
        print(f"  shadow   not constructed: the evidence/3 path refused this execution "
              f"(exit {refusal.code}); the result above is unaffected", file=sys.stderr)
    except Exception as error:  # noqa: BLE001 - a shadow failure must never fail the task
        print(f"  shadow   not constructed: {type(error).__name__}: {error}; "
              f"the result above is unaffected", file=sys.stderr)
    else:
        print(f"  shadow   {destination / name} (evidence/3, no authority)", file=sys.stderr)


def load_shadow_record(path: Path) -> dict:
    """Read a shadow record, refusing anything that is not one.

    The mirror of `load_record`, and the reason both exist: neither
    representation can be fed to the other's reader. There is no v3 composer yet,
    so nothing calls this in anger - it is the boundary, stated.
    """
    record = load_json(path)
    if record.get("schema") != SCHEMA_V3:
        raise blocked(f"{path}: not a {SCHEMA_V3} record")
    if not record.get("shadow"):
        raise blocked(f"{path}: not a shadow record; evidence/3 is not authoritative (see §M)")
    return record


def shadow_path_for(record: Path) -> Path:
    """Where §M1 wrote the shadow for this authoritative record.

    Derived, never discovered. Globbing the shadow directory and taking the
    newest file would pair whatever happens to be there with whatever is being
    read, which is the failure mode this mapping exists to make impossible.
    """
    return record.parent / SHADOW_DIR / (record.stem + ".v3.json")


def pair_shadow(record_path: Path, authoritative: dict) -> tuple[dict | None, str | None]:
    """The shadow record for this execution, or why there is not one to compare.

    Identity before comparison. Matching gate names is not identity: the same gate
    runs many times, and a shadow left behind by an earlier run sits at the same
    path as the one this run should have written. Everything the two records both
    carry has to agree before either is read as describing the other's execution.
    """
    path = shadow_path_for(record_path)
    if not path.exists():
        return None, f"no shadow record at {path.name}; §M1 emits one beside every execution"
    try:
        shadow = load_shadow_record(path)
    except SystemExit:
        return None, f"{path.name} is not a readable {SCHEMA_V3} shadow record"
    repository = (shadow.get("target_state") or {}).get("repository") or {}
    checks = (
        ("head", (authoritative["repository"]["head"], repository.get("head"))),
        ("repository state", (authoritative["repository"]["state_id"], repository.get("worktree_state"))),
        ("recording time", (authoritative.get("recorded_at"), shadow.get("recorded_at"))),
        ("gate set", (authoritative.get("gate_set"), shadow.get("gate_set"))),
        ("drift", (authoritative.get("drift"), shadow.get("drift"))),
        ("gate roster", ([g["name"] for g in authoritative["gates"]],
                         [g["gate"] for g in shadow.get("gates", [])])),
    )
    for what, (left, right) in checks:
        if left != right:
            return None, (f"{path.name} describes another execution: {what} differs "
                          f"({left!r} here, {right!r} there)")
    return shadow, None


def baseline_path_for(record: Path) -> Path:
    """Where a task's baseline sits relative to an evidence record.

    Beside the shadow records, and never in the evidence directory itself: a
    baseline swept into `compose .verification/*.json` would be read as evidence
    about a state it deliberately does not describe.
    """
    return record.parent / SHADOW_DIR / "baseline.v3.json"


def load_baseline(path: Path) -> dict:
    record = load_json(path)
    if record.get("schema") != BASELINE_SCHEMA:
        raise blocked(f"{path}: not a {BASELINE_SCHEMA} record")
    if record.get("origin") not in ("captured", "reconstructed"):
        raise blocked(f"{path}: baseline carries no origin; a reconstructed baseline must say so")
    return record


def baseline_applies(repo: Path, baseline: dict) -> tuple[bool, str | None]:
    """Whether this baseline describes a state the one under review descends from.

    A baseline binds to the state before the change, so it can never match the
    state under review - that is §F rather than a defect. What it must be is on
    this line of development: a baseline captured at a commit that is not an
    ancestor of HEAD was captured for another task, and the definitions it holds
    never governed this one.
    """
    head = ((baseline.get("target_state") or {}).get("repository") or {}).get("head")
    if not head:
        return False, "the baseline carries no commit, so there is nothing to place it against"
    try:
        git(repo, "merge-base", "--is-ancestor", head, "HEAD")
    except subprocess.CalledProcessError:
        return False, (f"the baseline was captured at {head[:12]}, which is not an ancestor of this "
                       f"HEAD; it governed another task")
    return True, None


def governance_rows(repo: Path, record_path: Path, candidate: dict,
                    rows: list[dict]) -> tuple[dict | None, list[dict]]:
    """Whether every gate still means what it meant when this task started.

    A missing baseline is not an error. Nothing about the authoritative result
    depends on one, and `compose` never reads it; this reports N/A and carries on,
    exactly as it did before a baseline surface existed. Whether a baseline
    becomes a prerequisite is M4's decision, not something M3 arrives at by
    making its absence inconvenient.
    """
    path = baseline_path_for(record_path)
    if not path.exists():
        return None, [_row("-", "governance", NOT_APPLICABLE, None, None,
                           "no baseline record for this task, so there is no governing definition "
                           "captured before the change to compare against")]
    try:
        baseline = load_baseline(path)
    except SystemExit:
        return None, [_row("-", "governance", NOT_COMPARABLE, None, None,
                           f"{path.name} is not a readable {BASELINE_SCHEMA} record")]
    applies, why = baseline_applies(repo, baseline)
    if not applies:
        return None, [_row("-", "governance", NOT_COMPARABLE, None, baseline.get("origin"), why)]
    state, findings = governance_status(baseline, candidate, rows)
    label = "governance" if state == DEFINITION_UNCHANGED else "governance (DEFINITION CHANGE PENDING)"
    return baseline, [_row("-", label, AGREE, baseline.get("origin"), state,
                           "; ".join(findings) or "every gate means what it meant at baseline")]


def _row(gate: str, predicate: str, outcome: str, observed=None, derived=None, why: str = "") -> dict:
    return {"gate": gate, "predicate": predicate, "outcome": outcome,
            "v2_observed": observed, "v3_derived": derived, "why": why}


def compare_gate(repo: Path, executed: dict, derived: dict, authority: str, governing: dict,
                 record_target: dict, current_target: dict, judgments, judged) -> list[dict]:
    """Every evidence/3 predicate that is meaningful for one compared gate.

    Diagnostic only. Nothing here decides anything about the task: the
    authoritative verdict was fixed before this ran and is not revisited.

    The shadow row is read, never edited. An earlier draft folded the record's
    authority and target into the row before handing it to the predicates, which
    silently broke `record_id` recomputation - the identity check would have
    failed for every behavioural gate, for a reason that had nothing to do with
    the gate. Record-scoped facts are passed beside the row instead.
    """
    name = executed["name"]
    rows = []

    rows.append(_row(name, "outcome", AGREE if executed["status"] == derived["status"] else DISAGREE,
                     executed["status"], derived["status"],
                     "the evidence/3 reading of this gate must not differ from what was executed"))
    rows.append(_row(name, "kind", AGREE if executed["kind"] == derived["kind"] else DISAGREE,
                     executed["kind"], derived["kind"],
                     "an execution read as a judgment, or the reverse, would satisfy the wrong requirement"))

    if name in judgments:
        # A judgment rung is not a gate. It has no definition and no authority by
        # design, so those predicates do not apply rather than failing to be
        # evaluated - reporting NOT COMPARABLE here would say the verifier could
        # not answer a question nobody asked.
        rows.append(_row(name, "definition", NOT_APPLICABLE, None, derived.get("definition"),
                         "a judgment rung is not a gate and carries no gate definition"))
        rows.append(_row(name, "authority", NOT_APPLICABLE, executed.get("kind"), None,
                         "a judgment rung is established by the agent that attests it"))
        return rows
    if name not in governing:
        rows.append(_row(name, "definition", NOT_COMPARABLE, "undeclared", derived.get("definition"),
                         "the policy declares no such gate, so there is no definition to bind to"))
        rows.append(_row(name, "authority", NOT_COMPARABLE, executed.get("command"), None,
                         "authority is declared per gate; an undeclared gate has none"))
        return rows

    claimed = {"gate": name, "authority": authority,
               "definition_sha256": derived.get("definition_sha256"),
               "target_projection": derived.get("target_projection")}

    status, reason = definition_status(claimed, governing)
    rows.append(_row(name, "definition", AGREE if status == ADMISSIBLE else DISAGREE,
                     governing[name]["definition_sha256"], derived.get("definition_sha256"),
                     reason or "the definition in force is the one this evidence was produced under"))
    status, reason = authority_status(name, authority, governing)
    rows.append(_row(name, "authority", AGREE if status == ADMISSIBLE else DISAGREE,
                     authority, governing[name]["permitted_authorities"],
                     reason or "only a declared authority may establish this gate"))

    status, reason = kind_status({"gate": name, "kind": derived["kind"]}, governing, judgments)
    rows.append(_row(name, "kind requirement", AGREE if status == ADMISSIBLE else DISAGREE,
                     derived["kind"], governing[name]["evidence_mode"],
                     reason or "the requirement takes the kind of evidence it takes"))

    declared_target = governing[name]["target"]
    # The row's projection must be the record's target narrowed to what this gate
    # declared. Checked rather than assumed: the projection is what every later
    # predicate reads, so a row carrying one its own record does not support is a
    # row that answers for a target nobody recorded.
    expected = target_projection(record_target, declared_target)
    rows.append(_row(name, "projection binding",
                     AGREE if derived.get("target_projection") == expected else DISAGREE,
                     sorted(expected), sorted(derived.get("target_projection") or {}),
                     "the recorded projection is this record's target, narrowed to this gate's dimensions"))
    status, reason = projection_status(derived.get("target_projection") or {}, declared_target, current_target)
    rows.append(_row(name, "target projection", _projection_outcome(status),
                     declared_target, sorted(derived.get("target_projection") or {}),
                     reason or "compared on exactly the dimensions this gate declares"))

    if governing[name]["evidence_mode"] != "execution+attestation":
        rows.append(_row(name, "artifact chain", NOT_APPLICABLE, None, None,
                         "this gate is an execution alone; no artifacts are judged"))
    elif derived["kind"] != EXECUTION:
        # The chain is anchored on the execution: it is the row that produced the
        # artifacts. A judgment row is one end of it, not something to evaluate
        # the whole chain against.
        rows.append(_row(name, "artifact chain", NOT_APPLICABLE, None, None,
                         "this row is the judgment half; the chain is evaluated from the execution"))
    elif judged is None:
        # The judgment half of a behavioural gate lives in its own record. Saying
        # DISAGREE because nobody handed us that record would report the
        # comparison's own scope as a defect in the verifier.
        rows.append(_row(name, "artifact chain", NOT_COMPARABLE, "execution recorded",
                         derived.get("artifacts"),
                         "no attestation record was offered to this comparison, so the judgment "
                         "half of this gate was not in scope"))
    else:
        status, reason = behavioural_status(derived, governing, [derived, *judged], repo)
        rows.append(_row(name, "artifact chain", AGREE if status == ADMISSIBLE else DISAGREE,
                         "execution recorded", derived.get("artifacts"),
                         reason or "the execution and the judgment over it bind to the same bytes"))

    # The aggregate M4 will call, beside the predicates it composes - and read as
    # a check on itself rather than as a sixth opinion. It reports AGREE when it
    # says exactly what definition, authority and projection said, so it can only
    # disagree by diverging from its own parts. That is what "aggregator only"
    # has to mean if it is to be shown rather than asserted.
    parts = [r["outcome"] for r in rows if r["predicate"] in ("definition", "authority", "target projection")]
    by_parts = all(outcome == AGREE for outcome in parts)
    status, reason = gate_admissibility(claimed, governing, current_target)
    rows.append(_row(name, "admissibility (aggregate)", AGREE if (status == ADMISSIBLE) == by_parts else DISAGREE,
                     ADMISSIBLE if by_parts else "refused by a constituent predicate", status,
                     reason or "the aggregate must say what definition, authority and projection say"))
    return rows


def _projection_outcome(status: str) -> str:
    """How an admissibility status reads as a comparison outcome.

    INADMISSIBLE and BLOCKED are NOT COMPARABLE - the evidence could not be read
    at all, which is a different report from evidence that was read and differed.
    """
    return AGREE if status == ADMISSIBLE else (DISAGREE if status == STALE else NOT_COMPARABLE)


def shadow_governing(repo: Path, declared: dict) -> tuple[dict | None, str | None]:
    """The gate definitions the shadow layer declares, or why it could not be read.

    A malformed `[shadow]` policy is a shadow diagnostic. It does not block, and
    it acquires no authority by being unreadable: a layer that could halt the
    task by being wrong would already be authoritative.
    """
    try:
        return gate_definitions(repo, shadow_declaration(declared)), None
    except SystemExit:
        return None, ("the [shadow] policy layer does not describe a set of gates; the diagnosis is "
                      "above, and nothing about the authoritative result depends on it")


def compare_records(repo: Path, record_path: Path, authoritative: dict, declared: dict,
                    judged: list[dict] | None = None) -> dict:
    """Read the shadow beside one authoritative record and say where the two differ.

    §M2. The comparison acquires no authority: v2 decided the outcome, this
    reports whether evidence/3 would have read the same execution the same way.
    A disagreement disqualifies the v3 path, not the task.
    """
    shadow, reason = pair_shadow(record_path, authoritative)
    if shadow is None:
        return {"paired": False, "reason": reason, "rows": []}

    governing, broken = shadow_governing(repo, declared)
    if governing is None:
        return {"paired": False, "reason": broken, "rows": []}
    judgments = declared.get("judgment_rungs") or []
    current_target = shadow_target(repo, declared, repository_identity(repo, relative_inside(repo, [])))
    derived_by_name = {gate["gate"]: gate for gate in shadow["gates"]}
    authority = shadow.get("authority")

    rows = []
    status, why = verifier_status([shadow])
    rows.append(_row("-", "verifier contract", AGREE if status == ADMISSIBLE else DISAGREE,
                     COMPATIBILITY, (shadow.get("verifier") or {}).get("compatibility"),
                     why or "records compose only under one set of admissibility rules"))
    baseline, governance = governance_rows(repo, record_path, governing, shadow["gates"])
    rows.extend(governance)
    if baseline is not None:
        # The definitions in force are the ones captured before the change, not
        # the working tree's - §L1. A policy read from the tree at this point is
        # one the task could have edited, and it would not even need committing.
        governing = governing_definitions(baseline)
    if shadow.get("source"):
        rows.extend(compare_ci_source(shadow, authoritative, declared, governing, derived_by_name))

    for executed in authoritative["gates"]:
        derived = derived_by_name.get(executed["name"])
        if derived is None:
            rows.append(_row(executed["name"], "pairing", NOT_COMPARABLE, executed["status"], None,
                             "the shadow record holds no row for this gate"))
            continue
        rows.extend(compare_gate(repo, executed, derived, authority, governing,
                                 shadow.get("target_state") or {}, current_target, judgments, judged))
    for name in shadow.get("unpaired") or []:
        rows.append(_row(name, "pairing", NOT_COMPARABLE, None, "declared in the shadow layer",
                         "the shadow layer declares this gate and no authoritative gate executed it; "
                         "a v3 declaration does not become executable by being written down"))
    return {"paired": True, "reason": None, "rows": rows, "shadow": shadow}


def compare_ci_source(shadow: dict, authoritative: dict, declared: dict, governing: dict,
                      derived_by_name: dict) -> list[dict]:
    """The CI provenance chain, and what each gate's step actually concluded.

    Two predicates, not one. `ci_provenance_status` establishes that this job
    belongs to this commit, run, attempt, workflow and repository; it says
    nothing about what the step concluded. `ci_step_status` reads the conclusion,
    from the raw step list the importer was given rather than from the status it
    derived - reading back a summary of an answer only establishes that the
    summary was copied correctly.
    """
    source = shadow["source"]
    # Declared, never taken from the payload under examination. Reading the
    # expected workflow out of the run that claims to be it makes the check
    # compare a value with itself, which passes for any payload at all.
    expect = (declared.get(SHADOW_POLICY) or {}).get("ci") or {}
    expected = {"head_sha": authoritative["repository"]["head"], "repository": expect.get("repository"),
                "workflow": expect.get("workflow"), "job": expect.get("job"),
                "events": declared.get("ci_head_events") or ["push"]}
    run = {"id": source.get("run_id"), "head_sha": source.get("head_sha"), "event": source.get("event"),
           "run_attempt": source.get("run_attempt"), "path": source.get("workflow"),
           "repository": {"full_name": source.get("repository")}}
    job = {"run_id": source.get("job_run_id"), "run_attempt": source.get("run_attempt"),
           "head_sha": source.get("head_sha"), "name": source.get("job"),
           "steps": source.get("steps") or []}
    status, why = ci_provenance_status(run, job, expected)
    rows = [_row("-", "ci provenance", AGREE if status == ADMISSIBLE else DISAGREE,
                 source.get("run_id"), source.get("job_run_id"),
                 why or "repository, run, attempt, workflow, job, step and commit are one chain")]
    if source.get("steps") is None:
        rows.append(_row("-", "ci step", NOT_COMPARABLE, None, None,
                         "this shadow record carries no raw step conclusions, so the evidence/3 "
                         "reading of a step could not be evaluated against the payload v2 read"))
        return rows
    for name, derived in derived_by_name.items():
        step = derived.get("step")
        if step is None:
            continue
        status, why = ci_step_status(job, step)
        executed_status = next((g["status"] for g in authoritative["gates"] if g["name"] == name), None)
        agreed = status == executed_status
        rows.append(_row(name, "ci step", AGREE if agreed else DISAGREE, executed_status, status,
                         (why if agreed else
                          f"the raw conclusions in this record do not yield the status it reports for "
                          f"step {step!r}")
                         or f"step {step!r} concluded what the authoritative record says it did"))
    return rows


def command_baseline(args) -> int:
    """Capture the definitions that govern this task, before the task changes them.

    §F and §L1. The record is non-authoritative like everything else in §M:
    `compose` never reads it, no verdict depends on it, and its absence is not an
    error. It is written into the shadow directory so the documented
    `compose .verification/*.json` cannot reach it.

    Nothing runs. A baseline captures what the gates mean, not what they say.
    """
    repo = require_repository(Path(args.repo).resolve())
    declared = policy(repo)
    output = Path(args.output).resolve() if args.output else baseline_path_for(
        repo / EVIDENCE_DIR / "record.json")
    exclude = relative_inside(repo, [output])
    if args.origin == "reconstructed" and is_dirty(repo, exclude):
        # §L5: a reconstructed baseline is reconstructible because every input is
        # reproducible from a commit. From a dirty tree it is not reconstructible
        # by anyone, which makes the claim unfalsifiable rather than true.
        raise blocked("a reconstructed baseline must come from a committed state; this tree is dirty")
    view = shadow_declaration(declared)
    record = baseline_record(repo, view, [], origin=args.origin, exclude=exclude)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2) + "\n")
    governing = record["governing"]["gates"]
    print(f"BASELINE ({record['origin']}, evidence/3, no authority)", file=sys.stderr)
    print(f"  state    {record['target_state']['repository']['worktree_state']}", file=sys.stderr)
    for name in sorted(governing):
        print(f"  governs  {name}  {governing[name]['definition_sha256'][:19]}…", file=sys.stderr)
    print(f"  baseline {output}", file=sys.stderr)
    return 0


def command_compare(args) -> int:
    """Report where the evidence/3 reading of an execution differs from the executed one.

    §M2, and it changes nothing. The authoritative record was written and its
    verdict fixed before this command could run; the exit code here describes the
    comparison, never the task.
    """
    repo = require_repository(Path(args.repo).resolve())
    record_path = Path(args.path).resolve()
    authoritative = load_record(record_path)
    judged = None
    if args.attestations:
        # Named, so the comparison's scope is the caller's statement rather than
        # whatever happened to be lying in the evidence directory.
        judged = [row for row in load_shadow_record(
            shadow_path_for(Path(args.attestations).resolve())).get("gates") or []
            if row.get("kind") == ATTESTATION]
    comparison = compare_records(repo, record_path, authoritative, policy(repo), judged)
    print(f"SHADOW COMPARISON (evidence/3, diagnostic - the task verdict is in {record_path.name})\n")
    if not comparison["paired"]:
        print(f"  NOT COMPARABLE  {comparison['reason']}")
        print("\n  No comparison was made. This does not change the authoritative result.")
        return EXIT[BLOCKED]
    for row in comparison["rows"]:
        print(f"  {row['outcome']:<15} {row['gate']:<16} {row['predicate']}")
        if row["outcome"] in (DISAGREE, NOT_COMPARABLE):
            print(f"                    executed: {row['v2_observed']!r}")
            print(f"                    evidence/3: {row['v3_derived']!r}")
            print(f"                    {row['why']}")
    disagreements = [r for r in comparison["rows"] if r["outcome"] == DISAGREE]
    incomparable = [r for r in comparison["rows"] if r["outcome"] == NOT_COMPARABLE]
    print("\n  " + "-" * 62)
    print(f"  AGREE {sum(1 for r in comparison['rows'] if r['outcome'] == AGREE)}"
          f"  DISAGREE {len(disagreements)}"
          f"  NOT COMPARABLE {len(incomparable)}"
          f"  N/A {sum(1 for r in comparison['rows'] if r['outcome'] == NOT_APPLICABLE)}")
    print("  " + "-" * 62)
    print(f"  EVIDENCE/3 QUALIFIED     {str(not disagreements and not incomparable).upper()}"
          f"   (§M3 decides activation; this is one input)")
    print("  The authoritative verdict is unchanged by anything above.")
    return EXIT[BLOCKED] if incomparable else (EXIT[FAIL] if disagreements else EXIT[PASS])


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise blocked(f"{path}: unreadable ({type(error).__name__})") from None


def load_record(path: Path) -> dict:
    record = load_json(path)
    if record.get("schema") != SCHEMA:
        raise blocked(f"{path}: not a {SCHEMA} record")
    return record


EVIDENCE_DIR = ".verification"
# Shadow records live in their own subdirectory of the evidence directory, so the
# documented `compose .verification/*.json` cannot reach them - a glob that swept
# them in would be the accidental consumption §M1 forbids, and would fail on the
# schema check rather than being refused for what it is.
SHADOW_DIR = "shadow"


def relative_inside(repo: Path, paths) -> frozenset[str]:
    """Evidence files written into the checkout must not perturb the state they describe.

    The evidence directory is excluded whole, not just the paths this command was
    handed. A run writes one record and names it, but the workflow also leaves
    attestations, imported CI payloads and, under evidence/3, artifacts in there -
    and a file this command did not name is still a file that moved the state it
    was measuring. Relying on the consumer's `.gitignore` to hide them made
    correctness depend on onboarding being remembered.
    """
    named = {p.relative_to(repo).as_posix() for p in paths if p is not None and p.is_relative_to(repo)}
    evidence = repo / EVIDENCE_DIR
    if evidence.is_dir():
        named |= {f.relative_to(repo).as_posix() for f in evidence.rglob("*") if f.is_file()}
    return frozenset(named)


def new_record(repo: Path, authority: str, gates: list[dict], exclude: frozenset[str],
               state: str | None = None, **extra) -> dict:
    """Describe one authority's gates over one state.

    `state` is passed in when it was captured before the gates ran, so that a
    record of a run describes the tree the gates saw rather than the tree they
    left behind.
    """
    return {
        "schema": SCHEMA,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "authority": authority,
        "repository": {
            "head": head_sha(repo),
            "branch": branch(repo),
            "state_id": state or state_id(repo, exclude),
            "dirty": is_dirty(repo, exclude),
        },
        "gates": gates,
        **extra,
    }


def emit(record: dict, output: Path | None) -> None:
    text = json.dumps(record, indent=2) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text)
    else:
        sys.stdout.write(text)


def run_gate(repo: Path, name: str, command: str) -> dict:
    """Execute one gate and record its result verbatim, including a failure to start."""
    started = time.monotonic()
    record = {"name": name, "kind": EXECUTION, "command": command}
    try:
        completed = subprocess.run(shlex.split(command), cwd=repo, capture_output=True, check=False)
    except (OSError, ValueError) as error:
        record |= {"status": BLOCKED, "exit_code": None, "reason": f"{type(error).__name__}: {error}"}
    else:
        output = (completed.stdout + completed.stderr).decode(errors="replace")
        record |= {
            "status": PASS if completed.returncode == 0 else FAIL,
            "exit_code": completed.returncode,
            "output_tail": output[-OUTPUT_TAIL_BYTES:],
        }
    record["duration_s"] = round(time.monotonic() - started, 3)
    return record


def verdict_of(gates: list[dict], drift: bool) -> str:
    """A gate set establishes PASS only when every gate ran and passed over one state."""
    if drift:
        return BLOCKED
    if any(gate["status"] == FAIL for gate in gates):
        return FAIL
    if any(gate["status"] == BLOCKED for gate in gates) or not gates:
        return BLOCKED
    return PASS


def command_run(args) -> int:
    repo = require_repository(Path(args.repo).resolve())
    # The policy is read whether or not gates were named on the command line. A
    # custom gate is a diagnostic convenience; it never licenses a record from a
    # repository that has declared nothing, which the documentation says is
    # BLOCKED. Without this, `run --gate anything=true` emitted a PASS-shaped
    # record against an ungoverned repository.
    declared = policy(repo)
    gates = [tuple(spec.split("=", 1)) for spec in args.gate] if args.gate else local_gates(declared)
    for spec in gates:
        if len(spec) != 2 or not spec[0] or not spec[1]:
            raise blocked(f"--gate expects NAME=COMMAND, got {'='.join(spec)!r}")
    output = Path(args.output).resolve() if args.output else None
    exclude = relative_inside(repo, [output])

    before = state_id(repo, exclude)
    before_paths = state_paths(repo, exclude)
    dirty = is_dirty(repo, exclude)
    results = [run_gate(repo, name, command) for name, command in gates]
    after = state_id(repo, exclude)
    drift = before != after

    record = new_record(repo, "local", results, exclude, state=before,
                        gate_set="policy" if not args.gate else "custom")
    record["repository"]["dirty"] = dirty
    record |= {"drift": drift, "verdict": verdict_of(results, drift)}
    if drift:
        record["state_id_after"] = after
        # Name what moved. Two digests tell a reader that something changed; the
        # paths tell them it was their own test runner's cache, and that the fix
        # is .gitignore rather than the change under verification.
        record["drift_paths"] = sorted({path for _, path in before_paths ^ state_paths(repo, exclude)})
    emit(record, output)
    summarize(record, output, file=sys.stderr)
    # After the authoritative record is written and its verdict is fixed. The
    # shadow record describes the same `results`, so no gate runs twice, and
    # nothing below can change what was just reported.
    emit_shadow(repo, output, lambda: shadow_record(
        repo, declared, results, "local",
        shadow_target(repo, declared, repository_identity(repo, exclude)),
        recorded_at=record["recorded_at"], drift=drift, gate_set=record["gate_set"]))
    return EXIT[record["verdict"]]


def command_attest(args) -> int:
    """Record an agent's judgment on a rung the machine cannot execute.

    Attestations are kept in one record and discarded wholesale when the state
    moves: an agent that repairs code must look at the diff again, and carrying
    "I reviewed this" across a repair is exactly the laundering to prevent.
    """
    repo = require_repository(Path(args.repo).resolve())
    output = Path(args.output or repo / ".verification" / "attestations.json").resolve()
    exclude = relative_inside(repo, [output])
    current = state_id(repo, exclude)

    kept = []
    if output.exists():
        previous = load_record(output)
        if previous["repository"]["state_id"] == current:
            kept = [gate for gate in previous["gates"] if gate["name"] != args.rung]
        else:
            print(f"state moved since {output}; earlier attestations discarded", file=sys.stderr)

    attestation = {
        "name": args.rung,
        "kind": ATTESTATION,
        "status": PASS,
        "note": args.note,
        "at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    record = new_record(repo, "agent", [*kept, attestation], exclude, state=current, verdict=PASS)
    emit(record, output)
    print(f"attested {args.rung} for {current}", file=sys.stderr)
    # The authoritative record is written and unchanged by everything below.
    # Nothing is re-run to manufacture the evidence/3 view: it is derived from
    # the attestation just recorded, plus the binding the caller named.
    if any((repo / name).exists() for name, _ in POLICY_FILES):
        emit_shadow(repo, output, lambda: shadow_attestation(
            repo, record, Path(args.execution).resolve() if args.execution else None,
            exclude, output))
    return 0


def kept_bindings(previous: Path, record: dict) -> dict[str, dict]:
    """Judgment bindings from the shadow written for this same state, if any."""
    try:
        earlier = load_shadow_record(previous)
    except (SystemExit, OSError):
        return {}
    repository = (earlier.get("target_state") or {}).get("repository") or {}
    if repository.get("worktree_state") != record["repository"]["state_id"]:
        return {}
    return {row["gate"]: {k: row[k] for k in ("execution_ref", "artifact_refs") if k in row}
            for row in earlier.get("gates") or []}


def shadow_attestation(repo: Path, record: dict, execution: Path | None,
                       exclude: frozenset[str], record_path: Path) -> dict:
    """The evidence/3 view of an attestation that has already been recorded.

    An attestation has no execution and this must not pretend it has one. Every
    row here is a judgment; what the binding adds is a reference to an execution
    someone else recorded, and a row whose gate that execution never covered
    simply carries no binding rather than an invented one.
    """
    declared = policy(repo)
    bindings = execution_bindings(execution) if execution else {}
    if execution and not bindings:
        print(f"  shadow   {execution.name} records no execution to bind a judgment to",
              file=sys.stderr)
    # Attesting a second rung rewrites the whole record, and the shadow is built
    # from that record - so a binding named on an earlier call would be silently
    # dropped by the next `attest` unless it is carried forward, exactly as the
    # authoritative rows are. Only while the target still matches: a binding that
    # survived a state move would be laundering the judgment across it.
    carried = kept_bindings(shadow_path_for(record_path), record)
    rows = [{**gate, **carried.get(gate["name"], {}), **bindings.get(gate["name"], {})}
            for gate in record["gates"]]
    return shadow_record(repo, declared, rows, "agent",
                         shadow_target(repo, declared, repository_identity(repo, exclude)),
                         recorded_at=record["recorded_at"])


def command_import_ci(args) -> int:
    """Normalize an Actions run into evidence, bound to the commit CI reports.

    Two separate things must hold. The run must NAME the commit under
    verification: the payload's head_sha is the branch tip, and a run naming any
    other commit is refused. The run must also have EXECUTED that commit: a
    pull_request run checks out a synthetic merge of head into base, so its
    results describe that merge, and only the events in `ci_head_events` (push,
    by default) check out the tip itself. Checking only the first would stamp
    merge-tree results with the head tree's state id - true while the branch is
    up to date with its base, and quietly false the moment it is not.
    """
    repo = require_repository(Path(args.repo).resolve())
    run, job = load_json(Path(args.run)), load_json(Path(args.job))
    declared = policy(repo)
    sha = run.get("head_sha")
    if not sha:
        raise blocked(f"{args.run}: no head_sha; cannot bind CI evidence to a commit")
    expected = args.expect_head or head_sha(repo)
    if sha != expected:
        raise blocked(f"{args.run}: run is for {sha[:12]}, expected {expected[:12]}")
    event = run.get("event")
    accepted = declared.get("ci_head_events", ["push"])
    if event not in accepted:
        raise blocked(
            f"{args.run}: a {event} run checks out a merge of {sha[:12]} into its base, so its "
            f"results describe that merge and not {sha[:12]}. Import the "
            f"{'/'.join(accepted)} run for this commit instead."
        )

    conclusions = {step.get("name"): step.get("conclusion") for step in job.get("steps", [])}
    gates = []
    for gate, step in gate_map(declared, "ci_steps").items():
        conclusion = conclusions.get(step)
        entry = {"name": gate, "kind": EXECUTION, "step": step,
                 "status": CI_CONCLUSIONS.get(conclusion, BLOCKED)}
        if entry["status"] == BLOCKED:
            entry["reason"] = f"step {conclusion or 'absent'}"
        gates.append(entry)

    record = {
        "schema": SCHEMA,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "authority": "ci",
        "repository": {"head": sha, "branch": run.get("head_branch", ""),
                       "state_id": clean_state_id(sha), "dirty": False},
        "source": {"run_id": run.get("id"), "event": event,
                   "conclusion": run.get("conclusion"), "url": run.get("html_url")},
        "gates": gates,
        "verdict": verdict_of(gates, drift=False),
    }
    output = Path(args.output).resolve() if args.output else None
    emit(record, output)
    summarize(record, output, file=sys.stderr)
    emit_shadow(repo, output, lambda: shadow_record(
        repo, declared, gates, "ci",
        {"repository": clean_repository_identity(sha)},
        recorded_at=record["recorded_at"],
        source={"repository": (run.get("repository") or {}).get("full_name"),
                "run_id": run.get("id"), "run_attempt": run.get("run_attempt"),
                "workflow": run.get("path"), "job": job.get("name"),
                "job_run_id": job.get("run_id"), "event": event, "head_sha": sha,
                # The conclusions as given, not the statuses v2 derived from
                # them. Reading back a summary of an answer only establishes
                # that the summary was copied correctly; `ci_step_status` has to
                # meet the same payload v2 met.
                "steps": [{"name": step.get("name"), "conclusion": step.get("conclusion")}
                          for step in job.get("steps") or []]}))
    return EXIT[record["verdict"]]


def compose_rows(records: list[dict]) -> dict[str, dict]:
    """Collapse every record's gates into one row per gate.

    Authorities that disagree about a gate do not average out: the gate is
    BLOCKED, because something ran twice and told two stories.
    """
    rows: dict[str, dict] = {}
    for record in records:
        authority = record.get("authority", "local")
        detail = f"run {record['source']['run_id']}" if record.get("source") else None
        for gate in record["gates"]:
            row = rows.get(gate["name"])
            if row is None:
                rows[gate["name"]] = {"status": gate["status"], "kind": gate["kind"],
                                      "authorities": [" ".join(filter(None, (authority, detail)))]}
                continue
            row["authorities"].append(" ".join(filter(None, (authority, detail))))
            if gate["kind"] == EXECUTION:
                row["kind"] = EXECUTION
            if gate["status"] != row["status"]:
                row |= {"status": BLOCKED, "reason": "authorities disagree"}
    return rows


def completion_findings(rows: dict[str, dict], declared: dict) -> list[str]:
    """Everything standing between this state and a defensible "complete"."""
    findings = []
    required = declared.get("required_gates", [])
    judgments = declared.get("judgment_rungs", [])
    for gate in required:
        row = rows.get(gate)
        if row is None:
            findings.append(f"{gate}: no evidence")
        elif row["kind"] != EXECUTION:
            findings.append(f"{gate}: attested, never executed")
        elif row["status"] != PASS:
            findings.append(f"{gate}: {row['status']}")
    for rung in judgments:
        row = rows.get(rung)
        if row is None:
            findings.append(f"{rung}: not attested for this state")
        elif row["status"] != PASS:
            findings.append(f"{rung}: {row['status']}")
    for name, row in rows.items():
        if name not in required and name not in judgments and row["status"] != PASS:
            findings.append(f"{name}: {row['status']} (not required, still failing)")
    return findings


def command_compose(args) -> int:
    repo = require_repository(Path(args.repo).resolve())
    paths = [Path(p).resolve() for p in args.paths]
    output = Path(args.output).resolve() if args.output else None
    exclude = relative_inside(repo, [*paths, output])
    records = [load_record(path) for path in paths]

    states = {record["repository"]["state_id"] for record in records}
    if len(states) > 1:
        raise blocked("records describe different states; composition needs one state:\n  " + "\n  ".join(
            f"{path.name}: {record['repository']['state_id']} ({record['repository']['head'][:12]})"
            for path, record in zip(paths, records, strict=True)))

    composed_state = states.pop() if states else None
    current = state_id(repo, exclude)
    rows = compose_rows(records)
    declared = policy(repo)
    findings = completion_findings(rows, declared)
    stale = composed_state != current
    verdict = STALE if stale else (INCOMPLETE if findings else COMPLETE)

    composite = {
        "schema": COMPOSITE,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "state": {"head": head_sha(repo), "state_id": composed_state, "current_state_id": current,
                  "match": not stale},
        "sources": [{"authority": record.get("authority"), "path": path.name} for path, record
                    in zip(paths, records, strict=True)],
        "rows": [{"gate": name, **row} for name, row in sorted(rows.items())],
        "findings": findings,
        "verdict": verdict,
    }
    if output:
        emit(composite, output)
    summarize_composite(composite, declared, file=sys.stdout)
    return EXIT[verdict]


def command_check(args) -> int:
    path = Path(args.path).resolve()
    repo = require_repository(Path(args.repo).resolve())
    record = load_record(path)
    exclude = relative_inside(repo, [path])
    current = state_id(repo, exclude)
    stale = current != record["repository"]["state_id"]
    verdict = STALE if stale else record["verdict"]
    summarize(record | {"verdict": verdict}, path, file=sys.stdout, current_state=current if stale else None)
    return EXIT[verdict]


def summarize(record: dict, path: Path | None, *, file, current_state: str | None = None) -> None:
    repository = record["repository"]
    print(f"{record['verdict']}  {repository['head'][:12]} ({repository['branch']})"
          f"{' dirty' if repository['dirty'] else ''}  [{record.get('authority', 'local')}]", file=file)
    print(f"  state    {repository['state_id']}", file=file)
    if current_state:
        print(f"  current  {current_state}  <- state changed since this record; re-verify", file=file)
    for gate in record["gates"]:
        detail = gate.get("reason") or gate.get("note") or (
            f"exit {gate['exit_code']}" if "exit_code" in gate else gate.get("step", gate["kind"]))
        print(f"  {gate['status']:<7} {gate['name']} ({detail})", file=file)
    if record.get("drift"):
        print("  drift    repository changed while gates ran; results bind to no single state", file=file)
        moved = record.get("drift_paths", [])
        for changed in moved:
            print(f"           moved  {changed}", file=file)
        if moved:
            print("           a gate wrote into the tree it was measuring. Git-ignore it if it is "
                  "disposable\n           tool output; if it is real repository state, BLOCKED is "
                  "the right answer.", file=file)
    if path:
        print(f"  evidence {path}", file=file)


def summarize_composite(composite: dict, declared: dict, *, file) -> None:
    state = composite["state"]
    print(f"VERIFICATION STATE: {state['head'][:12]} + {state['state_id']}\n", file=file)
    required = list(declared.get("required_gates", []))
    established = {row["gate"] for row in composite["rows"]}
    for row in composite["rows"]:
        mark = "*" if row["gate"] in required else " "
        print(f"  {mark} {row['gate']:<18} {row['status']:<7} {row['kind']:<12} "
              f"{', '.join(row['authorities'])}", file=file)
    for gate in required:
        # A required gate nobody ran is not an absence of news; show it.
        if gate not in established:
            print(f"  * {gate:<18} {'MISSING':<7} {'-':<12} no evidence", file=file)
    # Count on the same predicate completion uses. Counting PASS alone reported
    # zero blocked gates beside a finding reading "attested, never executed",
    # which is two instruments disagreeing about one row.
    rows = {row["gate"]: row for row in composite["rows"]}
    blocked_required = sum(1 for gate in required
                           if gate not in rows or rows[gate]["status"] != PASS
                           or rows[gate]["kind"] != EXECUTION)
    clean = composite["verdict"] == COMPLETE
    print("  " + "-" * 62, file=file)
    print(f"  BLOCKED REQUIRED GATES   {blocked_required}", file=file)
    print(f"  STALE EVIDENCE           {0 if state['match'] else len(composite['sources'])}", file=file)
    print(f"  STATE MATCH              {str(state['match']).upper()}", file=file)
    print(f"  CLEAN CLIMB              {str(clean).upper()}", file=file)
    print("  " + "-" * 62, file=file)
    print(f"  READY FOR HUMAN GATE     {str(clean).upper()}", file=file)
    for finding in composite["findings"]:
        print(f"    - {finding}", file=file)
    if not state["match"]:
        print(f"    - evidence binds to {state['state_id']}, checkout is {state['current_state_id']}", file=file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".", help="repository to verify (default: cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    runner = sub.add_parser("run", help="run the gates and write a state-bound evidence record")
    runner.add_argument("--gate", action="append", default=[], metavar="NAME=COMMAND",
                        help="replace the policy's local gates; repeat for each gate")
    runner.add_argument("--output", help="write the record here instead of stdout")
    runner.set_defaults(handler=command_run)

    attester = sub.add_parser("attest", help="record a judgment rung the machine cannot execute")
    attester.add_argument("--rung", required=True, help="the ladder rung being attested")
    attester.add_argument("--note", required=True, help="what was examined, in one line")
    attester.add_argument("--execution", metavar="RECORD",
                          help="the evidence record holding the execution this judgment is over; "
                               "affects the evidence/3 shadow only, never the attestation itself")
    attester.add_argument("--output", help="attestation record (default: .verification/attestations.json)")
    attester.set_defaults(handler=command_attest)

    importer = sub.add_parser("import-ci", help="normalize a GitHub Actions run into evidence")
    importer.add_argument("--run", required=True, help="workflow run payload (JSON)")
    importer.add_argument("--job", required=True, help="workflow job payload with steps (JSON)")
    importer.add_argument("--expect-head", help="commit the run must be for (default: local HEAD)")
    importer.add_argument("--output", help="write the record here instead of stdout")
    importer.set_defaults(handler=command_import_ci)

    composer = sub.add_parser("compose", help="combine records into one completion predicate")
    composer.add_argument("paths", nargs="+", help="evidence records to compose")
    composer.add_argument("--output", help="write the composite here")
    composer.set_defaults(handler=command_compose)

    capturer = sub.add_parser(
        "baseline", help="capture the gate definitions governing this task (evidence/3, no authority)")
    capturer.add_argument("--origin", choices=("captured", "reconstructed"), default="captured",
                          help="'reconstructed' marks a baseline rebuilt from an immutable commit")
    capturer.add_argument("--output", help="write the baseline here instead of .verification/shadow/")
    capturer.set_defaults(handler=command_baseline)

    comparer = sub.add_parser(
        "compare", help="diagnostic: how the evidence/3 reading of an execution differs from it")
    comparer.add_argument("path", help="the authoritative evidence/2 record to compare against")
    comparer.add_argument("--attestations", metavar="RECORD",
                          help="attestation record to bring into scope, for gates whose judgment "
                               "half lives in one")
    comparer.set_defaults(handler=command_compare)

    checker = sub.add_parser("check", help="re-bind a record to the current state")
    checker.add_argument("path", help="evidence record to check")
    checker.set_defaults(handler=command_check)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
