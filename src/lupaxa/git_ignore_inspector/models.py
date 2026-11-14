"""Diagnostic records shared by every command."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    """Outcome of one diagnostic check."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


class Classification(StrEnum):
    """Ignore status of one path. Git's check-ignore result is authoritative."""

    IGNORED = "IGNORED"
    NOT_IGNORED = "NOT_IGNORED"
    TRACKED = "TRACKED"
    TRACKED_IGNORED = "TRACKED_IGNORED"
    OUTSIDE_REPOSITORY = "OUTSIDE_REPOSITORY"
    NESTED_REPOSITORY = "NESTED_REPOSITORY"
    ERROR = "ERROR"


@dataclass
class Finding:
    """One diagnostic check result."""

    id: str
    category: str
    status: Status
    message: str
    details: dict[str, Any] | None = None
    recommendation: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Return the stable JSON object for this finding."""
        return {
            "id": self.id,
            "category": self.category,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
            "recommendation": self.recommendation,
        }


@dataclass(frozen=True)
class Rule:
    """The ignore rule Git reported as the effective match."""

    source: str
    source_type: str
    line: int
    pattern: str
    negation: bool

    def to_json(self) -> dict[str, Any]:
        """Return the stable JSON object for this rule."""
        return {
            "source": self.source,
            "source_type": self.source_type,
            "line": self.line,
            "pattern": self.pattern,
            "negation": self.negation,
        }


@dataclass
class Report:
    """Machine-readable result of one command."""

    command: str
    repository: str | None
    result: dict[str, Any]
    findings: list[Finding] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    error: dict[str, str] | None = None
