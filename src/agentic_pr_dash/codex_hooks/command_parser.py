"""Pure shell-command parsing for the Codex PR-watch hooks.

These helpers turn a raw hook ``command`` string into the structured facts the
hook entrypoints act on — top-level segments, ``cd`` targets, armable ``gh pr``
invocations, ``git push`` detection, and the effective git cwd. They are pure
(stdlib ``shlex``/``pathlib`` only, no config or I/O) so both ``run_arm_pr_watch``
and ``run_post_push_watch`` can share them and they can be tested in isolation.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path


def is_git_token(token: str) -> bool:
    """Return True iff *token* invokes the ``git`` executable.

    Matches by BASENAME so path-qualified invocations (``/usr/bin/git``,
    ``/opt/homebrew/bin/git``, ``../bin/git``) are recognized alongside the
    bare ``git`` command name — an absolute-path git must not evade git-aware
    policy hooks (BOU-2147). Non-git lookalikes (``mygit``, ``gitx``) and a
    trailing-slash ``git/`` (a directory, not an executable) stay rejected.
    """
    return token == "git" or os.path.basename(token) == "git"


def _skip_command_prefixes(tokens: list[str]) -> int:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "env":
            index += 1
            while index < len(tokens) and "=" in tokens[index]:
                index += 1
            continue
        if "=" in token:
            index += 1
            continue
        if Path(token).name in {"command", "builtin"}:
            index += 1
            continue
        break
    return index


def split_command_segments(command: str) -> list[tuple[str, str]]:
    """Split a command line into top-level ``(leading_op, segment)`` pairs.

    Breaks on ``&&``, ``||``, ``;``, ``|``, ``&`` and newlines while respecting
    single/double quotes and backslash escapes, so a compound one-liner such as
    ``git push && gh pr create --fill`` yields both commands. ``leading_op`` is
    the operator that precedes a segment (``""`` for the first) so the caller can
    honor shell conditionals — a segment guarded by ``||`` only runs on the
    previous command's failure, and a ``&&`` guard only on its success.
    """
    pairs: list[tuple[str, str]] = []
    buf: list[str] = []
    pending_op = ""  # operator that precedes the segment currently in `buf`
    quote: str | None = None
    i = 0
    n = len(command)

    def _flush(next_op: str) -> None:
        nonlocal pending_op
        seg = "".join(buf).strip()
        if seg:
            pairs.append((pending_op, seg))
        buf.clear()
        pending_op = next_op

    while i < n:
        ch = command[i]
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        two = command[i : i + 2]
        if two in ("&&", "||"):
            _flush(two)
            i += 2
            continue
        if ch in (";", "|", "&", "\n"):
            _flush(";" if ch in (";", "\n") else ch)
            i += 1
            continue
        buf.append(ch)
        i += 1
    _flush("")
    return pairs


def cd_target(command: str) -> str | None:
    """Return the destination of a ``cd <dir>`` command segment, else ``None``.

    A bare ``cd`` (to ``$HOME``) returns ``None`` — only an explicit directory
    relocates the effective arm cwd for a later segment in the same compound
    command (e.g. ``cd ../wt && gh pr ready``).
    """
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return None
    index = _skip_command_prefixes(tokens)
    if index >= len(tokens) or tokens[index] != "cd":
        return None
    index += 1
    while index < len(tokens) and tokens[index].startswith("-"):
        index += 1
    if index < len(tokens):
        return tokens[index]
    return None


def _names_explicit_repo(tokens: list[str]) -> bool:
    """True if a ``-R``/``--repo`` flag targets another repository.

    ``gh pr ready``/``create`` inherit ``-R/--repo`` to act on a different repo
    (https://cli.github.com/manual/gh_pr_ready). We can only arm a PR in the
    current worktree's repo, so an explicit repo means "not ours" — skip rather
    than mis-mark the local cwd.
    """
    for tok in tokens:
        if tok in {"-R", "--repo"} or tok.startswith("--repo="):
            return True
        if tok.startswith("-R") and len(tok) > 2:
            return True
    return False


def parse_gh_pr_arm_target(command: str):
    """Parse a single command segment for a PR-arming ``gh pr`` invocation.

    Returns ``None`` when the segment is not an armable ``gh pr create|ready|new``
    in the current repo (including explicit ``-R/--repo`` or a pull URL, which
    may name another repo we can't arm from here). Otherwise returns
    ``(pr_number, branch)`` with at most one set: an explicit ``gh pr ready 123``
    yields a pr number; a ``create --head <branch>`` or non-numeric
    ``ready <branch>`` yields a branch; a plain ``create``/``ready`` yields
    ``(None, None)`` so arm resolves the current branch.
    """
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return None

    index = _skip_command_prefixes(tokens)
    if index >= len(tokens):
        return None
    token = tokens[index]
    if token != "gh" and not token.endswith("/gh"):
        return None
    index += 1

    value_flags = {"-R", "--repo"}
    while index < len(tokens) and tokens[index].startswith("-"):
        index += 2 if tokens[index] in value_flags else 1

    if index >= len(tokens) or tokens[index] != "pr":
        return None
    index += 1

    if _names_explicit_repo(tokens):
        return None

    rest = tokens[index:]
    subcommand = None
    head_branch = None
    positional = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if subcommand is None and not tok.startswith("-"):
            subcommand = tok
            i += 1
            continue
        if tok in {"-H", "--head"} and i + 1 < len(rest):
            head_branch = rest[i + 1]
            i += 2
            continue
        if tok.startswith("--head="):
            head_branch = tok[len("--head=") :]
            i += 1
            continue
        if tok.startswith("-H") and tok != "-H":
            # Attached shorthand: `-Hfeature` or `-H=feature` (pflag syntax).
            head_branch = tok[2:].lstrip("=")
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if positional is None:
            positional = tok
        i += 1

    if subcommand in {"create", "new"}:
        # create/new take NO positional branch — only --head names one, so a
        # positional here is an option value (e.g. `--title 'Fix'`), never a
        # branch. Owner-prefix stripping for --head happens in `arm`.
        return (None, head_branch) if head_branch else (None, None)

    if subcommand == "ready":
        if positional is None:
            return (None, None)  # arm the current branch
        bare = positional.lstrip("#")
        if bare.isdigit():
            return (bare, None)  # explicit PR number in the current repo
        if "/pull/" in positional:
            return None  # a pull URL may point at another repo — skip
        return (None, positional)  # a branch designator

    return None


def is_gh_pr_open(command: str) -> bool:
    return parse_gh_pr_arm_target(command) is not None


def is_git_push(command: str) -> bool:
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return False
    if not tokens:
        return False

    index = _skip_command_prefixes(tokens)
    if index >= len(tokens):
        return False
    token = tokens[index]
    if not is_git_token(token):
        return False

    index += 1
    option_value_flags = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
    while index < len(tokens):
        token = tokens[index]
        if token == "push":
            return True
        if token.startswith("-"):
            option_name = token.split("=", 1)[0]
            if option_name in option_value_flags:
                index += 2 if "=" not in token else 1
                continue
            if token.startswith("-C") and token != "-C":
                index += 1
                continue
            if token.startswith("-c") and token != "-c":
                index += 1
                continue
            if (
                token.startswith("--git-dir=")
                or token.startswith("--work-tree=")
                or token.startswith("--namespace=")
            ):
                index += 1
                continue
            return False
        return False
    return False


def git_push_source_branch(command: str) -> tuple[bool, str | None]:
    """Return whether push has refspecs and its single local branch source."""
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return False, None
    index = _skip_command_prefixes(tokens)
    while index < len(tokens) and tokens[index] != "push":
        index += 1
    if index >= len(tokens):
        return False, None
    index += 1
    value_options = {"--repo", "--receive-pack", "--exec", "-o", "--push-option"}
    positionals: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            positionals.extend(tokens[index + 1 :])
            break
        if token in value_options:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        positionals.append(token)
        index += 1
    refspecs = positionals[1:] if positionals else []  # first positional is repository
    if not refspecs:
        return False, None
    if len(refspecs) != 1:
        return True, None
    source = refspecs[0].lstrip("+").split(":", 1)[0]
    if not source or source == "HEAD" or source.startswith("refs/tags/"):
        return True, None
    return True, source.removeprefix("refs/heads/")


def effective_git_cwd(command: str, base_cwd: str) -> str:
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return base_cwd

    index = _skip_command_prefixes(tokens)
    env_work_tree = None
    for prefix in tokens[:index]:
        if prefix.startswith("GIT_WORK_TREE="):
            env_work_tree = prefix.split("=", 1)[1]

    if index >= len(tokens):
        return base_cwd
    token = tokens[index]
    if not is_git_token(token):
        return base_cwd
    index += 1

    cwd = base_cwd
    work_tree = env_work_tree
    while index < len(tokens):
        token = tokens[index]
        if token == "push":
            break
        if token == "-C" and index + 1 < len(tokens):
            cwd = str((Path(cwd) / tokens[index + 1]).resolve())
            index += 2
            continue
        if token.startswith("-C") and token != "-C":
            cwd = str((Path(cwd) / token[2:]).resolve())
            index += 1
            continue
        if token == "--work-tree" and index + 1 < len(tokens):
            work_tree = tokens[index + 1]
            index += 2
            continue
        if token.startswith("--work-tree="):
            work_tree = token[len("--work-tree=") :]
            index += 1
            continue
        index += 1

    if work_tree is not None:
        return str((Path(cwd) / work_tree).resolve())
    return cwd
