import types

from agentic_pr_dash.capability_check import CapabilityRequirement, missing_capabilities


def test_missing_capabilities_reports_absent_module(monkeypatch) -> None:
    def missing(_name: str):
        raise ModuleNotFoundError("missing")

    monkeypatch.setattr("agentic_pr_dash.capability_check.importlib.import_module", missing)

    assert missing_capabilities((CapabilityRequirement("pkg.guard"),)) == (
        "pkg.guard (module unavailable)",
    )


def test_missing_capabilities_checks_callable_attributes(monkeypatch) -> None:
    module = types.SimpleNamespace(run=lambda: None, version="1")
    monkeypatch.setattr(
        "agentic_pr_dash.capability_check.importlib.import_module", lambda _name: module
    )

    assert missing_capabilities(
        (CapabilityRequirement("pkg.guard", callables=("run", "evaluate")),)
    ) == ("pkg.guard.evaluate (not callable)",)
