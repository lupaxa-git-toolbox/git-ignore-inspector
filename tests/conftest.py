"""Temporary repositories that cannot see the developer's Git configuration."""

from __future__ import annotations

import io
import os
import subprocess
from collections.abc import Iterator
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from lupaxa.git_ignore_inspector.cli import main


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point Git at a throwaway config and home so tests stay isolated."""
    home = tmp_path / "home"
    xdg = home / ".config"
    xdg.mkdir(parents=True)
    config = home / "gitconfig"
    config.write_text(
        "[init]\n\tdefaultBranch = master\n"
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        "[core]\n\tautocrlf = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    monkeypatch.delenv("GIT_IGNORE_INSPECTOR_CONFIG", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    return home


@pytest.fixture
def repo(git_env: Path, tmp_path: Path) -> Path:
    """An empty repository with one initial empty tree unavailable until commit_all."""
    del git_env
    root = tmp_path / "repo"
    root.mkdir()
    run_git(root, "init", "-q")
    return root


def run_git(root: Path, *args: str) -> None:
    """Run one Git command inside a fixture repository."""
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def commit_all(root: Path, message: str = "init") -> None:
    """Commit the current work tree."""
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", message)


def invoke(monkeypatch: pytest.MonkeyPatch, cwd: Path, args: list[str]) -> tuple[int, str, str]:
    """Run the CLI in-process and capture both streams."""
    monkeypatch.chdir(cwd)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(args)
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """Return the invoke helper bound to the active monkeypatch."""

    def _invoke(cwd: Path, args: list[str]) -> tuple[int, str, str]:
        return invoke(monkeypatch, cwd, args)

    yield _invoke
