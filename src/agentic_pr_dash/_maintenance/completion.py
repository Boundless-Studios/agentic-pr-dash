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
        (?P<module>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*){1,})(?![\w(])
    )
    """,
    re.VERBOSE,
)

# Module-ish tokens that are almost always prose, not real module references.
_MODULE_REF_STOPWORDS = frozenset({"e.g", "i.e", "etc"})

# BOU-2095: how far (in lines) a changed hunk may sit from a thread's anchored
# line and still count as evidence that the comment's subject was edited. Three
# lines matches the diff context GitHub shows around an inline comment.
_ANCHOR_CONTEXT_LINES = 3


def _spans_intersect_line(
    spans: list[tuple[int, int, int, int]],
    anchor_line: int,
    side: str,
) -> bool:
    """True if a changed hunk touches ``anchor_line`` (± context fuzz) on ``side``.

    ``spans`` come from :func:`agentic_pr_dash.github_api.get_changed_line_spans`
    — ``(old_start, old_end, new_start, new_end)`` per hunk, where an empty side
    is encoded as ``end < start``. ``side`` names the diff side the anchor
    coordinate lives on: a non-outdated thread's ``line`` is head-side
    (``"new"``); an outdated thread's ``original_line`` is base-side
    (``"old"``). Comparing a coordinate against the WRONG side let unrelated
    hunks masquerade as evidence — e.g. a deletion above a live thread whose
    old-side numbers happen to overlap the head-side anchor (PR #78 review).
    """
    for old_start, old_end, new_start, new_end in spans:
        start, end = (
            (new_start, new_end) if side == "new" else (old_start, old_end)
        )
        if end < start:  # empty side: pure insertion/deletion counterpart
            continue
        if start - _ANCHOR_CONTEXT_LINES <= anchor_line <= end + _ANCHOR_CONTEXT_LINES:
            return True
    return False


def _thread_completion_evidence(
    thread,  # type: ignore[no-untyped-def]
    spans: list[tuple[int, int, int, int]] | None,
    fixing_date: str | None = None,
) -> str | None:
    """Positive per-thread evidence that the completing commits addressed THIS thread.

    BOU-2095: "the anchor file was touched" / "GitHub marks the thread
    outdated" is NOT evidence — line drift and unrelated same-file edits were
    silently resolving live feedback. Returns the evidence kind, or ``None``
    when there is none (caller must leave the thread OPEN):

    - ``"reply"``  — a completion-marker reply is the thread's TERMINAL state
      per :func:`agentic_pr_dash.github_api._thread_state` (an earlier
      `complete` run replied but the resolve mutation failed). A marker
      followed by a human follow-up is ``reopened`` — the reviewer rejected
      the completion — so a merely-historical marker is NOT evidence
      (PR #78 review);
    - ``"hunk"``   — the completing commits changed content at the thread's
      anchored line (± :data:`_ANCHOR_CONTEXT_LINES`), compared on the diff
      side the coordinate lives on (head-side ``line`` vs base-side
      ``original_line``);
    - ``"file"``   — the thread is file-level (no line anchor at all) and the
      file's content changed; the whole file IS the anchor span.

    A reopened thread additionally requires ``fixing_date`` to be newer than
    its latest human follow-up, so an old hunk cannot re-complete rejected work.

    ``spans is None`` means the base..head diff was unavailable — that is
    absence of proof, never proof of absence, so it yields ``None``.
    """
    from agentic_pr_dash._maintenance._common import _parse_iso  # noqa: PLC0415
    from agentic_pr_dash.github_api import (  # noqa: PLC0415
        CLAIM_MARKER,
        COMPLETE_MARKER,
        FAILED_MARKER,
        _thread_state,
    )

    replies_as_dicts = [
        {"body": r.body, "created_at": r.created_at, "author": r.author}
        for r in thread.replies
    ]
    state, _ = _thread_state(replies_as_dicts, top_author=thread.top.author)
    if state == "completed":
        return "reply"
    if state == "reopened":
        # A marker followed by fresh human feedback invalidates the old fixing
        # evidence. The same base..HEAD hunk may still intersect the anchor,
        # but it cannot address feedback newer than HEAD. Fail closed unless a
        # later fixing commit is established.
        human_followups = [
            _parse_iso(r.created_at)
            for r in thread.replies
            if not any(marker in r.body for marker in (
                CLAIM_MARKER, COMPLETE_MARKER, FAILED_MARKER))
        ]
        human_followups = [stamp for stamp in human_followups if stamp is not None]
        fixed_at = _parse_iso(fixing_date or "")
        if not human_followups or fixed_at is None or fixed_at <= max(human_followups):
            return None
    anchor_line = thread.top.line
    anchor_side = "new"  # non-outdated `line` is a head-side coordinate
    if anchor_line is None:
        anchor_line = thread.top.original_line
        anchor_side = "old"  # `original_line` is a base-side coordinate
    if anchor_line is None:
        return "file" if spans else None
    if spans and _spans_intersect_line(spans, anchor_line, anchor_side):
        return "hunk"
    return None


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
    body_without_urls = re.sub(r"(?:https?:)?//[^\s)>]+", "", body or "")
    for m in _FILE_REF_RE.finditer(body_without_urls):
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
