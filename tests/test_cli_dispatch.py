"""The public console-script dispatch must route every executor subcommand
into maintenance_check, including reconcile-prs (PR #16 review round 2, #1)."""
from agentic_pr_dash import cli


def test_reconcile_prs_is_an_executor_command():
    assert "reconcile-prs" in cli._EXECUTOR_CMDS


def test_reconcile_prs_routes_to_maintenance_check(monkeypatch):
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        return 0

    import agentic_pr_dash.maintenance_check as mc
    monkeypatch.setattr(mc, "main", fake_main)
    rc = cli.main(["reconcile-prs", "--session-id", "sess-A", "--cwd", "/tmp"])
    assert rc == 0
    assert seen["argv"] == ["reconcile-prs", "--session-id", "sess-A", "--cwd", "/tmp"]


def test_unknown_command_still_rejected(capsys):
    rc = cli.main(["definitely-not-a-command"])
    assert rc == 2
    assert "unknown command" in capsys.readouterr().err
