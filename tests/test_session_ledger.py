from agentic_pr_dash import session_ledger as sl


def test_append_and_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path))
    sl.append("sess-A", pr=101, branch="bou-1-x", worktree="/wt/1", baseline_sha="abc")
    entries = sl.read("sess-A")
    assert len(entries) == 1
    e = entries[0]
    assert e.pr == 101 and e.branch == "bou-1-x" and e.worktree == "/wt/1"
    assert e.baseline_sha == "abc" and e.opened_at  # iso timestamp present


def test_append_is_idempotent_on_pr(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path))
    sl.append("sess-A", pr=101, branch="old", worktree="/wt/old")
    sl.append("sess-A", pr=101, branch="new", worktree="/wt/new")  # re-arm, last wins
    entries = sl.read("sess-A")
    assert len(entries) == 1
    assert entries[0].branch == "new" and entries[0].worktree == "/wt/new"


def test_read_tolerates_malformed_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path))
    sl.append("sess-A", pr=1, branch="b", worktree="/wt")
    path = sl.ledger_path("sess-A")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{ not json\n")
    entries = sl.read("sess-A")
    assert [e.pr for e in entries] == [1]  # malformed line skipped


def test_prune_drops_given_prs(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path))
    sl.append("sess-A", pr=1, branch="b1", worktree="/wt1")
    sl.append("sess-A", pr=2, branch="b2", worktree="/wt2")
    sl.prune("sess-A", {1})
    assert [e.pr for e in sl.read("sess-A")] == [2]


def test_path_is_sanitized_and_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path))
    p = sl.ledger_path("weird/../id with spaces")
    assert str(tmp_path) in p
    assert "/" not in p[len(str(tmp_path)) + 1:]  # filename has no path separators


def test_list_session_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path))
    sl.append("sess-A", pr=1, branch="b", worktree="/wt")
    sl.append("sess-B", pr=2, branch="b", worktree="/wt")
    assert set(sl.list_session_ids()) == {"sess-A", "sess-B"}


# --- round-2 #2: ledger entries are scoped by repository --------------------

def test_same_pr_in_two_repos_kept_separately(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path))
    sl.append("sess-A", pr=12, branch="a", worktree="/wt/a", repo="org/repoA")
    sl.append("sess-A", pr=12, branch="b", worktree="/wt/b", repo="org/repoB")
    entries = sl.read("sess-A")
    assert len(entries) == 2  # second did NOT overwrite the first
    by_repo = {e.repo: e for e in entries}
    assert by_repo["org/repoA"].worktree == "/wt/a"
    assert by_repo["org/repoB"].worktree == "/wt/b"


def test_read_filters_by_repo_keeping_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path))
    sl.append("sess-A", pr=12, branch="a", worktree="/wt/a", repo="org/repoA")
    sl.append("sess-A", pr=13, branch="b", worktree="/wt/b", repo="org/repoB")
    sl.append("sess-A", pr=14, branch="c", worktree="/wt/c")  # legacy, no repo
    got = sl.read("sess-A", repo="org/repoA")
    prs = {e.pr for e in got}
    assert prs == {12, 14}  # repoA PR + legacy repo-less PR; repoB excluded


def test_prune_is_repo_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path))
    sl.append("sess-A", pr=12, branch="a", worktree="/wt/a", repo="org/repoA")
    sl.append("sess-A", pr=12, branch="b", worktree="/wt/b", repo="org/repoB")
    sl.prune("sess-A", {12}, repo="org/repoA")
    remaining = sl.read("sess-A")
    assert len(remaining) == 1 and remaining[0].repo == "org/repoB"  # repoB untouched


def test_claim_path_is_repo_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_CLAIM_DIR", str(tmp_path))
    pa = sl.claim_path(12, "org/repoA")
    pb = sl.claim_path(12, "org/repoB")
    legacy = sl.claim_path(12)
    assert pa != pb != legacy and pa != legacy
    assert legacy.endswith("pr-12.json")  # backward-compatible legacy name


def test_read_tolerates_legacy_entries_without_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path))
    path = sl.ledger_path("sess-A")
    import os, json
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:  # pre-repo-scoping line
        fh.write(json.dumps({"pr": 9, "branch": "b", "worktree": "/wt"}) + "\n")
    entries = sl.read("sess-A")
    assert len(entries) == 1 and entries[0].pr == 9 and entries[0].repo == ""
