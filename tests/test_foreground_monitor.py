from argparse import Namespace
from types import SimpleNamespace

from agentic_pr_dash import maintenance_check


def _args(tmp_path):
    return Namespace(cwd=str(tmp_path), pr=42, session_id="sess", max_wait=1800.0,
                     poll_interval=30.0, policy=None, ledger=None)


def test_pending_then_red_never_reports_settled(monkeypatch, tmp_path):
    outcomes = iter([11, 10])
    monkeypatch.setattr(maintenance_check, "_monitor_observation", lambda _args: (next(outcomes), "head"))
    monkeypatch.setattr(maintenance_check.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(maintenance_check.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(maintenance_check, "_foreground_deadline_reached", lambda *_: False)
    assert maintenance_check._cmd_monitor(_args(tmp_path)) == 10


def test_pending_then_green_requires_two_clean_observations(monkeypatch, tmp_path):
    outcomes = iter([11, 0, 0])
    calls = []
    monkeypatch.setattr(maintenance_check, "_monitor_observation", lambda _args: (calls.append(1) or next(outcomes), "head"))
    monkeypatch.setattr(maintenance_check.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(maintenance_check, "_foreground_deadline_reached", lambda *_: False)
    args = _args(tmp_path)
    args.poll_interval = 0
    assert maintenance_check._cmd_monitor(args) == 0
    assert len(calls) == 3


def test_exit_while_pending_releases_live_ownership(monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance_check, "_monitor_observation", lambda _args: (10, "head"))
    monkeypatch.setattr(maintenance_check, "_foreground_deadline_reached", lambda *_: True)
    released = []
    monkeypatch.setattr(maintenance_check, "_release_foreground_ownership",
                        lambda args: released.append(args.cwd))
    assert maintenance_check._cmd_monitor(_args(tmp_path)) == 10
    assert released == [str(tmp_path)]


def test_new_push_resets_clean_observation_count(monkeypatch, tmp_path):
    observations = iter([(0, "old-head"), (0, "new-head"), (0, "new-head")])
    monkeypatch.setattr(maintenance_check, "_monitor_observation", lambda _args: next(observations))
    monkeypatch.setattr(maintenance_check.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(maintenance_check, "_foreground_deadline_reached", lambda *_: False)
    args = _args(tmp_path)
    args.poll_interval = 0
    assert maintenance_check._cmd_monitor(args) == 0


def test_missing_backstop_is_watch_pending_not_actionable(monkeypatch, tmp_path):
    snapshot = SimpleNamespace(
        clean=False,
        blockers=[],
        head_sha="head",
        review=SimpleNamespace(required_actions=[], missing_slots=["backstop:1"]),
    )
    monkeypatch.setattr(maintenance_check, "_resolve_pr_by_number", lambda *_args, **_kwargs:
                        SimpleNamespace(is_draft=False, latest_commit_sha="head", repo="owner/repo"))
    monkeypatch.setattr(maintenance_check, "_observe_finalization", lambda *_args: snapshot)
    monkeypatch.setattr("agent_review_coordinator.ReviewPolicy.from_yaml", lambda _text: object())
    monkeypatch.setattr("agent_review_coordinator.ReviewLedger.model_validate_json", lambda _text: object())
    policy = tmp_path / "policy.yaml"
    ledger = tmp_path / "ledger.json"
    policy.write_text("version: 1")
    ledger.write_text("{}")
    args = _args(tmp_path)
    args.policy = str(policy)
    args.ledger = str(ledger)
    assert maintenance_check._monitor_observation(args) == (11, "head")
