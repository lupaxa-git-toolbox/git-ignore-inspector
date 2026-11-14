"""Human-readable and JSON reports. JSON never includes terminal styling."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

from lupaxa.git_ignore_inspector.models import Finding, Report, Status
from lupaxa.git_ignore_inspector.version import get_version


class Style:
    """ANSI styling that stays off for JSON, ``NO_COLOR``, and non-terminals."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def paint(self, text: str, code: str) -> str:
        """Wrap ``text`` when styling is enabled."""
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def status(self, status: str) -> str:
        """Colour a diagnostic status token."""
        colours = {
            Status.PASS.value: "32",
            Status.WARN.value: "33",
            Status.FAIL.value: "31",
            Status.SKIP.value: "90",
        }
        return self.paint(status, colours.get(status, "36"))

    def title(self, text: str) -> str:
        """Colour a section heading."""
        return self.paint(text, "96")


def color_enabled(*, preference: str, json_output: bool, stream: TextIO) -> bool:
    """Decide whether text output may use colour."""
    if json_output or os.environ.get("NO_COLOR"):
        return False
    if preference == "never":
        return False
    if preference == "always":
        return True
    return stream.isatty()


def render_json(report: Report, *, stream: TextIO | None = None) -> None:
    """Write one JSON document and nothing else."""
    output = stream or sys.stdout
    payload = {
        "version": get_version(),
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repository": report.repository,
        "command": report.command,
        "result": report.result,
        "findings": [finding.to_json() for finding in report.findings],
        "summary": report.summary,
    }
    if report.error is not None:
        payload["error"] = report.error
    json.dump(payload, output, indent=2, ensure_ascii=False)
    output.write("\n")


def render_text(report: Report, style: Style, *, quiet: bool, stream: TextIO | None = None) -> None:
    """Write the human-readable report for one command."""
    output = stream or sys.stdout
    if report.error is not None:
        output.write(f"{report.error.get('message', 'Command failed.')}\n")
        return
    heading = {
        "check": "Check",
        "explain": "Explain",
        "list": "Ignored Paths",
        "sources": "Ignore Sources",
        "tracked": "Tracked Files Matching Ignore Rules",
        "audit": "Ignore Audit",
        "stats": "Git Ignore Statistics",
    }.get(report.command, report.command)
    if not quiet:
        output.write(f"{style.title(heading)}\n")
        output.write(f"{style.title('=' * 41)}\n\n")
        if report.repository:
            output.write(f"Repository: {report.repository}\n")
    writer = {
        "check": _check,
        "explain": _check,
        "list": _list,
        "sources": _sources,
        "tracked": _tracked,
        "audit": _audit,
        "stats": _stats,
    }.get(report.command, _audit)
    writer(report, style, quiet=quiet, output=output)


def summary_counts(findings: list[Finding]) -> dict[str, int]:
    """Count findings by status."""
    counts = {status.value.lower(): 0 for status in Status}
    for finding in findings:
        counts[finding.status.value.lower()] += 1
    return counts


def _check(report: Report, style: Style, *, quiet: bool, output: TextIO) -> None:
    result = report.result
    classification = str(result.get("classification", ""))
    output.write(f"Classification: {style.paint(classification, '36')}\n")
    message = result.get("message")
    if isinstance(message, str) and message:
        output.write(f"\n{message}\n")
    rule = result.get("rule")
    if isinstance(rule, dict):
        output.write("\nMatching rule:\n")
        output.write(f"  {rule.get('source')}:{rule.get('line')}\n")
        output.write(f"  Pattern: {rule.get('pattern')}\n")
        if not quiet:
            output.write(f"  Negation: {'yes' if rule.get('negation') else 'no'}\n")
    explanation = result.get("explanation")
    if not quiet and isinstance(explanation, list) and explanation:
        output.write("\nExplanation:\n")
        for line in explanation:
            output.write(f"  {line}\n")
    _recommendations(report.findings, output)


def _list(report: Report, style: Style, *, quiet: bool, output: TextIO) -> None:
    del style, quiet
    groups = report.result.get("groups")
    if isinstance(groups, list):
        for group in groups:
            output.write(f"\n{group.get('key')}\n")
            for entry in group.get("entries", []):
                _list_entry(entry, output, indent="  ")
        return
    entries = report.result.get("entries", [])
    if not entries:
        output.write("No ignored paths matched.\n")
        return
    for entry in entries:
        _list_entry(entry, output, indent="")


