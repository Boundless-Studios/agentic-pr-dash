from argparse import Namespace

from agentic_pr_dash import maintenance_check


def _args(tmp_path):
    return Namespace(cwd=str(tmp_path), max_wait=1800.0, poll_interval=30.0)


def test_pending_then_red_never_reports_settled(monkeypatch, tmp_path):
    outcomes = iter([10, 10])
    monkeypatch.setattr(maintenance_check, "_cmd_check", lambda _args: next(outcomes))
    monkeypatch.setattr(maintenance_check.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(maintenance_check.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(maintenance_check, "_foreground_deadline_reached", lambda *_: True)
    assert maintenance_check._cmd_monitor(_args(tmp_path)) == 10


def test_pending_then_green_requires_two_clean_observations(monkeypatch, tmp_path):
    outcomes = iter([10, 0, 0])
    calls = []
    monkeypatch.setattr(maintenance_check, "_cmd_check", lambda _args: calls.append(1) or next(outcomes))
    monkeypatch.setattr(maintenance_check.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(maintenance_check, "_foreground_deadline_reached", lambda *_: False)
    assert maintenance_check._cmd_monitor(_args(tmp_path)) == 0
    assert len(calls) == 3


def test_exit_while_pending_releases_live_ownership(monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance_check, "_cmd_check", lambda _args: 10)
    monkeypatch.setattr(maintenance_check, "_foreground_deadline_reached", lambda *_: True)
    released = []
    monkeypatch.setattr(maintenance_check, "_release_foreground_ownership",
                        lambda args: released.append(args.cwd))
    assert maintenance_check._cmd_monitor(_args(tmp_path)) == 10
    assert released == [str(tmp_path)]
