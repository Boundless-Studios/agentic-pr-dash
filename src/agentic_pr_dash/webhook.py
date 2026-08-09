"""Authenticated, bounded GitHub webhook ingress for PR observations."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import time
from typing import Callable, Protocol


MAX_WEBHOOK_BODY_BYTES = 1024 * 1024
DEFAULT_DEBOUNCE_SECONDS = 2.0
DEFAULT_DELIVERY_TTL_SECONDS = 60 * 60.0
DEFAULT_MAX_DELIVERIES = 4096

_SUPPORTED_EVENTS = {
    "ping",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "pull_request_review_thread",
    "check_run",
    "check_suite",
    "status",
}
_LOG = logging.getLogger(__name__)


class _ObservationOrchestrator(Protocol):
    async def handle_github_event(
        self,
        event_name: str,
        repo: str,
        number: int,
        head_sha: str,
        action: str | None = None,
    ) -> None: ...

    async def refresh_prs(self) -> None: ...

    async def handle_github_check_event(
        self,
        event_name: str,
        repo: str,
        head_sha: str,
        action: str | None = None,
    ) -> None: ...


class WebhookRejected(ValueError):
    """A webhook cannot be safely accepted."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class NormalizedGithubEvent:
    event_name: str
    repo: str
    number: int | None
    head_sha: str
    action: str | None