def _list_entry(entry: dict[str, Any], output: TextIO, *, indent: str) -> None:
    path = entry.get("path", "")
    output.write(f"{indent}{path}\n")
    rule = entry.get("rule")
    if isinstance(rule, dict):
        output.write(f"{indent}  {rule.get('source')}:{rule.get('line')}  {rule.get('pattern')}\n")
    if entry.get("collapsed"):
        output.write(f"{indent}  Directory collapsed by Git; contents were not expanded.\n")


def _sources(report: Report, style: Style, *, quiet: bool, output: TextIO) -> None:
    del style, quiet
    for source in report.result.get("sources", []):
        output.write(f"\n[{source.get('source_type')}] {source.get('path')}\n")
        output.write(f"  Exists: {'yes' if source.get('exists') else 'no'}\n")
        output.write(f"  Readable: {'yes' if source.get('readable') else 'no'}\n")
        output.write(f"  Patterns: {_count(source.get('patterns'))}\n")
        output.write(f"  Comments: {_count(source.get('comments'))}\n")
        output.write(f"  Blank lines: {_count(source.get('blanks'))}\n")
        if source.get("scope") not in (None, ""):
            output.write(f"  Scope: {source.get('scope')}\n")
        if source.get("applicable") is not None:
            output.write(f"  Applicable: {'yes' if source.get('applicable') else 'no'}\n")
        if source.get("error"):
            output.write(f"  Error: {source.get('error')}\n")


def _tracked(report: Report, style: Style, *, quiet: bool, output: TextIO) -> None:
    del quiet
    entries = report.result.get("entries", [])
    if not entries:
        output.write("No tracked files match an ignore rule.\n")
        return
    for entry in entries:
        output.write(f"\n{style.status('WARN')} {entry.get('path')}\n\n")
        output.write("File is tracked by Git.\n\n")
        rule = entry.get("rule") or {}
        output.write("Matching rule:\n")
        output.write(f"  {rule.get('source')}:{rule.get('line')}\n")
        output.write(f"  Pattern: {rule.get('pattern')}\n\n")
        output.write("Recommendation:\n")
        output.write("  Review whether this file should remain tracked.\n")
        output.write(f"  git rm --cached -- {entry.get('path')}\n")


def _audit(report: Report, style: Style, *, quiet: bool, output: TextIO) -> None:
    if not report.findings:
        output.write("No findings.\n")
        return
    for finding in report.findings:
        if quiet and finding.status == Status.PASS:
            continue
        output.write(f"\n[{style.status(finding.status.value)}] {finding.id}\n")
        output.write(f"{finding.message}\n")
        if finding.recommendation and not quiet:
            output.write(f"Recommendation:\n  {finding.recommendation}\n")


def _stats(report: Report, style: Style, *, quiet: bool, output: TextIO) -> None:
    del style, quiet
    result = report.result
    rows = (
        ("Ignore Sources", result.get("ignore_sources")),
        ("Ignore Rules", result.get("ignore_rules")),
        ("Ignored Files", result.get("ignored_files")),
        ("Ignored Directories", result.get("ignored_directories")),
        ("Tracked Matches", result.get("tracked_matches")),
    )
    for label, value in rows:
        output.write(f"{label + ':':<24}{_count(value):>6}\n")
    output.write("\nTop Matching Patterns:\n")
    patterns = result.get("top_patterns") or []
    if not patterns:
        output.write("  (none)\n")
    for item in patterns:
        output.write(f"  {item.get('pattern'):<22}{item.get('count')}\n")
    output.write("\nPotential Issues:\n")
    issues = (
        ("Duplicate Rules", result.get("duplicate_patterns")),
        ("Redundant Rules", result.get("redundant_patterns")),
        ("Unused Rules", result.get("unused_rules")),
        ("Tracked Matches", result.get("tracked_matches")),
    )
    for label, value in issues:
        output.write(f"  {label + ':':<22}{_count(value):>6}\n")
    if result.get("truncated"):
        output.write("\nEnumeration reached the configured limit. Some figures are incomplete.\n")
    if result.get("enumeration"):
        output.write(
            "\nIgnored files and directories use Git's collapsed listing. "
            "A collapsed directory counts as one directory, not as the files inside it.\n"
        )


def _recommendations(findings: list[Finding], output: TextIO) -> None:
    for finding in findings:
        if finding.recommendation:
            output.write(f"\nRecommendation:\n  {finding.recommendation}\n")


def _count(value: object) -> str:
    if value is None:
        return "n/a"
    return str(value)
