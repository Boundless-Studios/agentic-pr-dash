#!/usr/bin/env python3
"""PreToolUse hook: block a `git push` that would resurrect a merged branch
whose remote ref GitHub already deleted (BOU-2499).

Incident this guards against: a PR merges mid-session (GitHub squash-merges it
and auto-deletes the remote branch), but the session's worktree is still on
that branch with a fresh commit. A subsequent plain ``git push`` does NOT
fail -- git happily recreates the ref:

    * [new branch]          bou-2348-pipeline-rewire -> bou-2348-pipeline-rewire

and exits 0. `[new branch]` reads exactly like an unremarkable first push, so
nothing about the output says "the PR you were working on is already merged
and gone". The commit lands on a branch with no PR attached, and every
"is my work delivered?" probe that diffs against ``origin/main..HEAD`` now
reports the WHOLE squashed changeset as undelivered (none of the original
commits are ancestors of ``main`` post-squash), inviting a duplicate PR.

Detection is deliberately git-only (no ``gh``/network dependency beyond the
target remote itself), so it is fast and available even when the PR-dashboard
tooling is down:

  - the LOCAL branch being pushed has upstream tracking CONFIGURED
    (``branch.<name>.remote``/``branch.<name>.merge`` are set) -- i.e. it was
    pushed successfully before; and
  - the REMOTE ref it would push to does NOT currently exist
    (``git ls-remote --exit-code --heads <remote> refs/heads/<branch>``
    reports no match).

That combination -- "used to be tracked, remote ref is gone now" -- is exactly
the resurrection signature. A branch that was NEVER pushed (no upstream
configured yet) is a genuine new branch and must go through untouched; a
branch whose remote ref still exists is a genuine live push and must also go
through untouched. Anything the remote query cannot answer definitively
(timeout, auth failure, offline) fails OPEN: this hook only ever blocks a case
it has positively confirmed, never a push it merely failed to check.

This is a narrower, single-purpose parser than ``block-production.py``'s
adversarial-input-hardened command walker -- BOU-2499 is an accidental-mistake
guard, not a security boundary, so it only needs to recognise the git-push
invocations an agent actually issues (not obfuscated/injected ones).

Advisory-adjacent but NOT silent: a confirmed resurrection case blocks the
push (exit 2, message on stderr) rather than merely warning, because by the
time the push output is visible the resurrection has already happened.

State: none. Every check is a live git query against the command being run.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .codex_hooks.command_parser import (
    cd_target,
    effective_git_cwd,
    is_git_push,
    split_command_segments,
)

# Total wall-clock budget for ALL of a push's remote `ls-remote` checks
# combined, not a per-call timeout: `check_command()` sets a single deadline
# once per invocation and every `_remote_ref_exists()` call gets whatever
# time remains against it. A multi-refspec push that independently spent
# this long per target could otherwise scale linearly with refspec count and
# outrun the outer hook-level timeout (15s in `.claude/settings.json`) before
# reaching the guard's own fail-open resolution (PR #2887 P2 review). Local,
# non-network git-config reads (`_tracks_destination()`) are intentionally NOT
# metered against this budget -- they never hit a remote and are not the
# hang risk the shared deadline exists for.
GIT_TIMEOUT_SECONDS = 8

_DELETE_FLAGS = {"-d", "--delete"}

# Flags that DO take a separate value token (`-o ci.skip`, not `-o=ci.skip`).
# Missing one of these lets its value be mis-consumed as the remote/refspec
# positional, which can make the upstream/remote checks resolve the wrong
# branch entirely and fail open on a genuine resurrection (BOU-2571).
_VALUE_FLAGS = {
    "-o", "--push-option",
    "--receive-pack",
    "--exec",
    "--repo",
}

_REFS_HEADS_PREFIX = "refs/heads/"

# `-n`/`--dry-run` never mutate the remote -- git only reports what WOULD
# happen. Nothing this hook could confirm about a dry run is a real
# resurrection, so it must never block one (BOU-2576 finding 3).
_DRY_RUN_FLAGS = {"-n", "--dry-run"}

def _segments(command: str) -> list[str]:
    """Split a shell command into top-level segments on &&, ||, ;, |.

    Not a full shell grammar -- good enough to find a `git push` invocation
    among ordinary agent-issued command chains (`cd x && git push`, `git add
    -A && git commit -m ... && git push`), which is all this guard needs.
    """
    return [segment for _op, segment in split_command_segments(command)]


def _push_segment(command: str) -> str | None:
    """Return the first shell segment that is a `git push` invocation, else None."""
    for segment in _segments(command):
        if is_git_push(segment):
            return segment
    return None


def _push_context(command: str, cwd: Path) -> tuple[str, Path] | None:
    """Return the push segment and its effective cwd after shell/git relocation."""
    current = cwd
    segments = split_command_segments(command)
    saw_unmodeled_command = False
    for index, (leading_op, segment) in enumerate(segments):
        destination = cd_target(segment)
        if destination is not None:
            next_op = segments[index + 1][0] if index + 1 < len(segments) else ""
            target = Path(destination).expanduser()
            target = target.resolve() if target.is_absolute() else (current / target).resolve()
            cd_succeeds = target.is_dir()
            if next_op == "||" and cd_succeeds:
                return None  # the push segment cannot execute
            if not cd_succeeds:
                if next_op == "&&":
                    return None  # a command chained to the failed cd cannot execute
                continue
            if saw_unmodeled_command and leading_op in {"&&", "||"}:
                return None  # execution of this cd is conditional and unknowable pre-run
            current = target
            continue
        if is_git_push(segment):
            return segment, Path(effective_git_cwd(segment, str(current)))
        saw_unmodeled_command = True
    return None


def _push_tokens(segment: str) -> list[str]:
    """Return arguments after the git ``push`` subcommand."""
    tokens = shlex.split(segment)
    for index, token in enumerate(tokens):
        if token == "push":
            return tokens[index + 1 :]
    return []


def _is_dry_run(segment: str) -> bool:
    """True when the push segment carries `-n`/`--dry-run`.

    A dry run never mutates the remote -- git only prints what it WOULD do --
    so nothing about the tracked-upstream/absent-remote-ref signature this
    guard looks for can be a real resurrection when the push itself pushes
    nothing (BOU-2576 finding 3).
    """
    try:
        tokens = _push_tokens(segment)
    except ValueError:
        return False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _DRY_RUN_FLAGS:
            return True
        if token in _VALUE_FLAGS:
            index += 2
            continue
        index += 1
    return False


def _parse_push_args(segment: str) -> tuple[str | None, list[str], bool, bool]:
    """Parse a push into (remote, branch_refspecs, is_delete, had_refspecs).

    ``branch_refspecs`` keeps every branch-like positional refspec while
    discarding the `tag <tag>` shorthand pair. `git push` accepts
    `[<repository> [<refspec>...]]`, and checking only the first would let a
    later refspec in the same invocation resurrect a deleted branch while an
    earlier, still-alive one made the guard stop looking (BOU-2571 P2 review:
    `git push origin live feature/x` must still catch a deleted `feature/x`
    even though `live` is fine).

    A bare `--tags` (no other refspec) is also treated as "a destination was
    supplied" via ``had_refspecs``, the same way the `tag <name>` shorthand
    already is: `--tags` publishes every tag ref and, with no other refspec
    on the command line, never touches the current branch at all -- so it
    must not fall through to the current-branch check below (BOU-2576
    finding 4). `--follow-tags` is deliberately NOT included here: unlike
    `--tags`, it still pushes the current branch (plus any newly-reachable
    annotated tags), so the current-branch check must still run for it.
    """
    tokens = _push_tokens(segment)

    is_delete = False
    saw_bare_tags_flag = False
    repo_option: str | None = None
    positionals: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in _DELETE_FLAGS:
            is_delete = True
            i += 1
            continue
        if tok in _VALUE_FLAGS:
            # Consume the flag AND its separate value token (`-o ci.skip`) so
            # the value is never mistaken for the remote/refspec positional
            # (BOU-2571 P2 review: `git push -o ci.skip origin feature/x`
            # must not parse `ci.skip` as the remote).
            if tok == "--repo" and i + 1 < n:
                repo_option = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("--repo="):
            repo_option = tok.partition("=")[2]
            i += 1
            continue
        if tok.startswith("-"):
            if tok == "--tags":
                saw_bare_tags_flag = True
            i += 1
            continue  # a flag we don't special-case; assume no separate value
        positionals.append(tok)
        i += 1

    if repo_option is not None:
        remote = repo_option
        refspecs = positionals
    else:
        remote = positionals[0] if positionals else None
        refspecs = positionals[1:]

    had_refspecs = bool(refspecs) or saw_bare_tags_flag
    branch_refspecs: list[str] = []
    i = 0
    while i < len(refspecs):
        if refspecs[i] == "tag" and i + 1 < len(refspecs):
            i += 2
            continue
        branch_refspecs.append(refspecs[i])
        i += 1
    return remote, branch_refspecs, is_delete, had_refspecs


def _bulk_push_mode(segment: str) -> str | None:
    """Return ``all``/``mirror`` when either bulk branch mode was requested."""
    try:
        tokens = _push_tokens(segment)
    except ValueError:
        return None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _VALUE_FLAGS:
            index += 2
            continue
        if token == "--all":
            return "all"
        if token == "--mirror":
            return "mirror"
        index += 1
    return None


def _resolve_target(
    remote: str | None, refspec: str | None
) -> tuple[str, str, str] | None:
    """Return (remote_name, local_branch, remote_branch) or None when this push
    shape isn't a branch update the guard can reason about."""
    remote_name = remote or "origin"

    if refspec:
        refspec = refspec.lstrip("+")
        if ":" in refspec:
            local_part, _, remote_part = refspec.partition(":")
            if not local_part or not remote_part:
                return None
            local_branch = local_part
            remote_branch = remote_part
        else:
            local_branch = refspec
            remote_branch = refspec
    else:
        local_branch = "HEAD"
        remote_branch = "HEAD"  # resolved to the current branch name below

    if local_branch == "@":
        local_branch = "HEAD"
    if local_branch != "HEAD" and local_branch.startswith(_REFS_HEADS_PREFIX):
        # `git push origin refs/heads/feature/x` (or the local half of a
        # `local:remote` refspec) is fully qualified.
        # `_tracks_destination()` reads `branch.<name>.*` config keyed on the
        # BARE branch name -- passing the qualified form through leaves that
        # lookup permanently missing and misclassifies an already-tracked
        # branch as "never pushed before", which fails OPEN on exactly the
        # resurrection case this guard exists to catch (BOU-2571 P2 review).
        local_branch = local_branch[len(_REFS_HEADS_PREFIX):]

    if remote_branch.startswith(_REFS_HEADS_PREFIX):
        remote_branch = remote_branch[len(_REFS_HEADS_PREFIX):]
    elif remote_branch.startswith("refs/"):
        return None

    return remote_name, local_branch, remote_branch


