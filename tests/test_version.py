"""Package version metadata."""

from __future__ import annotations

import tomllib
from pathlib import Path

from lupaxa.git_ignore_inspector import version as version_mod
from lupaxa.git_ignore_inspector.version import get_version


def test_version_is_semver_like() -> None:
    assert isinstance(version_mod.__version__, str)
    parts = version_mod.__version__.split(".")
    assert len(parts) >= 2
    assert all(part.isdigit() for part in parts[:2])


def test_get_version_matches_dunder() -> None:
    assert get_version() == version_mod.__version__


def test_gii_installs_as_the_same_command() -> None:
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    scripts = tomllib.loads(project.read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts["gii"] == scripts["git-ignore-inspector"]
