"""BOU-1479: complete must resolve threads off the LOCAL git range so it doesn't
race the GitHub API's indexing lag right after a push."""

import subprocess

from agentic_pr_dash import github_api


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _make_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("a\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    return base


def test_local_new_commits_after_baseline(tmp_path):
    base = _make_repo(tmp_path)
    (tmp_path / "fix.py").write_text("fixed\n")
    _git(tmp_path, "add", "fix.py")
    _git(tmp_path, "commit", "-q", "-m", "fix the bug")

    commits = github_api.get_new_pr_commits(0, base, "", str(tmp_path))
    assert len(commits) == 1
    sha, msg = commits[0]
    assert msg == "fix the bug"
    assert len(sha) >= 7


def test_local_changed_files(tmp_path):
    _make_repo(tmp_path)
    (tmp_path / "src.py").write_text("x\n")
    _git(tmp_path, "add", "src.py")
    _git(tmp_path, "commit", "-q", "-m", "add src")
    head = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()

    files = github_api.get_commit_changed_files(head, str(tmp_path))
    assert "src.py" in files


def test_no_baseline_returns_empty_local(tmp_path):
    _make_repo(tmp_path)
    # No baseline -> local path returns [] (caller falls back to the API).
    from agentic_pr_dash.github_api import _local_new_commits
    assert _local_new_commits("", str(tmp_path)) == []


def test_scoped_to_pr_branch_ignores_unpushed_head_commits(tmp_path):
    # The fix is pushed (origin/feature points at it); an UNRELATED commit then
    # lands on HEAD. Scoping to the PR branch must return only the pushed fix.
    base = _make_repo(tmp_path)
    (tmp_path / "fix.py").write_text("fix\n")
    _git(tmp_path, "add", "fix.py")
    _git(tmp_path, "commit", "-q", "-m", "fix")
    head = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    _git(tmp_path, "update-ref", "refs/remotes/origin/feature", head)
    (tmp_path / "unrelated.py").write_text("x\n")
    _git(tmp_path, "add", "unrelated.py")
    _git(tmp_path, "commit", "-q", "-m", "unrelated unpushed")

    msgs = [m for _, m in github_api.get_new_pr_commits(0, base, "", str(tmp_path), pr_branch="feature")]
    assert msgs == ["fix"]  # NOT the unrelated unpushed commit


def test_get_local_pr_head(tmp_path):
    base = _make_repo(tmp_path)
    _git(tmp_path, "update-ref", "refs/remotes/origin/feature", base)
    sha, date = github_api.get_local_pr_head("feature", str(tmp_path))
    assert sha and "T" in date  # ISO committer date
    assert github_api.get_local_pr_head("nonexistent", str(tmp_path)) == ("", "")


def test_local_head_date_normalized_to_utc(tmp_path):
    # A committer date with a non-UTC offset must come back as a UTC `...Z`
    # stamp, else it sorts wrongly against GitHub's `...Z` createdAt strings.
    _make_repo(tmp_path)
    (tmp_path / "f.txt").write_text("x\n")
    _git(tmp_path, "add", "f.txt")
    env = {"GIT_COMMITTER_DATE": "2026-06-09T05:00:00-07:00",
           "GIT_AUTHOR_DATE": "2026-06-09T05:00:00-07:00"}
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "tz commit"],
                   check=True, capture_output=True, text=True,
                   env={**__import__("os").environ, **env})
    head = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    _git(tmp_path, "update-ref", "refs/remotes/origin/feature", head)
    _, date = github_api.get_local_pr_head("feature", str(tmp_path))
    assert date == "2026-06-09T12:00:00Z"  # 05:00 -07:00 == 12:00 UTC


def test_rebased_baseline_not_ancestor_falls_back(tmp_path):
    # After a force-push the saved baseline no longer sits on the branch tip;
    # `baseline..tip` would replay unrelated history, so the local range is
    # rejected (caller falls back to the API).
    base = _make_repo(tmp_path)
    (tmp_path / "old.py").write_text("old\n")
    _git(tmp_path, "add", "old.py")
    _git(tmp_path, "commit", "-q", "-m", "soon-orphaned baseline")
    orphaned = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    # Rewrite history off `base`: a brand-new tip that does NOT contain `orphaned`.
    _git(tmp_path, "reset", "-q", "--hard", base)
    (tmp_path / "new.py").write_text("new\n")
    _git(tmp_path, "add", "new.py")
    _git(tmp_path, "commit", "-q", "-m", "replayed tip")
    tip = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    _git(tmp_path, "update-ref", "refs/remotes/origin/feature", tip)
    assert github_api._local_new_commits(orphaned, str(tmp_path), "origin/feature") == []


def test_stale_local_ref_falls_back(tmp_path):
    # origin/feature is BEHIND the API's known head (F2 was pushed elsewhere and
    # not fetched here). With F2 named as must_contain_sha the stale local range
    # is rejected; without it the range is (wrongly) trusted.
    base = _make_repo(tmp_path)
    (tmp_path / "f1.py").write_text("f1\n")
    _git(tmp_path, "add", "f1.py")
    _git(tmp_path, "commit", "-q", "-m", "f1")
    f1 = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                        capture_output=True, text=True).stdout.strip()
    _git(tmp_path, "update-ref", "refs/remotes/origin/feature", f1)  # stale ref stops here
    (tmp_path / "f2.py").write_text("f2\n")
    _git(tmp_path, "add", "f2.py")
    _git(tmp_path, "commit", "-q", "-m", "f2")
    f2 = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                        capture_output=True, text=True).stdout.strip()
    # API head (f2) not contained in origin/feature -> stale -> reject local range.
    assert github_api._local_new_commits(base, str(tmp_path), "origin/feature",
                                         must_contain_sha=f2) == []
    # When the API head IS contained (the normal fresh case), the range is used.
    msgs = [m for _, m in github_api._local_new_commits(
        base, str(tmp_path), "origin/feature", must_contain_sha=f1)]
    assert msgs == ["f1"]
