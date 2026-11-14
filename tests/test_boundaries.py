"""Repository boundaries, a missing Git executable, and the read-only guard."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from conftest import commit_all, run_git

from lupaxa.git_ignore_inspector.errors import ToolError
from lupaxa.git_ignore_inspector.gitio import GitRunner


def test_worktree_uses_the_linked_git_dir(repo: Path, cli, tmp_path: Path) -> None:
    (repo / "file.txt").write_text("x", encoding="utf-8")
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    commit_all(repo)
    linked = tmp_path / "linked"
    run_git(repo, "worktree", "add", "-q", str(linked), "HEAD")
    assert (linked / ".git").is_file()
    (linked / "extra.log").write_text("x", encoding="utf-8")
    code, out, _err = cli(linked, ["check", "extra.log"])
    assert code == 0
    assert "IGNORED" in out
    code, out, _err = cli(linked, ["sources", "--json"])
    assert code == 0
    assert "worktree" in out
    assert '"worktree": true' in out


def test_nested_repository_is_a_boundary(repo: Path, cli) -> None:
    inner = repo / "inner"
    inner.mkdir()
    run_git(inner, "init", "-q")
    (inner / ".gitignore").write_text("*.secret\n", encoding="utf-8")
    (inner / "token.secret").write_text("x", encoding="utf-8")
    (repo / ".gitignore").write_text("*.secret\n", encoding="utf-8")
    code, out, _err = cli(repo, ["check", "inner/token.secret", "--json"])
    assert code == 0
    assert "NESTED_REPOSITORY" in out
    code, out, _err = cli(inner, ["check", "token.secret"])
    assert code == 0
    assert "IGNORED" in out
    assert "inner" not in out.split("Classification:", maxsplit=1)[-1].splitlines()[0]


def test_submodule_is_a_boundary(repo: Path, cli, tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    run_git(child, "init", "-q")
    (child / "lib.txt").write_text("x", encoding="utf-8")
    commit_all(child)
    run_git(
        repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", child.as_uri(), "vendor"
    )
    (repo / "vendor" / "lib.txt").write_text("changed", encoding="utf-8")
    code, out, _err = cli(repo, ["check", "vendor/lib.txt", "--json"])
    assert code == 0
    assert "NESTED_REPOSITORY" in out
    code, out, _err = cli(repo / "vendor", ["check", "lib.txt"])
    assert code == 0
    assert "TRACKED" in out


def test_bare_repository_is_unsupported(git_env: Path, cli, tmp_path: Path) -> None:
    del git_env
    bare = tmp_path / "bare.git"
    run_git(tmp_path, "init", "--bare", "-q", str(bare))
    code, _out, err = cli(tmp_path, ["--repo", str(bare), "check", "file"])
    assert code == 4
    assert "Bare" in err


def test_missing_and_old_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cli) -> None:
    empty = tmp_path / "bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    code, _out, err = cli(tmp_path, ["stats"])
    assert code == 4
    assert "not found" in err.lower()

    fake = empty / "git"
    fake.write_text("#!/bin/sh\necho 'git version 2.0.0'\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    code, _out, err = cli(tmp_path, ["stats"])
    assert code == 4
    assert "2.0.0" in err


def test_runner_refuses_mutating_git(git_env: Path) -> None:
    del git_env
    runner = GitRunner()
    with pytest.raises(ToolError) as caught:
        runner.run(["rm", "file"])
    assert caught.value.exit_code == 4
    with pytest.raises(ToolError):
        runner.run(["config", "--unset", "core.editor"])


def test_symlink_inside_the_repository_is_classified(repo: Path, cli, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    (repo / ".gitignore").write_text("link.txt\n", encoding="utf-8")
    try:
        (repo / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not available")
    code, out, _err = cli(repo, ["check", "link.txt"])
    assert code == 0
    assert "IGNORED" in out
    assert os.path.basename(out) or "link.txt" in out
