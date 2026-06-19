"""Results file serialization."""

from __future__ import annotations

import json

from .config import CIWatchConfig


def write_results(cfg: CIWatchConfig, results: dict) -> None:
    cfg.results_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.results_file.write_text(json.dumps(results, indent=2) + "\n")
