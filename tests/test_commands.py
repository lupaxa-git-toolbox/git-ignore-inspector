"""Check, explain, list, sources, tracked, audit, and stats against temporary repositories."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from conftest import commit_all, run_git


def test_check_ignored_tracked_missing_and_negation(repo: Path, cli) -> None:
    (repo / ".gitignore").write_text("*.log\n!keep.log\n", encoding="utf-8")
    (repo / "file.log").write_text("x", encoding="utf-8")
    (repo / "keep.log").write_text("x", encoding="utf-8")
    (repo / "notes.txt").write_text("x", encoding="utf-8")
    (repo / ".env").write_text("secret", encoding="utf-8")
    commit_all(repo)
    (repo / ".gitignore").write_text("*.log\n!keep.log\n.env\n", encoding="utf-8")
    (repo / "plain.txt").write_text("x", encoding="utf-8")

    code, out, _err = cli(repo, ["check", "file.log"])
    assert code == 0
    assert "IGNORED" in out
    assert "not tracked" in out
    assert "*.log" in out

    code, out, _err = cli(repo, ["check", "plain.txt"])
    assert code == 0
    assert "NOT_IGNORED" in out

    code, out, _err = cli(repo, ["check", "keep.log"])
    assert code == 0
    assert "TRACKED" in out
    assert "TRACKED_IGNORED" not in out

    code, out, _err = cli(repo, ["check", "missing.log"])
    assert code == 0
    assert "IGNORED" in out

    code, out, _err = cli(repo, ["check", ".env"])
    assert code == 0
    assert "TRACKED_IGNORED" in out

    code, out, _err = cli(repo, ["explain", "file.log"])
    assert code == 0
    assert "Effective pattern" in out
    assert "*" in out


def test_nested_gitignore_exclude_and_global(repo: Path, cli, git_env: Path) -> None:
    nested = repo / "sub"
    nested.mkdir()
    (nested / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    (nested / "data.tmp").write_text("x", encoding="utf-8")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("*.local\n", encoding="utf-8")
    (repo / "app.local").write_text("x", encoding="utf-8")
    global_ignore = git_env / "global-ignore"
    global_ignore.write_text("*.secret\n", encoding="utf-8")
    run_git(repo, "config", "--global", "core.excludesFile", str(global_ignore))
    (repo / "token.secret").write_text("x", encoding="utf-8")

    code, out, _err = cli(repo, ["check", "sub/data.tmp"])
    assert code == 0
    assert "IGNORED" in out
    assert "sub/.gitignore" in out

    code, out, _err = cli(repo, ["check", "app.local"])
    assert code == 0
    assert "IGNORED" in out
    assert "exclude" in out

    code, out, _err = cli(repo, ["check", "token.secret"])
    assert code == 0
    assert "IGNORED" in out

    code, out, _err = cli(repo, ["sources"])
    assert code == 0
    assert "sub/.gitignore" in out
    assert "info/exclude" in out
    assert str(global_ignore) in out


def test_pattern_shapes_spaces_unicode_and_newlines(repo: Path, cli) -> None:
    (repo / ".gitignore").write_text("/only-root.txt\nsecret/\n**/*.tmp\n\\#hash\nhas?newline.txt\n*world.txt\n", encoding="utf-8")
    (repo / "only-root.txt").write_text("x", encoding="utf-8")
    nested = repo / "dir"
    nested.mkdir()
    (nested / "only-root.txt").write_text("x", encoding="utf-8")
    secret = repo / "secret"
    secret.mkdir()
    (secret / "key.txt").write_text("x", encoding="utf-8")
    (repo / "cache.tmp").write_text("x", encoding="utf-8")
    (nested / "cache.tmp").write_text("x", encoding="utf-8")
    (repo / "#hash").write_text("x", encoding="utf-8")
    (repo / "héllo world.txt").write_text("x", encoding="utf-8")
    (repo / "has\nnewline.txt").write_text("x", encoding="utf-8")

    assert "IGNORED" in cli(repo, ["check", "only-root.txt"])[1]
    assert "NOT_IGNORED" in cli(repo, ["check", "dir/only-root.txt"])[1]
    assert "IGNORED" in cli(repo, ["check", "secret/key.txt"])[1]
    explain = cli(repo, ["explain", "secret/key.txt"])[1]
    assert "parent" in explain.lower() or "traversable" in explain.lower()
    assert "IGNORED" in cli(repo, ["check", "dir/cache.tmp"])[1]
    assert "IGNORED" in cli(repo, ["check", "#hash"])[1]
    assert "IGNORED" in cli(repo, ["check", "héllo world.txt"])[1]
    code, out, _err = cli(repo, ["check", "has\nnewline.txt", "--json"])
    assert code == 0
    payload = json.loads(out)
    assert payload["result"]["classification"] == "IGNORED"
    assert "\n" in payload["result"]["relative_path"]
    assert "\033" not in out


def test_list_collapses_directories_and_can_expand(repo: Path, cli) -> None:
    (repo / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")
    build = repo / "build"
    build.mkdir()
    (build / "out.o").write_text("x", encoding="utf-8")
    (repo / "app.log").write_text("x", encoding="utf-8")
    (repo / "keep.txt").write_text("x", encoding="utf-8")

    code, out, _err = cli(repo, ["list"])
    assert code == 0
    assert "build/" in out
    assert "collapsed" in out.lower()
    assert "app.log" in out
    assert "keep.txt" not in out

    code, out, _err = cli(repo, ["list", "--files"])
    assert code == 0
    assert "build/out.o" in out

    code, out, _err = cli(repo, ["list", "--directories"])
    assert code == 0
    assert "build/" in out
    assert "app.log" not in out

    code, out, _err = cli(repo, ["list", "--extension", "log", "--group-by", "pattern"])
    assert code == 0
    assert "*.log" in out
    assert "build/" not in out


def test_tracked_audit_and_stats(repo: Path, cli) -> None:
    (repo / ".env").write_text("secret", encoding="utf-8")
    (repo / ".gitignore").write_text("# tracked first\n", encoding="utf-8")
    commit_all(repo)
    (repo / ".gitignore").write_text("*.log\n*.log\n*\n*.nope\nlogs/\n!logs/keep.txt\n", encoding="utf-8")
    (repo / "file.log").write_text("x", encoding="utf-8")
    before = (repo / ".gitignore").read_bytes()
    head = _tree(repo)

    code, out, _err = cli(repo, ["tracked"])
    assert code == 1
    assert ".env" in out
    assert "git rm --cached" in out
    assert (repo / ".env").read_text(encoding="utf-8") == "secret"

    code, out, _err = cli(repo, ["audit"])
    assert code == 1
    assert "audit.duplicates" in out
    assert "audit.redundant" in out or "covered by" in out
    assert "audit.unused" in out
    assert "*.nope" in out
    assert "logs/keep.txt" in out or "negation" in out
    assert (repo / ".gitignore").read_bytes() == before
    assert _tree(repo) == head

    code, out, _err = cli(repo, ["stats"])
    assert code == 0
    assert "Ignored Files" in out
    assert "Duplicate Rules" in out
    assert "collapsed" in out.lower()


def test_json_shape_and_exit_codes(repo: Path, cli, tmp_path: Path) -> None:
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (repo / "file.log").write_text("x", encoding="utf-8")
    for command in ("check", "explain"):
        code, out, err = cli(repo, [command, "file.log", "--json"])
        assert code == 0
        assert err == ""
        payload = json.loads(out)
        for key in ("version", "timestamp", "repository", "command", "result", "findings", "summary"):
            assert key in payload
        assert payload["command"] == command
        assert payload["result"]["classification"] == "IGNORED"
        assert "\033" not in out

    for command in ("list", "sources", "stats"):
        code, out, _err = cli(repo, [command, "--json"])
        payload = json.loads(out)
        assert payload["command"] == command
        assert code == 0

    code, _out, err = cli(repo, ["check"])
    assert code == 3
    assert err

    code, out, _err = cli(repo, ["--json", "nope"])
    assert code == 3
    assert json.loads(out)["error"]["code"] == "USAGE"

    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    code, out, _err = cli(repo, ["check", str(outside), "--json"])
    assert code == 0
    assert json.loads(out)["result"]["classification"] == "OUTSIDE_REPOSITORY"

    code, _out, err = cli(tmp_path, ["--repo", str(tmp_path / "missing"), "stats"])
    assert code == 4
    assert err

    empty = tmp_path / "empty"
    empty.mkdir()
    code, _out, _err = cli(empty, ["stats"])
    assert code == 4


def test_config_quiet_and_unreadable_source(repo: Path, cli) -> None:
    config = repo / "git-ignore-inspector.toml"
    config.write_text('output = "json"\nmax_results = 1\n', encoding="utf-8")
    (repo / ".gitignore").write_text("*.log\n*.tmp\n", encoding="utf-8")
    (repo / "a.log").write_text("x", encoding="utf-8")
    (repo / "b.tmp").write_text("x", encoding="utf-8")
    code, out, _err = cli(repo, ["list"])
    assert code == 1
    payload = json.loads(out)
    assert payload["result"]["truncated"] is True
    assert payload["result"]["returned"] == 1

    ignore = repo / ".gitignore"
    ignore.chmod(0)
    try:
        code, out, _err = cli(repo, ["--config", str(config), "sources"])
    finally:
        ignore.chmod(stat.S_IRUSR | stat.S_IWUSR)
    if os.geteuid() != 0:
        assert code == 2
        assert json.loads(out)["summary"]["fail"] >= 1


def _tree(root: Path) -> str:
    completed = __import__("subprocess").run(["git", "-C", str(root), "rev-parse", "HEAD^{tree}"], check=True, capture_output=True, text=True)
    return completed.stdout.strip()
