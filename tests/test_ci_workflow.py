from pathlib import Path

import yaml


WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"


def test_ci_uses_only_github_hosted_runner() -> None:
    workflow_text = WORKFLOW_PATH.read_text()
    workflow = yaml.safe_load(workflow_text)

    assert set(workflow["jobs"]) == {"test"}
    assert workflow["jobs"]["test"]["runs-on"] == "ubuntu-latest"
    assert "needs" not in workflow["jobs"]["test"]
    assert all(
        forbidden not in workflow_text
        for forbidden in (
            "gaia-ci-desktop",
            "self-hosted",
            "RUNNERS_PROBE_PAT",
            "fromJSON(",
        )
    )