def _webhook_secret() -> str:
    inline = os.environ.get("AGENTIC_PR_DASH_GITHUB_WEBHOOK_SECRET", "").strip()
    if inline:
        return inline

    secret_file = os.environ.get(
        "AGENTIC_PR_DASH_GITHUB_WEBHOOK_SECRET_FILE", ""
    ).strip()
    if not secret_file:
        raise WebhookRejected(503, "GitHub webhook ingress is not configured")
    try:
        secret = Path(secret_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise WebhookRejected(
            503, "GitHub webhook ingress secret is unavailable"
        ) from exc
    if not secret:
        raise WebhookRejected(503, "GitHub webhook ingress secret is unavailable")
    return secret


def _verify_signature(secret: str, signature: str | None, body: bytes) -> None:
    if not signature or not signature.startswith("sha256="):
        raise WebhookRejected(401, "Invalid GitHub webhook signature")
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WebhookRejected(401, "Invalid GitHub webhook signature")


def _string(mapping: object, key: str) -> str:
    if not isinstance(mapping, dict):
        return ""
    value = mapping.get(key)
    return value if isinstance(value, str) else ""


def _number(mapping: object) -> int | None:
    if not isinstance(mapping, dict):
        return None
    value = mapping.get("number")
    return value if isinstance(value, int) and value > 0 else None


def _normalize(event_name: str, payload: object) -> tuple[NormalizedGithubEvent, ...]:
    normalized_name = event_name.strip().casefold()
    if normalized_name not in _SUPPORTED_EVENTS:
        raise WebhookRejected(400, "Unsupported GitHub webhook event")
    if not isinstance(payload, dict):
        raise WebhookRejected(400, "Malformed GitHub webhook payload")
    if normalized_name == "ping":
        return ()

    repository = payload.get("repository")
    repo = _string(repository, "full_name")
    action = _string(payload, "action") or None
    if not repo:
        raise WebhookRejected(400, "Malformed GitHub webhook payload")

    if normalized_name == "status":
        commit = payload.get("commit")
        head_sha = _string(payload, "sha") or _string(commit, "sha")
        if not head_sha:
            raise WebhookRejected(400, "Malformed GitHub webhook payload")
        return (NormalizedGithubEvent(normalized_name, repo, None, head_sha, action),)

    if normalized_name.startswith("pull_request"):
        pull_request = payload.get("pull_request")
        number = _number(pull_request)
        head = pull_request.get("head") if isinstance(pull_request, dict) else None
        head_sha = _string(head, "sha")
        if number is None or not head_sha:
            raise WebhookRejected(400, "Malformed GitHub webhook payload")
        return (
            NormalizedGithubEvent(
                normalized_name, repo, number, head_sha, action
            ),
        )

    check = payload.get(normalized_name)
    if not isinstance(check, dict):
        raise WebhookRejected(400, "Malformed GitHub webhook payload")
    check_suite = check.get("check_suite")
    head_sha = _string(check, "head_sha") or _string(check_suite, "head_sha")
    pull_requests = check.get("pull_requests")
    if not isinstance(pull_requests, list):
        pull_requests = []
    if not head_sha and pull_requests:
        raise WebhookRejected(400, "Malformed GitHub webhook payload")
    events = tuple(
        NormalizedGithubEvent(normalized_name, repo, number, head_sha, action)
        for pull_request in pull_requests
        if (number := _number(pull_request)) is not None
    )
    if events:
        return events
    if not head_sha:
        raise WebhookRejected(400, "Malformed GitHub webhook payload")
    return (NormalizedGithubEvent(normalized_name, repo, None, head_sha, action),)


class GithubWebhookIngress:
    """Verify, deduplicate, normalize, and asynchronously apply GitHub events."""

    def __init__(
        self,
        orchestrator: _ObservationOrchestrator,
        invalidate_dashboard: Callable[[], None],
        *,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
        delivery_ttl_seconds: float = DEFAULT_DELIVERY_TTL_SECONDS,
        max_deliveries: int = DEFAULT_MAX_DELIVERIES,
    ) -> None:
        self._orchestrator = orchestrator
        self._invalidate_dashboard = invalidate_dashboard
        self._debounce_seconds = debounce_seconds
        self._delivery_ttl_seconds = delivery_ttl_seconds
        self._max_deliveries = max_deliveries
        self._deliveries: OrderedDict[str, float] = OrderedDict()
        self._event_tasks: set[asyncio.Task[None]] = set()
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_started = False
        self._refresh_followup = False
        self._closing = False

    def accept(
        self,
        event_name: str,
        delivery_id: str | None,
        signature: str | None,
        body: bytes,
    ) -> int:
        """Validate and enqueue a delivery without waiting for GitHub reads."""

        if self._closing:
            raise WebhookRejected(503, "GitHub webhook ingress is stopping")
        if len(body) > MAX_WEBHOOK_BODY_BYTES:
            raise WebhookRejected(413, "GitHub webhook payload is too large")
        secret = _webhook_secret()
        _verify_signature(secret, signature, body)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebhookRejected(400, "Malformed GitHub webhook payload") from exc
        events = _normalize(event_name, payload)

        now = time.monotonic()
        self._prune_deliveries(now)
        normalized_delivery = (delivery_id or "").strip()
        if normalized_delivery and normalized_delivery in self._deliveries:
            return 202
        if normalized_delivery:
            self._deliveries[normalized_delivery] = now
            while len(self._deliveries) > self._max_deliveries:
                self._deliveries.popitem(last=False)
        if not events:
            return 202

        task = asyncio.create_task(self._apply_events(events, normalized_delivery))
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)
        return 202

    def _prune_deliveries(self, now: float) -> None:
        cutoff = now - self._delivery_ttl_seconds
        while self._deliveries:
            _, observed_at = next(iter(self._deliveries.items()))
            if observed_at >= cutoff:
                break
            self._deliveries.popitem(last=False)

    async def _apply_events(
        self,
        events: tuple[NormalizedGithubEvent, ...],
        delivery_id: str,
    ) -> None:
        try:
            for event in events:
                if event.number is None:
                    await self._orchestrator.handle_github_check_event(
                        event.event_name,
                        event.repo,
                        event.head_sha,
                        action=event.action,
                    )
                else:
                    await self._orchestrator.handle_github_event(
                        event.event_name,
                        event.repo,
                        event.number,
                        event.head_sha,
                        action=event.action,
                    )
            self._schedule_refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            if delivery_id:
                self._deliveries.pop(delivery_id, None)
            _LOG.exception("GitHub webhook observation invalidation failed")

    def _schedule_refresh(self) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            if self._refresh_started:
                # The GitHub reads are already in flight. Cancelling an
                # asyncio.to_thread boundary does not stop its worker and can
                # strand the associated quota reservation. Coalesce one
                # follow-up refresh after the active transaction instead.
                self._refresh_followup = True
                return
            self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(self._debounced_refresh())

    async def _debounced_refresh(self) -> None:
        try:
            await asyncio.sleep(self._debounce_seconds)
            self._refresh_started = True
            await self._orchestrator.refresh_prs()
            self._invalidate_dashboard()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.exception("GitHub webhook observation refresh failed")
        finally:
            current = asyncio.current_task()
            if self._refresh_task is current:
                self._refresh_task = None
                self._refresh_started = False
                if self._refresh_followup and not self._closing:
                    self._refresh_followup = False
                    self._refresh_task = asyncio.create_task(
                        self._debounced_refresh()
                    )

    async def shutdown(self) -> None:
        self._closing = True
        tasks = list(self._event_tasks)
        if self._refresh_task is not None:
            tasks.append(self._refresh_task)
        self._refresh_followup = False
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._event_tasks.clear()
        self._refresh_task = None
