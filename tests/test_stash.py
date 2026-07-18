"""Race-safe labeled-stash subcommand (BOU-2155, ported from gaia BOU-2031).

Every test runs inside a THROWAWAY git repo created under pytest's tmp_path —
never against any real repo's (shared, cross-worktree) stash. The concurrent
stack-shift scenarios are simulated by wrapping ``stash._git`` so an
interloper ``git stash push`` runs between label resolution and the
apply/drop action, exactly the window the wrapper must survive.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentic_pr_dash import cli, stash


# ---------------------------------------------------------------------------
# Fixture: throwaway git repo
# ---------------------------------------------------------------------------


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Init a throwaway repo with identity config and one base commit."""
    r = tmp_path / "fixture-repo"
    r.mkdir()
    _run_git(r, "init", "-q")
    _run_git(r, "config", "user.email", "t@t")
    _run_git(r, "config", "user.name", "t")
    _run_git(r, "commit", "-q", "--allow-empty", "-m", "init")
    return r


def _make_stash(repo: Path, filename: str, label: str) -> None:
    (repo / filename).write_text(f"content of {filename}\n")
    _run_git(repo, "add", filename)
    _run_git(repo, "stash", "push", "-q", "-m", label)


def _stash_list(repo: Path) -> str:
    return _run_git(repo, "stash", "list").stdout


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


