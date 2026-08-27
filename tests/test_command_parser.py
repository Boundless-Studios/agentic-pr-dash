"""Focused unit tests for the extracted Codex hook command parser.

These exercise the pure parsing helpers directly (no hook payload / I/O), as a
companion to the end-to-end hook tests in test_codex_hooks.py.
"""

from __future__ import annotations

from agentic_pr_dash.codex_hooks.command_parser import (
    cd_target,
    effective_git_cwd,
    git_push_source_branch,
    is_gh_pr_open,
    is_git_push,
    is_git_token,
    parse_gh_pr_arm_target,
    split_command_segments,
)

# --- is_git_token -----------------------------------------------------------

def test_is_git_token_matches_by_basename():
    """BOU-2147: bare and path-qualified git both match; lookalikes don't."""
    assert is_git_token("git") is True
    assert is_git_token("/usr/bin/git") is True
    assert is_git_token("/opt/homebrew/bin/git") is True
    assert is_git_token("../bin/git") is True
    assert is_git_token("bin/git") is True
    assert is_git_token("mygit") is False
    assert is_git_token("gitx") is False
    assert is_git_token("/usr/bin/gitx") is False
    assert is_git_token("git/") is False  # a directory, not an executable
    assert is_git_token("") is False


# --- split_command_segments -------------------------------------------------

def test_split_respects_operators_and_records_leading_op():
    assert split_command_segments("git push && gh pr create --fill") == [
        ("", "git push"),
        ("&&", "gh pr create --fill"),
    ]


def test_split_handles_or_pipe_semicolon_and_newline():
    assert split_command_segments("a || b ; c | d\ne") == [
        ("", "a"),
        ("||", "b"),
        (";", "c"),
        ("|", "d"),
        (";", "e"),
    ]


def test_split_does_not_break_inside_quotes_or_escapes():
    # The '&&' inside the quoted title must not split the segment.
    assert split_command_segments("gh pr create --title 'a && b'") == [
        ("", "gh pr create --title 'a && b'"),
    ]
    assert split_command_segments(r"echo a\&\& b") == [("", r"echo a\&\& b")]


# --- cd_target --------------------------------------------------------------

def test_cd_target_returns_explicit_dir_and_skips_flags_and_prefixes():
    assert cd_target("cd ../wt") == "../wt"
    assert cd_target("env FOO=1 cd /tmp/x") == "/tmp/x"
    assert cd_target("cd -P /tmp/y") == "/tmp/y"


def test_cd_target_bare_cd_or_non_cd_is_none():
    assert cd_target("cd") is None
    assert cd_target("git push") is None
    assert cd_target("gh pr ready") is None


# --- parse_gh_pr_arm_target / is_gh_pr_open ---------------------------------

def test_parse_create_plain_and_with_head():
    assert parse_gh_pr_arm_target("gh pr create --fill") == (None, None)
    assert parse_gh_pr_arm_target("gh pr create --head feature/x") == (None, "feature/x")
    assert parse_gh_pr_arm_target("gh pr create -Hfeature/y") == (None, "feature/y")
    assert parse_gh_pr_arm_target("gh pr new --head z") == (None, "z")


def test_parse_ready_number_branch_and_url():
    assert parse_gh_pr_arm_target("gh pr ready 123") == ("123", None)
    # A quoted '#45' survives shlex (a bare #45 would be a comment) and the
    # leading '#' is stripped to the PR number.
    assert parse_gh_pr_arm_target("gh pr ready '#45'") == ("45", None)
    assert parse_gh_pr_arm_target("gh pr ready") == (None, None)
    assert parse_gh_pr_arm_target("gh pr ready my-branch") == (None, "my-branch")
    # A pull URL may point at another repo — not armable from here.
    assert parse_gh_pr_arm_target("gh pr ready https://github.com/o/r/pull/9") is None


def test_parse_explicit_repo_and_non_gh_are_not_armable():
    assert parse_gh_pr_arm_target("gh pr ready 1 --repo other/repo") is None
    assert parse_gh_pr_arm_target("gh pr create -R other/repo") is None
    assert parse_gh_pr_arm_target("git push") is None
    assert parse_gh_pr_arm_target("gh issue list") is None


def test_is_gh_pr_open_mirrors_parse():
    assert is_gh_pr_open("gh pr create --fill") is True
    assert is_gh_pr_open("gh pr ready --repo other/repo") is False


# --- is_git_push ------------------------------------------------------------

def test_is_git_push_detects_push_through_global_flags():
    assert is_git_push("git push") is True
    assert is_git_push("git -C /tmp/wt push origin HEAD") is True
    assert is_git_push("/usr/bin/git push") is True
    assert is_git_push("env X=1 git push") is True


def test_is_git_push_false_for_non_push_or_non_git():
    assert is_git_push("git status") is False
    assert is_git_push("gh pr create") is False
    assert is_git_push("") is False
    assert is_git_push("/usr/bin/git status") is False
    assert is_git_push("mygit push") is False


def test_is_git_push_detects_homebrew_path_git():
    assert is_git_push("/opt/homebrew/bin/git push origin HEAD") is True


def test_push_source_branch_consumes_push_option_values():
    assert git_push_source_branch("git push -o ci.skip origin feature") == (
        True,
        "feature",
    )
    assert git_push_source_branch(
        "git push --push-option=ci.skip origin feature"
    ) == (True, "feature")
    assert git_push_source_branch("git push -oci.skip origin feature") == (
        True,
        "feature",
    )


# --- effective_git_cwd ------------------------------------------------------

def test_effective_git_cwd_applies_dash_C_relative_to_base():
    assert effective_git_cwd("git -C sub push", "/base") == "/base/sub"
    assert effective_git_cwd("git -Csub push", "/base") == "/base/sub"


def test_effective_git_cwd_honors_work_tree_and_env():
    assert effective_git_cwd("git --work-tree /wt push", "/base") == "/wt"
    assert effective_git_cwd("GIT_WORK_TREE=/wt git push", "/base") == "/wt"


def test_effective_git_cwd_defaults_to_base_when_no_relocation():
    assert effective_git_cwd("git push", "/base") == "/base"
    assert effective_git_cwd("not-git", "/base") == "/base"
