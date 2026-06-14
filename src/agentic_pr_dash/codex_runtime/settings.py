"""Layered Codex hook/skill settings resolution (BOU-1672).

Repo-agnostic. Settings are resolved from two JSON files the HOST repo owns:

* a ``defaults`` file (committed defaults + profiles), and
* a ``local`` override file (per-checkout, gitignored).

Resolution order: defaults, deep-merged with local, then the active profile's
overrides deep-merged on top, then per-item env overrides
(``CODEX_HOOK_<NAME>`` / ``CODEX_SKILL_<NAME>``).

A host repo binds its two paths once via :class:`SettingsResolver` and calls
``resolver.hook_enabled(...)``. The module-level :func:`hook_enabled` /
:func:`skill_enabled` / :func:`load_settings` take the paths explicitly and back
both the resolver and the CLI (:func:`main`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import resources
from pathlib import Path
from typing import Any, Callable

#: Name of the package-data file holding the framework's generic settings
#: skeleton (the profiles structure + framework-owned hook defaults). A host
#: repo deep-merges its own hook entries on top to form its committed
#: ``default-settings.json``.
_FRAGMENT_RESOURCE = "default-settings-fragment.json"


def default_settings_fragment() -> dict[str, Any]:
    """Return the framework's generic default-settings skeleton.

    Shipped as package data so an installer / a fresh checkout can seed a
    starting ``default-settings.json`` from the package rather than hand-copying
    the profiles structure. Host repos overlay their own hook entries on top.
    """
    text = resources.files("agentic_pr_dash.codex_runtime").joinpath(_FRAGMENT_RESOURCE).read_text(
        encoding="utf-8"
    )
    parsed = json.loads(text)
    return parsed if isinstance(parsed, dict) else {}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _parse_bool(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disable", "disabled"}:
        return False
    return None


def _env_key(hook_name: str) -> str:
    return "CODEX_HOOK_" + hook_name.upper().replace("-", "_")


def _skill_env_key(skill_name: str) -> str:
    return "CODEX_SKILL_" + skill_name.upper().replace("-", "_")


def _apply_env_overrides(items: dict[str, Any], prefix_key: Callable[[str], str]) -> dict[str, Any]:
    resolved = dict(items)
    for item_name in list(resolved):
        env_value = os.environ.get(prefix_key(item_name))
        if env_value is None:
            continue
        parsed = _parse_bool(env_value)
        if parsed is not None:
            resolved[item_name] = parsed
    return resolved


def load_settings(*, defaults_path: Path, local_path: Path) -> dict[str, Any]:
    defaults = _read_json(defaults_path)
    local = _read_json(local_path)
    merged = _deep_merge(defaults, local)

    hooks = dict(merged.get("hooks", {}) or {})
    skills = dict(merged.get("skills", {}) or {})
    profiles = merged.get("profiles", {}) or {}
    available_profiles = profiles.get("available", {}) or {}
    active_profile = profiles.get("active", "")
    if active_profile and isinstance(available_profiles.get(active_profile), dict):
        hooks = _deep_merge(hooks, available_profiles[active_profile])

    merged["hooks"] = _apply_env_overrides(hooks, _env_key)
    merged["skills"] = _apply_env_overrides(skills, _skill_env_key)
    return merged


def _resolve_item(
    section: str,
    item_name: str,
    *,
    default: bool,
    defaults_path: Path,
    local_path: Path,
) -> bool:
    cfg = load_settings(defaults_path=defaults_path, local_path=local_path)
    value = (cfg.get(section, {}) or {}).get(item_name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        parsed = _parse_bool(value)
        if parsed is not None:
            return parsed
    return default


def hook_enabled(
    hook_name: str,
    *,
    default: bool = True,
    defaults_path: Path,
    local_path: Path,
) -> bool:
    return _resolve_item(
        "hooks", hook_name, default=default, defaults_path=defaults_path, local_path=local_path
    )


def skill_enabled(
    skill_name: str,
    *,
    default: bool = True,
    defaults_path: Path,
    local_path: Path,
) -> bool:
    return _resolve_item(
        "skills", skill_name, default=default, defaults_path=defaults_path, local_path=local_path
    )


class SettingsResolver:
    """Binds a host repo's defaults/local settings paths once.

    Lets a thin shim call ``resolver.hook_enabled("peon_ping")`` without
    threading the two paths through every call site.
    """

    def __init__(self, *, defaults_path: Path, local_path: Path) -> None:
        self.defaults_path = defaults_path
        self.local_path = local_path

    def load_settings(self) -> dict[str, Any]:
        return load_settings(defaults_path=self.defaults_path, local_path=self.local_path)

    def hook_enabled(self, hook_name: str, *, default: bool = True) -> bool:
        return hook_enabled(
            hook_name,
            default=default,
            defaults_path=self.defaults_path,
            local_path=self.local_path,
        )

    def skill_enabled(self, skill_name: str, *, default: bool = True) -> bool:
        return skill_enabled(
            skill_name,
            default=default,
            defaults_path=self.defaults_path,
            local_path=self.local_path,
        )


def _write_local(local_path: Path, payload: dict[str, Any]) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with local_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _set_item(local_path: Path, section: str, item_name: str, enabled: bool) -> None:
    payload = _read_json(local_path)
    items = payload.setdefault(section, {})
    if not isinstance(items, dict):
        items = {}
        payload[section] = items
    items[item_name] = enabled
    _write_local(local_path, payload)


def _set_profile(local_path: Path, profile_name: str) -> None:
    payload = _read_json(local_path)
    profiles = payload.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        profiles = {}
        payload["profiles"] = profiles
    profiles["active"] = profile_name
    _write_local(local_path, payload)


def _print_section(title: str, items: dict[str, Any]) -> None:
    print(f"[{title}]")
    width = max([len(name) for name in items] + [4])
    for name in sorted(items):
        state = "on" if bool(items[name]) else "off"
        print(f"{name:<{width}}  {state}")


def _print_list(cfg: dict[str, Any], section: str) -> None:
    if section in {"all", "skills"}:
        _print_section("skills", cfg.get("skills", {}) or {})
    if section == "all":
        print()
    if section in {"all", "hooks"}:
        _print_section("hooks", cfg.get("hooks", {}) or {})


def build_cli(
    *,
    default_defaults_path: Path,
    default_local_path: Path,
    description: str = "Manage local Codex hook settings.",
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--defaults", type=Path, default=default_defaults_path)
    parser.add_argument("--local", type=Path, default=default_local_path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="Show effective settings.")
    list_parser.add_argument("--section", choices=("all", "hooks", "skills"), default="all")

    enable_parser = subparsers.add_parser("enable", help="Enable one hook locally.")
    enable_parser.add_argument("--section", choices=("hooks", "skills"), default="hooks")
    enable_parser.add_argument("name")

    disable_parser = subparsers.add_parser("disable", help="Disable one hook locally.")
    disable_parser.add_argument("--section", choices=("hooks", "skills"), default="hooks")
    disable_parser.add_argument("name")

    profile_parser = subparsers.add_parser("profile", help="Set local active profile.")
    profile_parser.add_argument("name")

    subparsers.add_parser("reset", help="Remove local hook settings.")
    return parser


def run_cli(args: argparse.Namespace) -> int:
    if args.command == "list":
        _print_list(
            load_settings(defaults_path=args.defaults, local_path=args.local),
            args.section,
        )
        return 0
    if args.command == "enable":
        _set_item(args.local, args.section, args.name, True)
        print(f"enabled {args.section}.{args.name}")
        return 0
    if args.command == "disable":
        _set_item(args.local, args.section, args.name, False)
        print(f"disabled {args.section}.{args.name}")
        return 0
    if args.command == "profile":
        defaults = _read_json(args.defaults)
        available = (defaults.get("profiles", {}) or {}).get("available", {}) or {}
        if args.name not in available:
            print(f"unknown profile: {args.name}", file=sys.stderr)
            return 2
        _set_profile(args.local, args.name)
        print(f"profile {args.name}")
        return 0
    if args.command == "reset":
        try:
            args.local.unlink()
        except FileNotFoundError:
            pass
        print("reset local hook settings")
        return 0
    return 2


def main(
    argv: list[str] | None = None,
    *,
    default_defaults_path: Path,
    default_local_path: Path,
) -> int:
    parser = build_cli(
        default_defaults_path=default_defaults_path,
        default_local_path=default_local_path,
    )
    args = parser.parse_args(argv)
    return run_cli(args)
