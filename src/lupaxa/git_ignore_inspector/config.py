"""Optional TOML configuration.

Configuration changes presentation and which audit checks run. It never
changes Git's ignore decision.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from lupaxa.git_ignore_inspector.errors import ToolError

AUDIT_CHECKS = frozenset({"duplicates", "redundant", "unused", "tracked", "confusing"})
_OUTPUTS = frozenset({"text", "json"})
_COLORS = frozenset({"auto", "always", "never"})


@dataclass
class AppConfig:
    """Resolved settings. Missing files leave the defaults in place."""

    output: str = "text"
    color: str = "auto"
    max_results: int = 10000
    expand_ignored_directories: bool = False
    unused_rule_analysis: bool = True
    audit_enabled: set[str] = field(default_factory=lambda: set(AUDIT_CHECKS))
    suppressed: set[str] = field(default_factory=set)
    path: Path | None = None


def load_config(explicit: Path | None, cwd: Path, repo_root: Path | None) -> AppConfig:
    """Load the first configuration file in the search order, or the defaults."""
    path = _resolve_path(explicit, cwd, repo_root)
    if path is None:
        return AppConfig()
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ToolError("CONFIG", f"Could not read configuration {path}: {exc}", 3) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ToolError("CONFIG", f"Malformed configuration {path}: {exc}", 3) from exc
    if not isinstance(payload, dict):
        raise ToolError("CONFIG", f"Configuration {path} must be a TOML table.", 3)
    return _parse(payload, path)


def _resolve_path(explicit: Path | None, cwd: Path, repo_root: Path | None) -> Path | None:
    if explicit is not None:
        if not explicit.is_file():
            raise ToolError("CONFIG", f"Configuration file does not exist: {explicit}", 3)
        return explicit
    env = os.environ.get("GIT_IGNORE_INSPECTOR_CONFIG")
    if env:
        path = Path(env).expanduser()
        if not path.is_file():
            raise ToolError("CONFIG", f"GIT_IGNORE_INSPECTOR_CONFIG does not exist: {path}", 3)
        return path
    for candidate in _candidates(cwd, repo_root):
        if candidate.is_file():
            return candidate
    return None


def _candidates(cwd: Path, repo_root: Path | None) -> list[Path]:
    found = [cwd / "git-ignore-inspector.toml"]
    if repo_root is not None:
        repo_file = repo_root / "git-ignore-inspector.toml"
        if repo_file not in found:
            found.append(repo_file)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    found.append(base / "git-ignore-inspector" / "config.toml")
    return found


def _parse(payload: dict[str, object], path: Path) -> AppConfig:
    output = _choice(payload, "output", _OUTPUTS, "text", path)
    color = _choice(payload, "color", _COLORS, "auto", path)
    max_results = payload.get("max_results", 10000)
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 0:
        raise ToolError("CONFIG", f"{path}: max_results must be a non-negative integer.", 3)
    expand = _bool(payload, "expand_ignored_directories", False, path)
    unused = _bool(payload, "unused_rule_analysis", True, path)
    audit = payload.get("audit", {})
    if audit is None:
        audit = {}
    if not isinstance(audit, dict):
        raise ToolError("CONFIG", f"{path}: audit must be a table.", 3)
    enabled = set(AUDIT_CHECKS)
    if "enabled" in audit:
        enabled = _string_set(audit["enabled"], "audit.enabled", path)
        unknown = enabled - AUDIT_CHECKS
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ToolError("CONFIG", f"{path}: unknown audit checks: {names}", 3)
    suppressed = set()
    if "suppressed" in audit:
        suppressed = _string_set(audit["suppressed"], "audit.suppressed", path)
    if not unused:
        enabled.discard("unused")
    return AppConfig(
        output=output,
        color=color,
        max_results=max_results,
        expand_ignored_directories=expand,
        unused_rule_analysis=unused,
        audit_enabled=enabled,
        suppressed=suppressed,
        path=path,
    )


def _choice(
    payload: dict[str, object],
    key: str,
    allowed: frozenset[str],
    default: str,
    path: Path,
) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str) or value not in allowed:
        options = ", ".join(sorted(allowed))
        raise ToolError("CONFIG", f"{path}: {key} must be one of: {options}", 3)
    return value


def _bool(payload: dict[str, object], key: str, default: bool, path: Path) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ToolError("CONFIG", f"{path}: {key} must be true or false.", 3)
    return value


def _string_set(value: object, label: str, path: Path) -> set[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ToolError("CONFIG", f"{path}: {label} must be an array of strings.", 3)
    return set(value)
