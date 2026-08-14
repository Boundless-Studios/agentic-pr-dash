"""Head-scoped ownership contract for PR finalization."""

from __future__ import annotations

import importlib.util
import inspect
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_coordinator.store import JsonlClaimStore
from pydantic import ValidationError

from agentic_pr_dash import finalization_lease, maintenance_check


class FinalizationLeaseContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)

    def _service(self):
        self.assertTrue(hasattr(finalization_lease, "FinalizationLeaseService"))
        return finalization_lease.FinalizationLeaseService(
            JsonlClaimStore(Path(self._temporary.name) / "claims.jsonl")
        )

    @staticmethod
    def _key(head: str = "a" * 40):
        return finalization_lease.FinalizationKey(
            repository="boundless/gaia",
            pr_number=42,
            head_sha=head,
        )

    @staticmethod
    def _actor(session_id: str):
        return finalization_lease.FinalizationActor(
            session_id=session_id,
            pid=os.getpid(),
            agent="codex",
            worktree_path="/tmp/gaia",
        )

    def test_finalization_lease_module_is_public(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("agentic_pr_dash.finalization_lease"),
            "the reusable finalization lease boundary does not exist",
        )

    def test_finalization_key_requires_exact_head_identity(self) -> None:
        self.assertTrue(hasattr(finalization_lease, "FinalizationKey"))
        FinalizationKey = finalization_lease.FinalizationKey
        with self.assertRaises(ValidationError):
            FinalizationKey(repository="boundless/gaia", pr_number=42, head_sha="")

    def test_finalization_task_changes_with_head(self) -> None:
        self.assertTrue(hasattr(finalization_lease, "FinalizationKey"))
        self.assertTrue(hasattr(finalization_lease, "finalization_task"))
        FinalizationKey = finalization_lease.FinalizationKey
        finalization_task = finalization_lease.finalization_task
        first = finalization_task(
            FinalizationKey(
                repository="boundless/gaia",
                pr_number=42,
                head_sha="a" * 40,
            )
        )
        second = finalization_task(
            FinalizationKey(
                repository="boundless/gaia",
                pr_number=42,
                head_sha="b" * 40,
            )
        )

        self.assertNotEqual(first.task_id, second.task_id)
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_only_one_actor_acquires_a_pr_head(self) -> None:
        service = self._service()
        first = service.acquire(self._key(), self._actor("live-session"))
        second = service.acquire(self._key(), self._actor("detached-maintainer"))

        self.assertTrue(first.acquired)
        self.assertIsNotNone(first.lease)
        self.assertFalse(second.acquired)
        self.assertIsNone(second.lease)
        self.assertEqual(second.conflict_session_id, "live-session")

    def test_new_head_has_independent_finalization_authority(self) -> None:
        service = self._service()
        first = service.acquire(self._key("a" * 40), self._actor("live-session"))
        second = service.acquire(
            self._key("b" * 40), self._actor("detached-maintainer")
        )

        self.assertTrue(first.acquired)
        self.assertTrue(second.acquired)

    def test_release_is_fenced_by_claim_and_epoch(self) -> None:
        service = self._service()
        acquired = service.acquire(self._key(), self._actor("live-session"))
        self.assertIsNotNone(acquired.lease)
        lease = acquired.lease

        stale = lease.model_copy(update={"lease_epoch": lease.lease_epoch + 1})
        self.assertFalse(service.release(stale).released)
        self.assertTrue(service.release(lease).released)

    def test_conflicting_actor_cannot_run_finalization_mutation(self) -> None:
        self.assertTrue(hasattr(finalization_lease, "run_with_finalization_lease"))
        service = self._service()
        first = service.acquire(self._key(), self._actor("live-session"))
        self.assertTrue(first.acquired)
        called = False

        def mutate() -> int:
            nonlocal called
            called = True
            return 0

        result = finalization_lease.run_with_finalization_lease(
            key=self._key(),
            actor=self._actor("detached-maintainer"),
            operation=mutate,
            service=service,
        )

        self.assertFalse(result.executed)
        self.assertEqual(result.exit_code, 10)
        self.assertFalse(called)

    def test_complete_command_runs_mutations_under_finalization_lease(self) -> None:
        source = inspect.getsource(maintenance_check._cmd_complete)

        self.assertIn("run_with_finalization_lease", source)

    def test_complete_command_passes_resolved_head_to_lease(self) -> None:
        args = SimpleNamespace(cwd="/tmp/gaia", pr=42, session_id="session-a")
        pr = SimpleNamespace(
            number=42,
            repo="boundless/gaia",
            latest_commit_sha="a" * 40,
        )
        captured = {}

        def run_lease(**kwargs):
            captured.update(kwargs)
            return finalization_lease.FinalizationRun(
                state=finalization_lease.FinalizationRunState.COMPLETED,
                exit_code=7,
                reason="completed",
            )

        with (
            patch.object(
                maintenance_check,
                "_complete_resolve_target_pr",
                return_value=pr,
            ),
            patch.object(
                finalization_lease,
                "run_with_finalization_lease",
                side_effect=run_lease,
            ),
        ):
            result = maintenance_check._cmd_complete(args)

        self.assertEqual(result, 7)
        self.assertEqual(captured["key"].head_sha, "a" * 40)
        self.assertEqual(captured["actor"].caller_session_id, "session-a")
        self.assertTrue(captured["actor"].session_id.startswith("session-a:"))

    def test_invocations_never_share_lease_owner_identity(self) -> None:
        first = finalization_lease.invocation_actor(
            caller_session_id="caller", pid=123, agent="codex", worktree_path="/tmp"
        )
        second = finalization_lease.invocation_actor(
            caller_session_id="caller", pid=123, agent="codex", worktree_path="/tmp"
        )

        self.assertNotEqual(first.session_id, second.session_id)
        self.assertEqual(first.caller_session_id, "caller")

    def test_complete_derives_repository_when_pr_snapshot_omits_it(self) -> None:
        args = SimpleNamespace(cwd="/tmp/gaia", pr=42, session_id="caller")
        pr = SimpleNamespace(
            number=42,
            repo="",
            latest_commit_sha="a" * 40,
        )
        captured = {}

        def run_lease(**kwargs):
            captured.update(kwargs)
            return finalization_lease.FinalizationRun(
                state=finalization_lease.FinalizationRunState.COMPLETED,
                exit_code=0,
                reason="completed",
            )

        with (
            patch.object(
                maintenance_check, "_complete_resolve_target_pr", return_value=pr
            ),
            patch.object(
                maintenance_check, "_repo_slug", return_value="boundless/gaia"
            ),
            patch.object(
                finalization_lease,
                "run_with_finalization_lease",
                side_effect=run_lease,
            ),
        ):
            result = maintenance_check._cmd_complete(args)

        self.assertEqual(result, 0)
        self.assertEqual(captured["key"].repository, "boundless/gaia")

    def test_claim_store_failure_is_typed_unavailable(self) -> None:
        service = self._service()
        with patch.object(
            service._coordinator, "claim_task", side_effect=OSError("offline")
        ):
            result = service.acquire(self._key(), self._actor("caller"))

        self.assertEqual(
            result.state, finalization_lease.LeaseAcquisitionState.UNAVAILABLE
        )
        self.assertIsNone(result.lease)

    def test_long_operation_renews_lease_before_expiry(self) -> None:
        store = JsonlClaimStore(Path(self._temporary.name) / "renewals.jsonl")
        service = finalization_lease.FinalizationLeaseService(store, lease_seconds=1)
        competitor = finalization_lease.FinalizationLeaseService(store, lease_seconds=1)
        competing_results = []

        def operation() -> int:
            time.sleep(1.2)
            competing_results.append(
                competitor.acquire(self._key(), self._actor("competitor"))
            )
            return 0

        result = finalization_lease.run_with_finalization_lease(
            key=self._key(),
            actor=self._actor("owner"),
            operation=operation,
            service=service,
        )

        self.assertEqual(
            result.state, finalization_lease.FinalizationRunState.COMPLETED
        )
        self.assertEqual(
            competing_results[0].state,
            finalization_lease.LeaseAcquisitionState.CONTENDED,
        )

    def test_sweep_refuses_before_resolving_pr(self) -> None:
        args = SimpleNamespace(cwd="/tmp/gaia", sweep_p2=True)
        with patch.object(
            maintenance_check,
            "_complete_resolve_target_pr",
            side_effect=AssertionError("must not resolve"),
        ):
            result = maintenance_check._cmd_complete(args)

        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
