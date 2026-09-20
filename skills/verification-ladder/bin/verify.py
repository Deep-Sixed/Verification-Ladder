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
COMPOSITE = "verification.ladder.composite/1"
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

    Two checkouts share a state id only if they would verify identically. Ignored
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
            return declared
    raise blocked(f"{repo}: {ONBOARDING}")


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
    """The gates this checkout can execute, named by the repository's policy."""
    gates = gate_map(declared, "gates", "local")
    if not gates:
        raise blocked("policy declares no [gates.local]; nothing can be executed here")
    return list(gates.items())


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


def relative_inside(repo: Path, paths) -> frozenset[str]:
    """Evidence files written into the checkout must not perturb the state they describe."""
    return frozenset(p.relative_to(repo).as_posix() for p in paths if p is not None and p.is_relative_to(repo))


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
    gates = [tuple(spec.split("=", 1)) for spec in args.gate] if args.gate else local_gates(policy(repo))
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
    return 0


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
    blocked_required = sum(1 for gate in required if gate not in established or next(
        row["status"] for row in composite["rows"] if row["gate"] == gate) != PASS)
    print("  " + "-" * 62, file=file)
    print(f"  UNRESOLVED FINDINGS      {len(composite['findings'])}", file=file)
    print(f"  BLOCKED REQUIRED GATES   {blocked_required}", file=file)
    print(f"  STALE EVIDENCE           {0 if state['match'] else len(composite['sources'])}", file=file)
    print(f"  STATE MATCH              {str(state['match']).upper()}", file=file)
    print("  " + "-" * 62, file=file)
    print(f"  VERIFICATION COMPLETE    {str(composite['verdict'] == COMPLETE).upper()}", file=file)
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

    checker = sub.add_parser("check", help="re-bind a record to the current state")
    checker.add_argument("path", help="evidence record to check")
    checker.set_defaults(handler=command_check)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
