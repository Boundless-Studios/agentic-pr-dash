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
