"""Codex hook adapter for PR-watch ownership arming.

The generic hook behavior is: parse a Codex hook payload, identify events that
should register PR maintenance ownership, and delegate the actual marker write
to ``agentic_pr_dash maintenance_check arm``. Repo-local shims may keep policy
settings and fallbacks, but the marker-writing source of truth lives upstream.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from agentic_pr_dash import maintenance_check

_TRUE_VALUES = {"1", "true", "TRUE", "True", "yes", "YES", "on", "ON"}


def load_payload() -> dict:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - hooks are advisory
        return {}
    return payload if isinstance(payload, dict) else {}


def normalized_payload(payload: dict) -> dict:
    """Map Codex ``exec_command`` payloads to the Bash shape Claude hooks use.

    A Codex ``exec_command`` may carry its own ``tool_input.workdir`` — the
    directory the command actually ran in (e.g. ``gh pr create`` invoked against
    a sibling worktree). That executed workdir, when present, is the correct cwd
    to arm: the top-level payload ``cwd`` / hook process cwd can point at a
    different worktree and make ``maintenance_check arm`` resolve the wrong (or
    no) PR. Surface it as the normalized ``cwd`` so the workdir wins.
    """
    tool_name = payload.get("tool_name")
    if tool_name in {"exec_command", "functions.exec_command"}:
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        command = tool_input.get("cmd", "")
        normalized = dict(payload)
        normalized["tool_name"] = "Bash"
        normalized["tool_input"] = {"command": command}
        workdir = tool_input.get("workdir")
        if isinstance(workdir, str) and workdir:
            normalized["cwd"] = workdir
        return normalized
    return payload


def normalized_cwd(payload: dict) -> str:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return os.path.abspath(cwd)
    return os.getcwd()


def session_id_from_payload(payload: dict) -> str:
    raw = payload.get("session_id")
    if isinstance(raw, str) and raw:
        return raw
    return os.environ.get("CODEX_SESSION_ID") or os.environ.get("GAIA_SESSION_ID") or ""


def _strip_optional_quotes(value: str) -> str:
    return value.strip().strip('"').strip("'")


def _configured_autoloop_flag() -> str:
    flag = os.environ.get("AGENTIC_PR_DASH_PR_WATCH_AUTOLOOP") or os.environ.get("GAIA_PR_WATCH_AUTOLOOP", "")
    if flag:
        return flag

    conf = os.environ.get("WORKTREE_CONSOLE_CONFIG") or str(Path.home() / ".config" / "gaia" / "worktree-console.conf")
    try:
        lines = Path(conf).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        key, sep, value = line.partition("=")
        if sep and key.strip() == "WC_PR_WATCH_AUTOLOOP":
            return _strip_optional_quotes(value)
    return ""


def pr_watch_autoloop_enabled() -> bool:
    return _configured_autoloop_flag() in _TRUE_VALUES


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
    if token != "git" and not token.endswith("/git"):
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
    if token != "git" and not token.endswith("/git"):
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


def _command_failed(payload: dict) -> bool:
    """Best-effort: True only when the tool reports a non-zero exit code.

    A PostToolUse fires after the command ran; the exit code (when the harness
    provides it) tells us whether a ``&&``-guarded later segment actually
    executed. Absent/unparseable exit info → ``False`` (assume it ran), so the
    common ``git push && gh pr create`` still arms.
    """
    response = payload.get("tool_response")
    candidates = []
    if isinstance(response, dict):
        for key in ("exit_code", "exitCode", "returncode", "code", "status"):
            if key in response:
                candidates.append(response.get(key))
    for key in ("exit_code", "exitCode", "returncode"):
        if key in payload:
            candidates.append(payload.get(key))
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value != 0
        if isinstance(value, str) and value.lstrip("-").isdigit():
            return int(value) != 0
    return False


def _arm(cwd: str, session_id: str, *, pr: str | None = None, branch: str | None = None) -> None:
    # Deliberately omit --pid: when this hook runs through a repo-local shell
    # shim, os.getppid() is the short-lived shell that exits the instant the
    # hook returns, so a marker stamped with it looks dead immediately and a
    # sibling/detached loop can adopt the same PR. Letting `arm` resolve the pid
    # walks the ancestry to the durable claude/codex session instead.
    argv = ["arm", "--cwd", cwd, "--session-id", session_id]
    if pr is not None:
        argv += ["--pr", str(pr)]
    elif branch is not None:
        argv += ["--branch", branch]
    result = maintenance_check.main(argv)
    if result != 0:
        print(f"run_arm_pr_watch.py: arm returned {result}; continuing", file=sys.stderr)


def main() -> int:
    payload = load_payload()
    phase = sys.argv[1] if len(sys.argv) > 1 else payload.get("hook_event_name", "")
    session_id = session_id_from_payload(payload)
    if not session_id:
        return 0

    if phase == "SessionStart":
        if pr_watch_autoloop_enabled():
            _arm(normalized_cwd(payload), session_id)
        return 0

    if phase != "PostToolUse":
        return 0

    # Normalize first so a Codex exec_command's own workdir wins over the
    # top-level/hook-process cwd when choosing where to arm.
    normalized = normalized_payload(payload)
    if normalized.get("tool_name") != "Bash":
        return 0
    base_cwd = normalized_cwd(normalized)
    tool_input = normalized.get("tool_input") if isinstance(normalized.get("tool_input"), dict) else {}
    command = tool_input.get("command", "")
    if not isinstance(command, str):
        return 0

    # Walk each top-level segment so a `gh pr ...` after a separator (e.g.
    # `git push && gh pr create`) is still detected, and a preceding `cd`
    # relocates the effective arm cwd. Honor shell conditionals: a `||`-guarded
    # segment only runs on the previous command's failure, and a `&&`-guarded
    # one only on success — don't record ownership for a PR action the shell
    # never executed.
    autoloop = pr_watch_autoloop_enabled()
    failed = _command_failed(payload)
    eff_cwd = base_cwd
    for op, segment in split_command_segments(command):
        destination = cd_target(segment)
        if destination is not None:
            # Expand ~ / ~user first: `cd ~/wt` runs gh from the home-relative
            # worktree, not `<old cwd>/~/wt`.
            destination = os.path.expanduser(destination)
            eff_cwd = (
                destination
                if os.path.isabs(destination)
                else str((Path(eff_cwd) / destination).resolve())
            )
            continue
        if op == "||":
            continue  # only-on-failure guard: the PR action wasn't intended
        if op == "&&" and failed:
            continue  # prior command failed → this segment never ran
        target = parse_gh_pr_arm_target(segment)
        if target is not None:
            pr_number, branch = target
            _arm(eff_cwd, session_id, pr=pr_number, branch=branch)
            continue
        if autoloop and is_git_push(segment):
            _arm(effective_git_cwd(segment, eff_cwd), session_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