def _run(args: list[str], cwd: Path) -> str | None:
    """Run a git command; return stdout, or None on any failure/timeout."""
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _current_branch_name(cwd: Path) -> str | None:
    out = _run(["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if out is None:
        return None
    name = out.strip()
    return name if name and name != "HEAD" else None


def _config_value(key: str, cwd: Path) -> str | None:
    return _run(["git", "-C", str(cwd), "config", "--get", key], cwd)


def _push_remote(local_branch: str, cwd: Path) -> str:
    """Resolve Git's remote selection for a push with no repository argument."""
    for key in (
        f"branch.{local_branch}.pushRemote",
        "remote.pushDefault",
        f"branch.{local_branch}.remote",
    ):
        value = _config_value(key, cwd)
        if value and value.strip() and value.strip() != ".":
            return value.strip()
    return "origin"


def _local_branches(cwd: Path) -> list[str]:
    out = _run(
        ["git", "-C", str(cwd), "for-each-ref", "--format=%(refname:short)", "refs/heads"],
        cwd,
    )
    return [line for line in (out or "").splitlines() if line]


def _configured_upstream(
    local_branch: str, cwd: Path
) -> tuple[str, str] | None:
    """Return the branch's configured (remote, remote branch), if complete."""
    remote = _run(
        ["git", "-C", str(cwd), "config", "--get", f"branch.{local_branch}.remote"],
        cwd,
    )
    merge = _run(
        ["git", "-C", str(cwd), "config", "--get", f"branch.{local_branch}.merge"],
        cwd,
    )
    if not remote or not merge or not merge.strip().startswith(_REFS_HEADS_PREFIX):
        return None
    return remote.strip(), merge.strip()[len(_REFS_HEADS_PREFIX):]


def _push_url(remote: str, cwd: Path) -> str | None:
    """Return `remote`'s configured push URL, or None when it isn't an alias."""
    out = _run(
        ["git", "-C", str(cwd), "remote", "get-url", "--push", remote],
        cwd,
    )
    if out is None:
        return None
    url = out.strip()
    return url or None


def _push_urls(remote: str, cwd: Path) -> list[str]:
    """Return every configured push destination, or the raw repository value."""
    out = _run(
        ["git", "-C", str(cwd), "remote", "get-url", "--push", "--all", remote],
        cwd,
    )
    urls = [line.strip() for line in (out or "").splitlines() if line.strip()]
    return urls or [remote]


def _same_remote(upstream_remote: str, remote: str, cwd: Path) -> bool:
    """True when both names denote the same push destination.

    `git push <repository>` accepts a configured alias OR a raw URL/path, and
    a repo can configure SEVERAL aliases for one URL. Comparing only the
    literal strings -- or resolving only the upstream side to a URL -- misses
    both directions of that: `git push /tmp/origin.git feature/x` (raw path
    for the alias `origin`) and `git push mirror feature/x` (a second alias
    sharing `origin`'s push URL) each reach exactly the ref `origin/feature/x`
    tracks, so each can resurrect it (BOU-2571 P2 review). Resolving BOTH
    sides and comparing the results covers every combination; a name that
    isn't a configured alias resolves to itself, which is what makes the raw
    URL/path form compare equal to the alias it names.
    """
    if upstream_remote == remote:
        return True
    return (_push_url(upstream_remote, cwd) or upstream_remote) == (
        _push_url(remote, cwd) or remote
    )


def _tracks_destination(
    local_branch: str, remote: str, remote_branch: str, cwd: Path
) -> bool:
    """True when the branch's configured upstream is this exact destination.

    Reads `branch.<name>.remote` / `branch.<name>.merge` directly from git
    config rather than resolving `<local_branch>@{u}`. `@{u}` requires the
    local remote-tracking ref (`refs/remotes/<remote>/<branch>`) to exist, but
    a `git fetch --prune` run after the remote branch is deleted removes
    exactly that ref while leaving the `branch.*` config untouched -- so
    `@{u}` stops resolving even though the tracking configuration (the actual
    "was this pushed before" signal) is still present. Requiring `@{u}` to
    resolve therefore lets this guard be defeated by the single most likely
    real-world sequence: merge, fetch, push (BOU-2499 P1).
    """
    upstream = _configured_upstream(local_branch, cwd)
    if upstream is None:
        return False
    upstream_remote, upstream_branch = upstream
    if upstream_branch != remote_branch:
        return False
    return _same_remote(upstream_remote, remote, cwd)


def _remote_ref_exists(
    remote: str, branch: str, cwd: Path, deadline: float
) -> bool | None:
    """True/False when determined; None when the check was inconclusive.

    An inconclusive result (offline, auth failure, unreachable remote) must
    never block a push -- this guard only fires on a POSITIVELY confirmed
    absence, never on an inability to check.

    Queries the fully-qualified `refs/heads/<branch>` rather than the bare
    branch name: `git ls-remote` treats an unqualified pattern as a ref-TAIL
    match, so querying e.g. `foo` also matches `refs/heads/bar/foo` -- if the
    tracked `foo` branch was deleted while `bar/foo` still exists, that
    tail-match would report success and let this guard recreate `foo`
    (BOU-2499 P1). The returned ref name is also verified exactly rather than
    trusting the exit code alone, as defense in depth against the same
    substring/tail-match failure mode.
    """
    exact_ref = f"refs/heads/{branch}"
    inconclusive = False
    for push_destination in _push_urls(remote, cwd):
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            inconclusive = True
            break
        try:
            result = subprocess.run(
                ["git", "-C", str(cwd), "ls-remote", "--exit-code", "--heads", push_destination, exact_ref],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            inconclusive = True
            continue
        if result.returncode == 0:
            exists = any(
                line.split("\t", 1)[-1].strip() == exact_ref
                for line in result.stdout.splitlines()
                if "\t" in line
            )
            if not exists:
                return False
            continue
        if result.returncode == 2:
            return False
        inconclusive = True
    return None if inconclusive else True


def _check_one_target(
    remote: str | None, refspec: str | None, cwd: Path, deadline: float
) -> str | None:
    """Return a blocking message for one confirmed-dead refspec, else None."""
    resolved = _resolve_target(remote, refspec)
    if resolved is None:
        return None
    remote_name, local_branch, remote_branch = resolved

    if local_branch == "HEAD":
        current = _current_branch_name(cwd)
        if current is None:
            return None  # detached HEAD or unreadable repo -- not our concern
        local_branch = current
        if remote_branch == "HEAD" and refspec is None:
            push_default = _config_value("push.default", cwd)
            mode = push_default.strip() if push_default else "simple"
            if mode in {"matching", "nothing"}:
                return None  # these modes cannot recreate an absent current ref
            if mode == "upstream":
                upstream = _configured_upstream(local_branch, cwd)
                if upstream is None:
                    return None
                configured_remote, remote_branch = upstream
                if remote is None:
                    selected_remote = _push_remote(local_branch, cwd)
                    if not _same_remote(configured_remote, selected_remote, cwd):
                        return None  # Git rejects upstream pushes to another remote
                    remote_name = configured_remote
            else:
                remote_branch = current
                if remote is None:
                    remote_name = _push_remote(local_branch, cwd)
        elif remote_branch == "HEAD":
            remote_branch = current

    if not _tracks_destination(local_branch, remote_name, remote_branch, cwd):
        return None

    exists = _remote_ref_exists(remote_name, remote_branch, cwd, deadline)
    if exists is not False:
        return None  # exists, or the check was inconclusive -- fail open

    return (
        f"BLOCKED (BOU-2499): `{local_branch}` has upstream tracking configured, "
        f"but `{remote_name}/{remote_branch}` no longer exists on the remote.\n"
        "\n"
        "This is the signature of a PR that merged mid-session: GitHub squash-"
        "merges and auto-deletes the branch, and this `git push` would SILENTLY "
        "RESURRECT it as a fresh branch with no PR attached -- `[new branch]` in "
        "the push output reads exactly like an ordinary first push, and the "
        "commit lands with nothing pointing back to the work that was already "
        "merged.\n"
        "\n"
        f"Verify first: `gh pr view {local_branch} --json state,number,url`.\n"
        "If it shows MERGED, do NOT push this branch. Recover instead:\n"
        "  1. git checkout -b <new-branch> origin/main\n"
        "  2. git cherry-pick <the commit(s) made since the merge>\n"
        "  3. git push -u origin <new-branch>\n"
        "  4. gh pr create --title '...' --body '...'\n"
        f"  5. delete the stale local branch: git branch -D {local_branch}\n"
        "\n"
        "If the remote ref is gone for some other reason (not a merge), confirm "
        "with `gh pr view` / `git log` before pushing -- do not bypass this check "
        "without first understanding why the ref disappeared."
    )


def check_command(command: str, cwd: Path) -> str | None:
    """Return a blocking message for a confirmed resurrection push, else None.

    Checks EVERY refspec in the push, not just the first -- `git push origin
    live feature/x` must still block on a deleted `feature/x` even though
    `live` (checked first) is fine (BOU-2571 P2 review).
    """
    context = _push_context(command, Path(cwd))
    if context is None:
        return None
    segment, effective_cwd = context

    if _is_dry_run(segment):
        return None  # pushes nothing -- cannot resurrect anything (finding 3)

    remote, refspecs, is_delete, had_refspecs = _parse_push_args(segment)
    if is_delete:
        return None  # deleting a ref is never a resurrection
    if had_refspecs and not refspecs:
        return None  # every supplied destination was non-branch (e.g. tags)

    # No refspec at all (`git push` / `git push origin`) resolves to the
    # current branch -- represented as a single `None` target, same as before
    # this refspec list existed.
    bulk_mode = _bulk_push_mode(segment)
    if bulk_mode:
        current_branch = _current_branch_name(effective_cwd) or ""
        bulk_remote = remote or _push_remote(current_branch, effective_cwd)
        targets = [
            (bulk_remote, f"{branch}:{branch}")
            for branch in _local_branches(effective_cwd)
        ]
    else:
        targets = [(remote, refspec) for refspec in (list(refspecs) if refspecs else [None])]
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    for target_remote, refspec in targets:
        message = _check_one_target(target_remote, refspec, effective_cwd, deadline)
        if message is not None:
            return message
    return None


def _resolve_cwd(data: dict) -> Path:
    """Resolve the directory this push is actually running in.

    Prefers the payload's own `cwd` (e.g. Codex's `exec_command`
    `tool_input.workdir`, threaded through by
    scripts/codex-hooks/common.py's `normalized_cwd()`/`normalized_payload()`)
    over `CLAUDE_PROJECT_DIR`. `apply_shared_env()` unconditionally pins
    `CLAUDE_PROJECT_DIR`/`GAIA_PROJECT_DIR` to the Gaia repo root before this
    hook runs on the Codex path, so trusting the env var alone made the guard
    inspect Gaia's branch state for a push issued from a different
    checkout/worktree entirely -- able to both miss a real resurrection there
    and false-block an unrelated legitimate push (BOU-2571 P2 review).

    A RELATIVE payload `cwd` (Codex's `workdir` can be a bare relative path
    like `backend`) is anchored to `CLAUDE_PROJECT_DIR`/`GAIA_PROJECT_DIR`,
    NOT the guard's own ambient process cwd. `run_bash_policy.run_script()`
    (the Codex dispatch path) never sets an explicit subprocess `cwd` for
    this hook -- it just inherits whatever ambient cwd the calling chain
    happened to have, which nothing guarantees equals the target repo.
    Resolving a bare relative path with `Path.resolve()` alone would silently
    anchor to that unreliable ambient cwd instead, re-opening the exact
    "wrong repository" class this hook's payload-cwd preference was meant to
    close (PR #2887 P1 review).
    """
    anchor = Path(
        os.environ.get("CLAUDE_PROJECT_DIR")
        or os.environ.get("GAIA_PROJECT_DIR")
        or os.getcwd()
    )
    payload_cwd = data.get("cwd")
    if isinstance(payload_cwd, str) and payload_cwd:
        candidate = Path(payload_cwd)
        if not candidate.is_absolute():
            candidate = anchor / candidate
        return candidate.resolve()
    return anchor.resolve()


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    if data.get("tool_name") != "Bash":
        return 0

    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return 0

    cwd = _resolve_cwd(data)
    message = check_command(command, cwd)
    if message:
        print(message, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 -- must never wedge a turn on an internal bug
        sys.exit(0)
