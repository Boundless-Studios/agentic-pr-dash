from agentic_pr_dash.completion_state import (
    CompletionBlocker,
    CompletionStateRequest,
    CompletionStateResult,
    collect_completion_state,
    evaluate_completion_state,
)


def test_completion_result_round_trips_versioned_json() -> None:
    result = CompletionStateResult(
        branch="feature/demo",
        head_oid="abc123",
        blockers=(CompletionBlocker("unpushed", "Commit is not pushed", "git push"),),
        advisories=("PR lookup unavailable",),
    )

    assert CompletionStateResult.from_json(result.to_json()) == result
    assert result.schema_version == 1


def test_evaluator_combines_generic_probes_and_policy_callback() -> None:
    request = CompletionStateRequest(
        branch="feature/demo", head_oid="abc123", session_id="session-1"
    )

    result = evaluate_completion_state(
        request,
        probes=(
            lambda _request: CompletionBlocker(
                "uncommitted", "One file is uncommitted", "commit it"
            ),
            lambda _request: None,
        ),
        policy_callbacks=(
            lambda _request: (
                CompletionBlocker("pr-body", "Missing verification", "edit PR"),
            ),
        ),
    )

    assert [blocker.check_id for blocker in result.blockers] == [
        "uncommitted",
        "pr-body",
    ]


def test_unavailable_probe_is_advisory_not_clean() -> None:
    request = CompletionStateRequest(branch="feature/demo", head_oid="abc123")

    result = evaluate_completion_state(
        request,
        probes=(lambda _request: RuntimeError("GitHub unavailable"),),
    )

    assert result.blockers == ()
    assert result.advisories == ("GitHub unavailable",)
    assert not result.observable


def test_collector_owns_generic_git_state_and_calls_policy_with_pr() -> None:
    commands: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...]) -> str:
        commands.append(command)
        return "M file.py" if command[:2] == ("git", "status") else "0"

    seen_prs: list[dict] = []

    result = collect_completion_state(
        CompletionStateRequest(branch="feature/demo", head_oid="abc123"),
        command_runner=run,
        pr_lookup=lambda _branch: {"number": 42, "isDraft": False},
        policy_callbacks=(
            lambda _request, pr: seen_prs.append(pr) or (),
        ),
    )

    assert [blocker.check_id for blocker in result.blockers] == ["uncommitted-files"]
    assert seen_prs == [{"number": 42, "isDraft": False}]
    assert ("git", "status", "--porcelain") in commands


def test_collector_marks_pr_lookup_outage_unobservable() -> None:
    result = collect_completion_state(
        CompletionStateRequest(branch="feature/demo", head_oid="abc123"),
        command_runner=lambda _command: "0",
        pr_lookup=lambda _branch: (_ for _ in ()).throw(RuntimeError("GitHub down")),
    )

    assert result.advisories == ("PR lookup unavailable: GitHub down",)
    assert not result.observable
