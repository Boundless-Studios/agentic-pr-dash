"""Completion-phase helpers — thread resolution and completion reply."""
from __future__ import annotations

import re


# A token that looks like a Python module path or a concrete source filename.
_FILE_REF_RE = re.compile(
    r"""
    (?:
        # Backtick/quote-wrapped or bare path with a source extension.
        (?P<path>[\w./-]+\.(?:py|pyi|ts|tsx|js|jsx|gd|go|rs|java|kt|rb|c|h|cpp|hpp|sql|sh))
      |
        # Dotted module path with >=2 segments, e.g. gaia.api.worker_app.
        (?P<module>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*){1,})
    )
    """,
    re.VERBOSE,
)

# Module-ish tokens that are almost always prose, not real module references.
_MODULE_REF_STOPWORDS = frozenset({"e.g", "i.e", "etc"})


def _commit_subject(message: str) -> str:
    """First non-empty line of a commit message, trimmed."""
    for line in message.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return message.strip()


def _completion_reply_body(
    marker: str,
    path: str | None,
    commits_by_file: dict[str, list[tuple[str, str]]],
    all_commits: list[tuple[str, str]],
) -> str:
    """Build a substantive completion reply that cites the fixing commit(s)."""
    cites = commits_by_file.get(path, []) if path else []
    if not cites:
        cites = all_commits
    seen: set[str] = set()
    unique = [(sha, msg) for sha, msg in cites if not (sha in seen or seen.add(sha))]
    lines = [marker]
    if len(unique) == 1:
        sha, msg = unique[0]
        lines.append(f"Addressed by the local maintenance loop in {sha[:9]} — {_commit_subject(msg)}.")
    elif unique:
        lines.append("Addressed by the local maintenance loop in:")
        lines.extend(f"- `{sha[:9]}` {_commit_subject(msg)}" for sha, msg in unique[:6])
    else:
        lines.append("Addressed by the local maintenance loop.")
    return "\n".join(lines)


def _mark_maintenance_complete(maintenance, cwd: str, pr_number: int) -> None:  # type: ignore[no-untyped-def]
    """Best-effort: write COMPLETE to the on-disk maintenance state."""
    try:
        from agentic_pr_dash.models import MaintenanceStatus  # noqa: PLC0415

        state = maintenance.load_state(cwd, pr_number)
        if state is not None:
            maintenance.mark_state(state, MaintenanceStatus.COMPLETE)
    except Exception:  # noqa: BLE001
        pass


def _review_comments_from_threads(threads) -> list:
    """Build ReviewComment records from review threads."""
    from agentic_pr_dash.models import ReviewComment  # noqa: PLC0415

    out = []
    for t in threads:
        top = t.top
        out.append(ReviewComment(
            id=top.database_id,
            author=top.author,
            body=top.body,
            path=top.path,
            line=top.line,
            created_at=top.created_at,
            is_inline=True,
            thread_id=t.node_id,
        ))
    return out


def _candidate_file_refs(body: str) -> list[str]:
    """Extract file/module references from a review-thread body."""
    refs: list[str] = []
    for m in _FILE_REF_RE.finditer(body or ""):
        path = m.group("path")
        if path:
            refs.append(path)
            continue
        module = m.group("module")
        if module and module.lower() not in _MODULE_REF_STOPWORDS:
            refs.append(module)
    return refs


def _ref_matches_touched(ref: str, touched: set[str]) -> bool:
    """True if a body file/module reference plausibly matches a touched path."""
    if not touched:
        return False
    if "/" in ref or ref.endswith(
        (".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".gd", ".go", ".rs",
         ".java", ".kt", ".rb", ".c", ".h", ".cpp", ".hpp", ".sql", ".sh")
    ):
        base = ref.rsplit("/", 1)[-1]
        for t in touched:
            if t == ref or t.endswith("/" + ref) or t.rsplit("/", 1)[-1] == base:
                return True
        return False
    segments = ref.split(".")
    frag = "/".join(segments)
    last = segments[-1]
    for t in touched:
        no_ext = t.rsplit(".", 1)[0]
        if frag in no_ext:
            return True
        if no_ext.rsplit("/", 1)[-1] == last:
            return True
    return False


def _thread_elsewhere_refs(body: str, anchor_path: str | None,
                           touched: set[str]) -> list[str]:
    """Body file/module refs that point at something NOT among ``touched``.

    Returns the concrete references (in body order, de-duplicated) that neither
    match a touched path nor resolve to the thread's own anchor. An empty list
    means the body points only at touched files / the anchor — i.e. it does not
    point elsewhere. Surfacing the refs lets the caller name the specific
    conflicting paths when it declines to auto-resolve (BOU-1748).
    """
    anchor_base = anchor_path.rsplit("/", 1)[-1] if anchor_path else None
    elsewhere: list[str] = []
    for ref in _candidate_file_refs(body):
        if _ref_matches_touched(ref, touched):
            continue
        if anchor_base:
            ref_base = ref.rsplit("/", 1)[-1]
            if ref_base == anchor_base:
                continue
            if "." in ref and not ref_base.endswith(
                tuple(f".{ext}" for ext in
                      ("py", "pyi", "ts", "tsx", "js", "jsx", "gd", "go",
                       "rs", "java", "kt", "rb", "c", "h", "cpp", "hpp",
                       "sql", "sh"))
            ):
                if ref.split(".")[-1] == anchor_base.rsplit(".", 1)[0]:
                    continue
        if ref not in elsewhere:
            elsewhere.append(ref)
    return elsewhere


def _thread_points_elsewhere(body: str, anchor_path: str | None,
                             touched: set[str]) -> bool:
    """True if the thread body references a file/module NOT among ``touched``."""
    return bool(_thread_elsewhere_refs(body, anchor_path, touched))
