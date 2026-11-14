"""Read-only Git subprocess access.

Every invocation is an argument list. The runner refuses subcommands that
would update the repository, the index, or Git configuration.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from lupaxa.git_ignore_inspector.errors import ToolError

_MIN_VERSION = (2, 30, 0)
_ALLOWED = frozenset({"version", "rev-parse", "check-ignore", "ls-files", "config"})
_CONFIG_READS = frozenset({"--get", "--get-all", "--get-regexp", "--list", "--show-origin"})
_CONFIG_WRITES = frozenset(
    {"--unset", "--unset-all", "--add", "--replace-all", "--rename-section", "--remove-section"}
)
_TAKES_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def decode(data: bytes) -> str:
    """Decode Git output without dropping unusual filename bytes."""
    return data.decode("utf-8", "surrogateescape")


def encode(text: str) -> bytes:
    """Encode a path the same way :func:`decode` read it."""
    return text.encode("utf-8", "surrogateescape")


@dataclass
class GitResult:
    """Captured output from one Git invocation."""

    args: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def text(self) -> str:
        """Standard output decoded for unusual filenames."""
        return decode(self.stdout).strip()


class GitRunner:
    """Invoke the installed Git executable without updating the repository."""

    def __init__(self, *, verbose: bool = False) -> None:
        git = shutil.which("git")
        if git is None:
            raise ToolError(
                "GIT_NOT_FOUND",
                "Git was not found on PATH. Install Git 2.30 or newer and retry.",
                4,
            )
        self.git = git
        self.verbose = verbose
        self._assert_version()

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        stdin: bytes | None = None,
    ) -> GitResult:
        """Run one allowlisted Git command. Non-zero statuses are returned as data."""
        self._guard(args)
        command = [self.git, "--no-optional-locks", "-c", "core.pager=cat", "-c", "color.ui=false"]
        command.extend(args)
        env = os.environ.copy()
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_PAGER"] = "cat"
        if self.verbose:
            shown = " ".join(command)
            print(f"+ {shown}", file=sys.stderr)
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                input=stdin,
                capture_output=True,
                check=False,
                env=env,
            )
        except OSError as exc:
            raise ToolError("GIT_FAILED", f"Could not run Git: {exc}", 4) from exc
        return GitResult(
            args=tuple(args),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def _assert_version(self) -> None:
        result = self.run(["version"])
        if result.returncode != 0:
            raise ToolError(
                "GIT_FAILED",
                "Could not read the Git version.",
                4,
                {"stderr": decode(result.stderr)},
            )
        match = _VERSION_RE.search(decode(result.stdout))
        if match is None:
            raise ToolError(
                "GIT_UNSUPPORTED",
                f"Unrecognised Git version string: {decode(result.stdout).strip()}",
                4,
            )
        found = (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
        if found < _MIN_VERSION:
            readable = ".".join(str(part) for part in found)
            raise ToolError(
                "GIT_UNSUPPORTED",
                f"Git {readable} is older than the required 2.30.",
                4,
            )

    def _guard(self, args: Sequence[str]) -> None:
        sub = _subcommand(args)
        if sub not in _ALLOWED:
            raise ToolError(
                "FORBIDDEN_GIT",
                f"Refusing to run git {sub}. This tool only reads ignore state.",
                4,
            )
        if sub == "config":
            if any(token in _CONFIG_WRITES for token in args):
                raise ToolError(
                    "FORBIDDEN_GIT",
                    "Refusing to modify Git configuration.",
                    4,
                )
            if not any(token in _CONFIG_READS for token in args):
                raise ToolError(
                    "FORBIDDEN_GIT",
                    "Refusing a Git config command that is not a read.",
                    4,
                )


def _subcommand(args: Sequence[str]) -> str:
    index = 0
    while index < len(args):
        token = args[index]
        if token in _TAKES_VALUE:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    raise ToolError("FORBIDDEN_GIT", "Refusing a Git invocation with no subcommand.", 4)