def test_push_requires_message_flag(repo: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        stash.main(["push", "--cwd", str(repo)])
    assert exc.value.code == 2


def test_push_rejects_empty_or_blank_message(repo: Path) -> None:
    (repo / "f.txt").write_text("x\n")
    _run_git(repo, "add", "f.txt")
    assert stash.main(["push", "-m", "", "--cwd", str(repo)]) == 2
    assert stash.main(["push", "-m", "   ", "--cwd", str(repo)]) == 2
    assert _stash_list(repo) == ""  # nothing was stashed


def test_push_creates_labeled_entry(repo: Path) -> None:
    (repo / "f.txt").write_text("x\n")
    _run_git(repo, "add", "f.txt")
    assert stash.main(["push", "-m", "test-branch: wip a", "--cwd", str(repo)]) == 0
    assert "test-branch: wip a" in _stash_list(repo)
    assert not (repo / "f.txt").exists()


def test_push_include_untracked(repo: Path) -> None:
    (repo / "untracked.txt").write_text("x\n")
    assert stash.main(["push", "-u", "-m", "test-branch: wip u", "--cwd", str(repo)]) == 0
    assert "test-branch: wip u" in _stash_list(repo)
    assert not (repo / "untracked.txt").exists()


# ---------------------------------------------------------------------------
# apply / drop happy paths (non-top entry — index resolution must disambiguate)
# ---------------------------------------------------------------------------


def test_apply_by_unique_label_resolves_non_top_entry(repo: Path) -> None:
    _make_stash(repo, "f.txt", "test-branch: wip a")
    _make_stash(repo, "g.txt", "test-branch: wip b")  # "wip a" is now stash@{1}
    assert stash.main(["apply", "wip a", "--cwd", str(repo)]) == 0
    assert (repo / "f.txt").exists(), "apply restored the wrong entry (f.txt missing)"
    assert not (repo / "g.txt").exists(), "apply restored the wrong entry (g.txt present)"


def test_drop_by_unique_label_removes_exactly_that_entry(repo: Path) -> None:
    _make_stash(repo, "f.txt", "test-branch: wip a")
    _make_stash(repo, "g.txt", "test-branch: wip b")
    assert stash.main(["drop", "wip a", "--cwd", str(repo)]) == 0
    listing = _stash_list(repo)
    assert "wip a" not in listing, "dropped entry still listed"
    assert "wip b" in listing, "drop removed the wrong entry"


def test_list_prints_entries(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _make_stash(repo, "f.txt", "test-branch: wip a")
    assert stash.main(["list", "--cwd", str(repo)]) == 0
    assert "test-branch: wip a" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# fail-closed: zero / ambiguous label matches
# ---------------------------------------------------------------------------


def test_unknown_label_fails_closed(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _make_stash(repo, "f.txt", "test-branch: wip a")
    assert stash.main(["drop", "no-such-label", "--cwd", str(repo)]) == 1
    assert "no stash entry matches" in capsys.readouterr().err
    assert "wip a" in _stash_list(repo)  # stash untouched


def test_ambiguous_label_fails_closed_and_leaves_stash_untouched(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    _make_stash(repo, "f.txt", "test-branch: wip a")
    _make_stash(repo, "g.txt", "test-branch: wip b")
    assert stash.main(["apply", "test-branch", "--cwd", str(repo)]) == 1
    assert "ambiguous" in capsys.readouterr().err
    assert len(_stash_list(repo).splitlines()) == 2
    assert not (repo / "f.txt").exists()
    assert not (repo / "g.txt").exists()


def test_label_never_matches_hash_hex(repo: Path) -> None:
    """The label is matched against the entry text only, never the hash."""
    _make_stash(repo, "f.txt", "test-branch: wip a")
    out = _run_git(repo, "stash", "list", "--format=%H").stdout.strip()
    hash_prefix = out[:12]
    assert stash.main(["apply", hash_prefix, "--cwd", str(repo)]) == 1


# ---------------------------------------------------------------------------
# concurrent-shift (TOCTOU) simulations
# ---------------------------------------------------------------------------


def _install_shift_interloper(monkeypatch: pytest.MonkeyPatch, repo: Path, before: str) -> None:
    """Wrap stash._git: push an interloper stash right before the first git
    call whose subcommand starts with *before* — simulating another worktree
    shifting every stash@{n} index between label resolution and the action."""
    real_git = stash._git
    fired = {"done": False}

    def shifting_git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
        if not fired["done"] and args[: len(before.split())] == before.split():
            fired["done"] = True
            (repo / "interloper.txt").write_text("interloper\n")
            _run_git(repo, "add", "interloper.txt")
            _run_git(repo, "stash", "push", "-q", "-m", "concurrent: interloper")
        return real_git(args, cwd)

    monkeypatch.setattr(stash, "_git", shifting_git)


def test_apply_by_hash_survives_index_shift(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stack shift between resolution and apply must still apply the right
    entry — apply acts on the pinned commit hash, not the shifted index."""
    _make_stash(repo, "f.txt", "test-branch: wip a")
    _install_shift_interloper(monkeypatch, repo, before="stash apply")

    assert stash.main(["apply", "wip a", "--cwd", str(repo)]) == 0
    assert (repo / "f.txt").exists(), "apply restored the wrong entry after shift"
    assert not (repo / "interloper.txt").exists(), "apply grabbed the interloper's entry"
    # Interloper actually ran (harness sanity).
    assert "concurrent: interloper" in _stash_list(repo)


def test_drop_aborts_on_index_shift(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A stack shift between resolution and the pre-drop re-verify must abort
    loudly, leaving the target entry (and the interloper's) in place."""
    _make_stash(repo, "f.txt", "test-branch: toctou target")
    _install_shift_interloper(monkeypatch, repo, before="rev-parse")

    assert stash.main(["drop", "toctou target", "--cwd", str(repo)]) == 1
    err = capsys.readouterr().err
    assert "ABORT" in err, "TOCTOU abort message missing"

    listing = _stash_list(repo)
    assert "toctou target" in listing, "TOCTOU abort must leave the target entry in place"
    assert "concurrent: interloper" in listing, "interloper push did not run (harness broken)"


# ---------------------------------------------------------------------------
# CLI dispatch + argparse guards
# ---------------------------------------------------------------------------


def test_cli_routes_stash_to_stash_main(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(stash, "main", fake_main)
    assert cli.main(["stash", "list", "--cwd", "/tmp"]) == 0
    assert seen["argv"] == ["list", "--cwd", "/tmp"]


def test_unknown_action_rejected(repo: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        stash.main(["pop", "--cwd", str(repo)])  # no pop, by design
    assert exc.value.code == 2


def test_missing_action_rejected() -> None:
    with pytest.raises(SystemExit) as exc:
        stash.main([])
    assert exc.value.code == 2
