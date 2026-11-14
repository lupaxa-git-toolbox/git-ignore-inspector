"""Conservative audit of ignore sources.

Redundancy is limited to a later pattern covered by an earlier ``*`` or ``**``
in the same file, with no negation between them. Git's check-ignore result is
what decides tracked matches and ineffective negations.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from lupaxa.git_ignore_inspector.config import AppConfig
from lupaxa.git_ignore_inspector.models import Finding, Rule, Status
from lupaxa.git_ignore_inspector.patterns import (
    IgnoreFile,
    duplicate_groups,
    is_negation,
    pattern_body,
    read_ignore_file,
    redundant_patterns,
    suspicious_syntax,
)
from lupaxa.git_ignore_inspector.query import (
    ListedPath,
    Repository,
    _is_tracked,
    _nested_gitignores,
    _rules_for,
    ignored_census,
    list_ignored,
    tracked_matches,
)

_CHECKS = ("duplicates", "redundant", "unused", "tracked", "confusing")


@dataclass
class Analysis:
    """Shared facts for ``audit`` and ``stats``."""

    findings: list[Finding] = field(default_factory=list)
    source_files: int = 0
    rules: int = 0
    ignored_files: int = 0
    ignored_directories: int = 0
    tracked: int = 0
    duplicate_groups: int = 0
    redundant: int = 0
    unused: int | None = None
    top_patterns: list[tuple[str, int]] = field(default_factory=list)
    truncated: bool = False


def analyse(repo: Repository, config: AppConfig) -> Analysis:
    """Run the enabled checks. Disabled checks are reported as SKIP."""
    analysis = Analysis()
    ignored_files, ignored_directories, collapsed = ignored_census(repo)
    analysis.ignored_files = ignored_files
    analysis.ignored_directories = ignored_directories
    listed = list_ignored(
        repo,
        files=False,
        directories=False,
        expand=config.expand_ignored_directories,
        directory=None,
        extension=None,
        limit=config.max_results,
    )
    analysis.truncated = listed.truncated or (len(collapsed) > config.max_results)
    analysis.top_patterns = _top_patterns(listed.entries)

    tracked, tracked_truncated, tracked_total = tracked_matches(repo, limit=config.max_results)
    analysis.tracked = tracked_total
    analysis.truncated = analysis.truncated or tracked_truncated

    files = _audit_files(repo)
    analysis.source_files = sum(1 for _kind, path in files if path.exists())
    consulted = _consulted(repo, files, analysis, config)

    for kind, path in files:
        parsed = read_ignore_file(path)
        if not path.exists():
            continue
        if not parsed.readable:
            analysis.findings.append(
                Finding(
                    id="audit.unreadable",
                    category="sources",
                    status=Status.FAIL,
                    message=f"Could not read {path}.",
                    details={"path": str(path), "source_type": kind, "error": parsed.error},
                    recommendation="Restore read permission, then run the audit again.",
                )
            )
            continue
        analysis.rules += len(parsed.patterns)
        if path.resolve() in consulted:
            _pattern_checks(analysis, config, repo, path, parsed)

    if "tracked" in config.audit_enabled:
        if tracked:
            for tracked_path, rule in tracked:
                analysis.findings.append(_tracked_finding(tracked_path, rule))
        else:
            _pass(analysis, "audit.tracked", "tracked", "No tracked files match an ignore rule.")
    else:
        _skip(analysis, "audit.tracked", "tracked", "Tracked-file check is disabled.")

    if analysis.truncated:
        analysis.findings.append(
            Finding(
                id="enum.truncated",
                category="limits",
                status=Status.WARN,
                message=(
                    "Enumeration stopped at the configured limit. "
                    "Counts that depend on every match are incomplete."
                ),
                details={"max_results": config.max_results},
                recommendation="Raise max_results or narrow the repository and run the command again.",
            )
        )

    if "unused" in config.audit_enabled:
        if analysis.truncated:
            analysis.unused = None
            _skip(
                analysis,
                "audit.unused",
                "unused",
                "Unused-rule analysis was skipped because enumeration is incomplete.",
            )
        else:
            used = _used_keys(repo, listed.entries, tracked)
            unused_count = 0
            for path in consulted:
                parsed = read_ignore_file(path)
                for line in parsed.patterns:
                    key = (str(path.resolve()), line.number)
                    if key not in used:
                        unused_count += 1
                        analysis.findings.append(
                            Finding(
                                id="audit.unused",
                                category="unused",
                                status=Status.WARN,
                                message=(
                                    f"{_display(repo, path)}:{line.number} pattern `{line.text}` "
                                    "matched no enumerated path."
                                ),
                                details={
                                    "path": str(path),
                                    "line": line.number,
                                    "pattern": line.text,
                                },
                                recommendation=(
                                    "No action is required. A rule can be intentional even when "
                                    "nothing in the current working tree matches it."
                                ),
                            )
                        )
            analysis.unused = unused_count
            if unused_count == 0:
                _pass(
                    analysis,
                    "audit.unused",
                    "unused",
                    "Every ignore rule matched an enumerated path.",
                )
    else:
        analysis.unused = None
        _skip(analysis, "audit.unused", "unused", "Unused-rule analysis is disabled.")

    for name in _CHECKS:
        if name in {"tracked", "unused"}:
            continue
        if name not in config.audit_enabled:
            _skip(analysis, f"audit.{name}", name, f"The {name} check is disabled.")
            continue
        if not any(item.id == f"audit.{name}" for item in analysis.findings):
            _pass(analysis, f"audit.{name}", name, f"No {name} findings.")
    return analysis


def _pattern_checks(
    analysis: Analysis,
    config: AppConfig,
    repo: Repository,
    path: Path,
    parsed: IgnoreFile,
) -> None:
    if "duplicates" in config.audit_enabled:
        groups = duplicate_groups(parsed)
        analysis.duplicate_groups += len(groups)
        for pattern, numbers in groups:
            analysis.findings.append(
                Finding(
                    id="audit.duplicates",
                    category="duplicates",
                    status=Status.WARN,
                    message=(
                        f"Pattern `{pattern}` is repeated in {_display(repo, path)} "
                        f"on lines {', '.join(str(number) for number in numbers)}."
                    ),
                    details={
                        "path": str(path),
                        "pattern": pattern,
                        "lines": numbers,
                        "note": "Identical repetition is harmless and can be confusing.",
                    },
                    recommendation="Keep one copy of the pattern unless the repetition is deliberate.",
                )
            )
    if "redundant" in config.audit_enabled:
        redundant = redundant_patterns(parsed)
        analysis.redundant += len(redundant)
        for line_number, pattern, broader_line, broader in redundant:
            analysis.findings.append(
                Finding(
                    id="audit.redundant",
                    category="redundant",
                    status=Status.WARN,
                    message=(
                        f"{_display(repo, path)}:{line_number} pattern `{pattern}` is covered by "
                        f"`{broader}` on line {broader_line}."
                    ),
                    details={
                        "path": str(path),
                        "line": line_number,
                        "pattern": pattern,
                        "broader_line": broader_line,
                        "broader_pattern": broader,
                    },
                    recommendation="Review the later pattern. It was not removed.",
                )
            )
    if "confusing" not in config.audit_enabled:
        return
    for line in parsed.lines:
        if line.kind == "pattern" and line.raw != line.text:
            analysis.findings.append(
                Finding(
                    id="audit.confusing",
                    category="confusing",
                    status=Status.WARN,
                    message=(
                        f"{_display(repo, path)}:{line.number} has trailing whitespace. "
                        "Git ignores unescaped trailing spaces."
                    ),
                    details={"path": str(path), "line": line.number, "pattern": line.text},
                    recommendation="Remove the trailing space or escape it with a backslash.",
                )
            )
        if line.kind != "pattern":
            continue
        reason = suspicious_syntax(line.text)
        if reason:
            analysis.findings.append(
                Finding(
                    id="audit.confusing",
                    category="confusing",
                    status=Status.WARN,
                    message=f"{_display(repo, path)}:{line.number} {reason}",
                    details={"path": str(path), "line": line.number, "pattern": line.text},
                    recommendation="Check the pattern against the Git ignore rules you intended.",
                )
            )
        if is_negation(line.text):
            _negation_probe(analysis, repo, path, line.number, line.text)


def _negation_probe(
    analysis: Analysis, repo: Repository, path: Path, line: int, pattern: str
) -> None:
    relative = _probe_path(repo, path, pattern)
    if relative is None:
        return
    rule = _rules_for(repo, [relative], no_index=True).get(relative.rstrip("/"))
    if rule is None or rule.negation:
        return
    winner = _resolve(repo, rule.source)
    if winner == path.resolve() and rule.line == line:
        return
    analysis.findings.append(
        Finding(
            id="audit.confusing",
            category="confusing",
            status=Status.WARN,
            message=(
                f"{_display(repo, path)}:{line} negation `{pattern}` does not re-include "
                f"{relative}. {rule.source}:{rule.line} pattern `{rule.pattern}` still excludes it."
            ),
            details={
                "path": str(path),
                "line": line,
                "pattern": pattern,
                "probe": relative,
                "effective_source": rule.source,
                "effective_line": rule.line,
                "effective_pattern": rule.pattern,
            },
            recommendation=(
                "Negate each excluded parent directory before re-including a file inside it. "
                "This tool will not edit the ignore file."
            ),
        )
    )


def _probe_path(repo: Repository, path: Path, pattern: str) -> str | None:
    body = pattern_body(pattern)
    if any(token in body for token in ("*", "?", "[")):
        return None
    if "/" not in body:
        return None
    body = body.lstrip("/")
    if body.endswith("/"):
        body = body[:-1]
        if not body:
            return None
    scope = ""
    if path.name == ".gitignore":
        scope = path.parent.relative_to(repo.root).as_posix()
        if scope == ".":
            scope = ""
    if scope:
        return f"{scope}/{body}"
    return body


def _consulted(
    repo: Repository,
    files: list[tuple[str, Path]],
    analysis: Analysis,
    config: AppConfig,
) -> set[Path]:
    consulted: set[Path] = set()
    for kind, path in files:
        if not path.exists():
            continue
        if kind != "gitignore":
            consulted.add(path.resolve())
            continue
        relative = path.relative_to(repo.root).as_posix()
        rule = _rules_for(repo, [relative], no_index=False).get(relative)
        if rule is not None and not rule.negation and not _is_tracked(repo, relative):
            if "confusing" in config.audit_enabled:
                analysis.findings.append(
                    Finding(
                        id="audit.confusing",
                        category="confusing",
                        status=Status.WARN,
                        message=(
                            f"{relative} is ignored, so Git does not consult the rules inside it."
                        ),
                        details={"path": str(path), "effective_pattern": rule.pattern},
                        recommendation=(
                            "Re-include the parent directories before relying on this file. "
                            "This tool will not edit it."
                        ),
                    )
                )
            continue
        consulted.add(path.resolve())
    return consulted


def _audit_files(repo: Repository) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = [("gitignore", repo.root / ".gitignore")]
    files.extend(("gitignore", path) for path in _nested_gitignores(repo.root))
    files.append(("exclude", repo.exclude_file))
    global_path = repo.global_excludes if repo.global_configured else repo.default_excludes
    if global_path is not None:
        files.append(("global", global_path))
    return files


def _used_keys(
    repo: Repository,
    entries: Sequence[ListedPath],
    tracked: Sequence[tuple[str, Rule]],
) -> set[tuple[str, int]]:
    used: set[tuple[str, int]] = set()
    for entry in entries:
        if entry.rule is not None:
            used.add((_resolve(repo, entry.rule.source), entry.rule.line))
    for _path, rule in tracked:
        used.add((_resolve(repo, rule.source), rule.line))
    return used


def _resolve(repo: Repository, source: str) -> str:
    path = Path(source)
    if not path.is_absolute():
        path = repo.root / path
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _display(repo: Repository, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo.root.resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


def _top_patterns(entries: Sequence[ListedPath]) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for entry in entries:
        if entry.rule is not None:
            counter[entry.rule.pattern] += 1
    return counter.most_common(5)


def _tracked_finding(path: str, rule: Rule) -> Finding:
    return Finding(
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
        recommendation=(
            f"Review whether this file should remain tracked. "
            f"One way to stop tracking it, which this tool will not run, is: "
            f"git rm --cached -- {path}"
        ),
    )


def _pass(analysis: Analysis, identifier: str, category: str, message: str) -> None:
    analysis.findings.append(
        Finding(id=identifier, category=category, status=Status.PASS, message=message)
    )


def _skip(analysis: Analysis, identifier: str, category: str, message: str) -> None:
    analysis.findings.append(
        Finding(id=identifier, category=category, status=Status.SKIP, message=message)
    )
