"""Command line for Git Ignore Inspector."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, NoReturn

from lupaxa.git_ignore_inspector.audit import analyse
from lupaxa.git_ignore_inspector.config import AppConfig, load_config
from lupaxa.git_ignore_inspector.errors import ToolError
from lupaxa.git_ignore_inspector.gitio import GitRunner
from lupaxa.git_ignore_inspector.models import Classification, Finding, Report, Status
from lupaxa.git_ignore_inspector.query import (
    SourceInfo,
    collect_sources,
    inspect_path,
    list_ignored,
    open_repository,
    require_worktree,
    tracked_matches,
)
from lupaxa.git_ignore_inspector.render import (
    Style,
    color_enabled,
    render_json,
    render_text,
    summary_counts,
)
from lupaxa.git_ignore_inspector.version import get_version

_COMMANDS = ("check", "explain", "list", "sources", "tracked", "audit", "stats")
_VALUED = ("--repo", "--config", "--color")
_FLAGS = {"--verbose", "--quiet", "--json", "--version", "-V", "--help", "-h"}
_HELP = """\
Git Ignore Inspector explains and audits Git ignore rules. It is read-only.

Usage:
  git-ignore-inspector [GLOBAL_OPTIONS] COMMAND [COMMAND_OPTIONS]
  gii [GLOBAL_OPTIONS] COMMAND [COMMAND_OPTIONS]

Commands:
  check PATH       Ignore status of one file or directory
  explain PATH     Why that path is ignored, tracked, or neither
  list             Ignored untracked files and collapsed directories
  sources          Ignore files that apply to the repository
  tracked          Tracked files that match an ignore rule
  audit            Duplicate, redundant, unused, and confusing rules
  stats            Counts for the current ignore configuration

Global options:
  --repo PATH                  Repository to inspect. Default: the current directory
  --config PATH                TOML configuration file
  --color auto|always|never    Default: auto. NO_COLOR disables colour
  --json                       Write a single JSON document to stdout
  --verbose                    Print the Git commands on stderr
  --quiet                      Shorter text. JSON is unchanged
  --version                    Print the version and exit 0
  --help                       Show help and exit 0

List options:
  --files                      Expand ignored directories and show files
  --directories                Show ignored directories
  --group-by pattern|source    Group the listing
  --extension EXT              Keep files with this extension
  --directory PATH             Keep paths under this directory

