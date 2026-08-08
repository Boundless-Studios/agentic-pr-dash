"""BOU-2895 webhook ingress contract tests."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

from agentic_pr_dash import app
from agentic_pr_dash.webhook import (
    MAX_WEBHOOK_BODY_BYTES,
    GithubWebhookIngress,
    WebhookRejected,
)


class _Orchestrator:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, int, str, str | None]] = []
        self.head_events: list[tuple[str, str, str, str | None]] = []
        self.refreshes = 0
        self.event_started = asyncio.Event()
        self.release_event = asyncio.Event()

    async def handle_github_event(
        self,
        event_name: str,
        repo: str,
        number: int,
        head_sha: str,
        action: str | None = None,
    ) -> None:
        self.event_started.set()
        await self.release_event.wait()
        self.events.append((event_name, repo, number, head_sha, action))

    async def refresh_prs(self) -> None:
        self.refreshes += 1

    async def handle_github_check_event(
        self,
        event_name: str,
        repo: str,
        head_sha: str,
        action: str | None = None,
    ) -> None:
        self.head_events.append((event_name, repo, head_sha, action))


def _signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _pull_request_body() -> bytes:
    return json.dumps(
        {
            "action": "synchronize",
            "repository": {"full_name": "acme/widgets"},
            "pull_request": {"number": 42, "head": {"sha": "abc123"}},
        }
    ).encode()


def test_webhook_rejects_unconfigured_invalid_and_oversized_requests(monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.delenv("AGENTIC_PR_DASH_GITHUB_WEBHOOK_SECRET", raising=False)
        monkeypatch.delenv("AGENTIC_PR_DASH_GITHUB_WEBHOOK_SECRET_FILE", raising=False)
        ingress = GithubWebhookIngress(_Orchestrator(), lambda: None)
        body = _pull_request_body()

        for signature, expected_status in (("sha256=bad", 503),):
            try:
                ingress.accept("pull_request", "one", signature, body)
            except WebhookRejected as exc:
                assert exc.status_code == expected_status
            else:
                raise AssertionError("request should have been rejected")

        monkeypatch.setenv("AGENTIC_PR_DASH_GITHUB_WEBHOOK_SECRET", "hook-secret")
        try:
            ingress.accept("pull_request", "two", "sha256=bad", body)
        except WebhookRejected as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError("bad signature should have been rejected")

        try:
            ingress.accept(
                "pull_request",
                "three",
                _signature("hook-secret", b"x" * (MAX_WEBHOOK_BODY_BYTES + 1)),
                b"x" * (MAX_WEBHOOK_BODY_BYTES + 1),
            )
        except WebhookRejected as exc:
            assert exc.status_code == 413
        else:
            raise AssertionError("oversized body should have been rejected")

        await ingress.shutdown()

    asyncio.run(scenario())


def test_pull_request_webhook_returns_before_observation_and_debounces(monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("AGENTIC_PR_DASH_GITHUB_WEBHOOK_SECRET", "hook-secret")
        orchestrator = _Orchestrator()
        invalidations = 0

        def invalidate() -> None:
            nonlocal invalidations
            invalidations += 1

        ingress = GithubWebhookIngress(orchestrator, invalidate, debounce_seconds=0.01)
        body = _pull_request_body()

        assert ingress.accept(
            "pull_request", "delivery-1", _signature("hook-secret", body), body
        ) == 202
        await asyncio.wait_for(orchestrator.event_started.wait(), timeout=0.2)
        assert orchestrator.events == []
        assert orchestrator.refreshes == 0

        orchestrator.release_event.set()
        await asyncio.sleep(0.04)
        assert orchestrator.events == [
            ("pull_request", "acme/widgets", 42, "abc123", "synchronize")
        ]
        assert orchestrator.refreshes == 1
        assert invalidations == 1
        await ingress.shutdown()

    asyncio.run(scenario())


def test_check_event_fans_out_and_delivery_is_deduplicated(monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("AGENTIC_PR_DASH_GITHUB_WEBHOOK_SECRET", "hook-secret")
        orchestrator = _Orchestrator()
        orchestrator.release_event.set()
        ingress = GithubWebhookIngress(orchestrator, lambda: None, debounce_seconds=0.01)
        body = json.dumps(
            {
                "action": "completed",
                "repository": {"full_name": "acme/widgets"},
                "check_run": {
                    "head_sha": "def456",
                    "pull_requests": [{"number": 7}, {"number": 8}],
                },
            }
        ).encode()
        signature = _signature("hook-secret", body)

        assert ingress.accept("check_run", "delivery-2", signature, body) == 202
        assert ingress.accept("check_run", "delivery-2", signature, body) == 202
        await asyncio.sleep(0.04)

        assert orchestrator.events == [
            ("check_run", "acme/widgets", 7, "def456", "completed"),
            ("check_run", "acme/widgets", 8, "def456", "completed"),
        ]
        assert orchestrator.refreshes == 1
        await ingress.shutdown()

    asyncio.run(scenario())


def test_review_thread_resolution_and_unassociated_check_are_invalidated(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("AGENTIC_PR_DASH_GITHUB_WEBHOOK_SECRET", "hook-secret")
        orchestrator = _Orchestrator()
        orchestrator.release_event.set()
        ingress = GithubWebhookIngress(orchestrator, lambda: None, debounce_seconds=0.01)
        review_body = json.dumps(
            {
                "action": "resolved",
                "repository": {"full_name": "acme/widgets"},
                "pull_request": {"number": 42, "head": {"sha": "abc123"}},
            }
        ).encode()
        check_body = json.dumps(
            {
                "action": "completed",
                "repository": {"full_name": "acme/widgets"},
                "check_suite": {"head_sha": "fork789", "pull_requests": []},
            }
        ).encode()

        ingress.accept(
            "pull_request_review_thread",
            "delivery-thread",
            _signature("hook-secret", review_body),
            review_body,
        )
        ingress.accept(
            "check_suite",
            "delivery-unassociated-check",
            _signature("hook-secret", check_body),
            check_body,
        )
        await asyncio.sleep(0.04)

        assert orchestrator.events == [
            (
                "pull_request_review_thread",
                "acme/widgets",
                42,
                "abc123",
                "resolved",
            )
        ]
        assert orchestrator.head_events == [
            ("check_suite", "acme/widgets", "fork789", "completed")
        ]
        assert orchestrator.refreshes == 1
        await ingress.shutdown()

    asyncio.run(scenario())


def test_malformed_or_unsupported_payload_is_rejected(monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("AGENTIC_PR_DASH_GITHUB_WEBHOOK_SECRET", "hook-secret")
        ingress = GithubWebhookIngress(_Orchestrator(), lambda: None)

        for event_name, body in (("pull_request", b"{"), ("push", b"{}")):
            try:
                ingress.accept(event_name, None, _signature("hook-secret", body), body)
            except WebhookRejected as exc:
                assert exc.status_code == 400
            else:
                raise AssertionError("payload should have been rejected")

        await ingress.shutdown()

    asyncio.run(scenario())


def test_webhook_route_streams_a_bounded_body_and_returns_accepted(monkeypatch) -> None:
    class _Request:
        def __init__(self, body: bytes, headers: dict[str, str]) -> None:
            self._body = body
            self.headers = headers

        async def stream(self):
            midpoint = len(self._body) // 2
            yield self._body[:midpoint]
            yield self._body[midpoint:]

    async def scenario() -> None:
        monkeypatch.setenv("AGENTIC_PR_DASH_GITHUB_WEBHOOK_SECRET", "hook-secret")
        orchestrator = _Orchestrator()
        orchestrator.release_event.set()
        ingress = GithubWebhookIngress(orchestrator, lambda: None, debounce_seconds=0.01)
        monkeypatch.setattr(app, "webhook_ingress", ingress)
        body = _pull_request_body()
        request = _Request(
            body,
            {
                "x-github-event": "pull_request",
                "x-github-delivery": "delivery-route",
                "x-hub-signature-256": _signature("hook-secret", body),
            },
        )

        response = await app.github_webhook(request)
        assert response.status_code == 202
        await asyncio.sleep(0.04)
        assert orchestrator.refreshes == 1

        oversized = _Request(
            b"x" * (MAX_WEBHOOK_BODY_BYTES + 1),
            {"content-length": str(MAX_WEBHOOK_BODY_BYTES + 1)},
        )
        response = await app.github_webhook(oversized)
        assert response.status_code == 413
        await ingress.shutdown()

    asyncio.run(scenario())
