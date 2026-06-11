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