An ignored path is a normal check result and exits 0.
Exit status: 0 clean, 1 warnings, 2 confirmed failures, 3 usage or configuration, 4 could not complete.
"""


class _Parser(argparse.ArgumentParser):
    """Usage errors become ToolError so JSON mode stays a single document."""

    def error(self, message: str) -> NoReturn:
        raise ToolError("USAGE", message, 3)


def main(argv: list[str] | None = None) -> int:
    """Run one command. Returns a process exit status."""
    args = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in args
    try:
        if not args:
            sys.stdout.write(_HELP)
            return 3
        global_tokens, rest = _partition(args)
        options = _parse_globals(global_tokens)
        json_output = options.json
        if options.help and not rest:
            sys.stdout.write(_HELP)
            return 0
        if options.version and not rest:
            sys.stdout.write(get_version() + "\n")
            return 0
        if options.verbose and options.quiet:
            raise ToolError("USAGE", "--verbose and --quiet cannot be combined.", 3)
        if not rest or rest[0] not in _COMMANDS:
            if options.help:
                sys.stdout.write(_HELP)
                return 0
            name = rest[0] if rest else "(missing)"
            raise ToolError("USAGE", f"Unknown command {name}.", 3)
        command = rest[0]
        if "--help" in rest or "-h" in rest:
            sys.stdout.write(_HELP)
            return 0
        command_args = _parse_command(command, rest[1:])
        cwd = Path.cwd()
        repo_arg = Path(options.repo).expanduser() if options.repo else cwd
        if not repo_arg.is_absolute():
            repo_arg = cwd / repo_arg
        runner = GitRunner(verbose=options.verbose)
        repo = open_repository(runner, repo_arg)
        config = load_config(
            Path(options.config).expanduser() if options.config else None,
            cwd,
            None if repo.bare else repo.root,
        )
        if options.json:
            config.output = "json"
        if options.color:
            config.color = options.color
        report = _dispatch(command, command_args, repo, config, cwd)
        report.findings = [item for item in report.findings if item.id not in config.suppressed]
        report.summary = {**summary_counts(report.findings), **report.summary}
        _emit(report, config, quiet=options.quiet)
        return _exit_code(command, report)
    except ToolError as exc:
        return _fail(exc, json_output)
    except BrokenPipeError:
        return 0


def _dispatch(
    command: str, namespace: argparse.Namespace, repo: Any, config: AppConfig, cwd: Path
) -> Report:
    repository = None if repo.bare else str(repo.root)
    if command in {"check", "explain"}:
        require_worktree(repo)
        status = inspect_path(repo, namespace.path, cwd)
        if status.classification == Classification.ERROR:
            raise ToolError("INSPECT_FAILED", status.message or "Could not classify the path.", 4)
        result = {
            "target": status.requested,
            "relative_path": status.relative_path,
            "classification": status.classification.value,
            "tracked": status.tracked,
            "ignored": status.ignored,
            "rule": status.rule.to_json() if status.rule else None,
            "explanation": status.explanation if command == "explain" or status.explanation else [],
            "message": status.message,
            "nested_repository": status.nested_root,
            "worktree": repo.worktree,
        }
        if command == "check":
            result["explanation"] = status.explanation
        return Report(command=command, repository=repository, result=result)
    if command == "list":
        require_worktree(repo)
        expand = config.expand_ignored_directories or bool(namespace.files)
        listed = list_ignored(
            repo,
            files=bool(namespace.files),
            directories=bool(namespace.directories),
            expand=expand,
            directory=namespace.directory,
            extension=namespace.extension,
            limit=config.max_results,
        )
        entries = [_listed(entry) for entry in listed.entries]
        groups = _groups(entries, namespace.group_by)
        findings: list[Finding] = []
        if listed.truncated:
            findings.append(_truncated(config.max_results))
        return Report(
            command=command,
            repository=repository,
            result={
                "entries": entries,
                "groups": groups,
                "total": listed.total,
                "returned": len(entries),
                "truncated": listed.truncated,
                "enumeration": "files" if namespace.files else "collapsed",
            },
            findings=findings,
            summary={"total": listed.total, "returned": len(entries)},
        )
    if command == "sources":
        require_worktree(repo)
        sources = collect_sources(repo)
        findings = _source_findings(sources)
        return Report(
            command=command,
            repository=repository,
            result={"sources": [_source(item) for item in sources], "worktree": repo.worktree},
            findings=findings,
        )
    if command == "tracked":
        require_worktree(repo)
        matches, truncated, total = tracked_matches(repo, limit=config.max_results)
        entries = [
            {
                "path": path,
                "rule": rule.to_json(),
                "recommendation": f"git rm --cached -- {path}",
            }
            for path, rule in matches
        ]
        findings = [
            Finding(
                id="audit.tracked",
                category="tracked",
                status=Status.WARN,
                message=f"{path} is tracked and matches an ignore rule.",
                details={
                    "path": path,
                    "source": rule.source,
                    "line": rule.line,
                    "pattern": rule.pattern,
                },
                recommendation=f"git rm --cached -- {path}",
            )
            for path, rule in matches
        ]
        if not findings:
            findings.append(
                Finding(
                    id="audit.tracked",
                    category="tracked",
                    status=Status.PASS,
                    message="No tracked files match an ignore rule.",
                )
            )
        if truncated:
            findings.append(_truncated(config.max_results))
        return Report(
            command=command,
            repository=repository,
            result={"entries": entries, "total": total, "truncated": truncated},
            findings=findings,
            summary={"tracked_matches": total},
        )
    analysis = analyse(repo, config)
    if command == "stats":
        findings = [
            item
            for item in analysis.findings
            if item.status == Status.FAIL or item.id == "enum.truncated"
        ]
        return Report(
            command=command,
            repository=repository,
            result=_stats_result(analysis),
            findings=findings,
            summary=_stats_result(analysis),
        )
    return Report(
        command="audit",
        repository=repository,
        result={
            "duplicate_patterns": analysis.duplicate_groups,
            "redundant_patterns": analysis.redundant,
            "unused_rules": analysis.unused,
            "tracked_matches": analysis.tracked,
            "truncated": analysis.truncated,
            "ignore_sources": analysis.source_files,
            "ignore_rules": analysis.rules,
        },
        findings=analysis.findings,
        summary={
            "duplicate_patterns": analysis.duplicate_groups,
            "redundant_patterns": analysis.redundant,
            "unused_rules": analysis.unused,
            "tracked_matches": analysis.tracked,
        },
    )


def _stats_result(analysis: Any) -> dict[str, Any]:
    return {
        "ignore_sources": analysis.source_files,
        "ignore_rules": analysis.rules,
        "ignored_files": analysis.ignored_files,
        "ignored_directories": analysis.ignored_directories,
        "tracked_matches": analysis.tracked,
        "duplicate_patterns": analysis.duplicate_groups,
        "redundant_patterns": analysis.redundant,
        "unused_rules": analysis.unused,
        "top_patterns": [
            {"pattern": pattern, "count": count} for pattern, count in analysis.top_patterns
        ],
        "enumeration": "collapsed",
        "truncated": analysis.truncated,
    }


def _emit(report: Report, config: AppConfig, *, quiet: bool) -> None:
    if config.output == "json":
        render_json(report)
        return
    style_on = color_enabled(preference=config.color, json_output=False, stream=sys.stdout)
    render_text(report, Style(style_on), quiet=quiet)


def _exit_code(command: str, report: Report) -> int:
    statuses = {item.status for item in report.findings}
    if Status.FAIL in statuses:
        return 2
    if Status.WARN in statuses and command not in {"stats"}:
        return 1
    if command == "stats" and any(item.id == "enum.truncated" for item in report.findings):
        return 1
    return 0


def _fail(exc: ToolError, json_output: bool) -> int:
    if json_output or "--json" in sys.argv:
        report = Report(
            command="",
            repository=None,
            result={},
            error={"code": exc.code, "message": exc.message},
        )
        render_json(report)
    else:
        sys.stderr.write(f"{exc.message}\n")
    return exc.exit_code


def _partition(argv: list[str]) -> tuple[list[str], list[str]]:
    globals_: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in _VALUED:
            if index + 1 >= len(argv):
                raise ToolError("USAGE", f"{token} needs a value.", 3)
            globals_.extend(argv[index : index + 2])
            index += 2
            continue
        if any(token.startswith(f"{name}=") for name in _VALUED):
            globals_.append(token)
            index += 1
            continue
        if token in _FLAGS:
            globals_.append(token)
            index += 1
            continue
        rest.append(token)
        index += 1
    return globals_, rest


def _parse_globals(tokens: list[str]) -> argparse.Namespace:
    parser = _Parser(prog="git-ignore-inspector", add_help=False)
    parser.add_argument("--repo")
    parser.add_argument("--config")
    parser.add_argument("--color", choices=("auto", "always", "never"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--version", "-V", action="store_true")
    parser.add_argument("--help", "-h", action="store_true")
    return parser.parse_args(tokens)


def _parse_command(command: str, tokens: list[str]) -> argparse.Namespace:
    parser = _Parser(prog=f"git-ignore-inspector {command}", add_help=False)
    parser.add_argument("--help", "-h", action="store_true")
    if command in {"check", "explain"}:
        parser.add_argument("path")
    elif command == "list":
        parser.add_argument("--files", action="store_true")
        parser.add_argument("--directories", action="store_true")
        parser.add_argument("--group-by", choices=("pattern", "source"), dest="group_by")
        parser.add_argument("--extension")
        parser.add_argument("--directory")
    return parser.parse_args(tokens)


def _listed(entry: Any) -> dict[str, Any]:
    return {
        "path": entry.path,
        "kind": entry.kind,
        "collapsed": entry.collapsed,
        "rule": entry.rule.to_json() if entry.rule else None,
    }


def _source(item: SourceInfo) -> dict[str, Any]:
    return {
        "source_type": item.source_type,
        "path": item.path,
        "exists": item.exists,
        "readable": item.readable,
        "patterns": item.patterns,
        "comments": item.comments,
        "blanks": item.blanks,
        "in_working_tree": item.in_working_tree,
        "scope": item.scope,
        "applicable": item.applicable,
        "error": item.error,
    }


def _source_findings(sources: list[SourceInfo]) -> list[Finding]:
    findings: list[Finding] = []
    for source in sources:
        if source.exists and not source.readable:
            findings.append(
                Finding(
                    id="source.unreadable",
                    category="sources",
                    status=Status.FAIL,
                    message=f"Could not read {source.path}.",
                    details={"path": source.path, "error": source.error},
                    recommendation="Restore read permission and run the command again.",
                )
            )
    return findings


def _groups(entries: list[dict[str, Any]], group_by: str | None) -> list[dict[str, Any]] | None:
    if group_by is None:
        return None
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        rule = entry.get("rule") or {}
        if group_by == "pattern":
            key = str(rule.get("pattern") or "(no rule)")
        else:
            key = str(rule.get("source") or "(no source)")
        grouped[key].append(entry)
    return [{"key": key, "entries": grouped[key]} for key in grouped]


def _truncated(limit: int) -> Finding:
    return Finding(
        id="enum.truncated",
        category="limits",
        status=Status.WARN,
        message=f"Results were limited to {limit}. The listing is incomplete.",
        details={"max_results": limit},
        recommendation="Raise max_results in the configuration and run the command again.",
    )
