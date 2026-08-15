from argparse import Namespace
from types import SimpleNamespace

from agentic_pr_dash import maintenance_check
from agentic_pr_dash.github_api import ObservationReadResult


def _clock(*values):
    remaining = iter(values)
    last = values[-1]

    def now():
        nonlocal last
        try:
            last = next(remaining)
        except StopIteration:
            pass
        return last

    return now


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
    times = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 30.0])
    monkeypatch.setattr(maintenance_check.time, "monotonic", _clock(*times))
    monkeypatch.setattr(maintenance_check, "_foreground_deadline_reached", lambda *_: False)
    args = _args(tmp_path)
    args.poll_interval = 0
    assert maintenance_check._cmd_monitor(args) == 0
    assert len(calls) == 3


def test_fast_poll_accepts_later_clean_observation(monkeypatch, tmp_path):
    observations = iter([(0, "head"), (0, "head"), (0, "head")])
    monkeypatch.setattr(maintenance_check, "_monitor_observation", lambda _args: next(observations))
    monkeypatch.setattr(maintenance_check.time, "sleep", lambda _seconds: None)
    times = iter([0.0, 0.0, 0.0, 10.0, 10.0, 10.0, 10.0, 30.0])
    monkeypatch.setattr(maintenance_check.time, "monotonic", _clock(*times))
    monkeypatch.setattr(maintenance_check, "_foreground_deadline_reached", lambda *_: False)
    args = _args(tmp_path)
    args.poll_interval = 0
    assert maintenance_check._cmd_monitor(args) == 0


def test_actionable_after_early_clean_pair_releases_ownership(monkeypatch, tmp_path):
    observations = iter([(0, "head"), (0, "head"), (10, "head")])
    monkeypatch.setattr(maintenance_check, "_monitor_observation", lambda _args: next(observations))
    monkeypatch.setattr(maintenance_check.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(maintenance_check.time, "monotonic", _clock(0.0, 0.0, 0.0, 10.0, 10.0))
    monkeypatch.setattr(maintenance_check, "_foreground_deadline_reached", lambda *_: False)
    released = []
    monkeypatch.setattr(maintenance_check, "_release_foreground_ownership", lambda args: released.append(args.cwd))
    assert maintenance_check._cmd_monitor(_args(tmp_path)) == 10
    assert released == [str(tmp_path)]


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
    times = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 30.0])
    monkeypatch.setattr(maintenance_check.time, "monotonic", _clock(*times))
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
    monkeypatch.setattr(maintenance_check, "_observe_finalization",
                        lambda *_args, **_kwargs: snapshot)
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


def test_policy_clean_observation_key_includes_full_snapshot(monkeypatch, tmp_path):
    class Snapshot(SimpleNamespace):
        def model_dump_json(self, **_kwargs):
            return self.payload

    snapshots = iter([
        Snapshot(clean=True, head_sha="head", payload='{"checks":["one"]}'),
        Snapshot(clean=True, head_sha="head", payload='{"checks":["one","two"]}'),
    ])
    monkeypatch.setattr(
        maintenance_check,
        "_resolve_pr_by_number",
        lambda *_args, **_kwargs: SimpleNamespace(
            is_draft=False, latest_commit_sha="head", repo="owner/repo"
        ),
    )
    monkeypatch.setattr(
        maintenance_check, "_observe_finalization", lambda *_args, **_kwargs: next(snapshots)
    )
    monkeypatch.setattr("agent_review_coordinator.ReviewPolicy.from_yaml", lambda _text: object())
    monkeypatch.setattr("agent_review_coordinator.ReviewLedger.model_validate_json", lambda _text: object())
    policy = tmp_path / "policy.yaml"
    ledger = tmp_path / "ledger.json"
    policy.write_text("version: 1")
    ledger.write_text("{}")
    args = _args(tmp_path)
    args.policy = str(policy)
    args.ledger = str(ledger)

    first = maintenance_check._monitor_observation(args)
    second = maintenance_check._monitor_observation(args)

    assert first[0] == second[0] == 0
    assert first[1] != second[1]


def test_policy_free_changes_requested_is_actionable(monkeypatch, tmp_path):
    pr = SimpleNamespace(
        is_draft=False, latest_commit_sha="head", review_decision="CHANGES_REQUESTED",
        merge_state="CLEAN", mergeable="MERGEABLE", status=SimpleNamespace(value="clean"),
        failing_checks=[], review_comments=[],
    )
    monkeypatch.setattr(maintenance_check, "_resolve_pr_by_number", lambda *_args, **_kwargs: pr)
    monkeypatch.setattr(
        "agentic_pr_dash.github_api.scan_review_threads_observation",
        lambda *_args: ObservationReadResult.observed(([], [])),
    )
    assert maintenance_check._monitor_observation(_args(tmp_path)) == (10, "head")


def test_policy_free_unobservable_review_is_watch_pending(monkeypatch, tmp_path):
    pr = SimpleNamespace(is_draft=False, latest_commit_sha="head", latest_commit_date="")
    monkeypatch.setattr(maintenance_check, "_resolve_pr_by_number", lambda *_args, **_kwargs: pr)
    monkeypatch.setattr(
        "agentic_pr_dash.github_api.scan_review_threads_observation",
        lambda *_args: ObservationReadResult.unavailable("boom"),
    )
    assert maintenance_check._monitor_observation(_args(tmp_path)) == (11, "head")


def test_monitor_poll_interval_must_be_positive():
    assert maintenance_check._positive_float("1") == 1.0
    for value in ("0", "-1"):
        try:
            maintenance_check._positive_float(value)
        except Exception as exc:
            assert "greater than zero" in str(exc)
        else:
            raise AssertionError(f"accepted invalid poll interval {value}")
