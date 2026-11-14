"""Static reading of ignore files and plain-language pattern notes.

Git remains the authority for whether a path matches. These helpers explain
the effective pattern and support only redundancy checks that are reliable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ParsedLine:
    """One physical line of an ignore file."""

    number: int
    kind: str
    text: str
    raw: str


@dataclass(frozen=True)
class IgnoreFile:
    """A readable ignore source split into comments, blanks, and patterns."""

    path: Path
    lines: tuple[ParsedLine, ...]
    readable: bool
    error: str | None = None

    @property
    def patterns(self) -> tuple[ParsedLine, ...]:
        """Active pattern lines, in file order."""
        return tuple(line for line in self.lines if line.kind == "pattern")

    @property
    def comment_count(self) -> int:
        """Number of comment lines."""
        return sum(1 for line in self.lines if line.kind == "comment")

    @property
    def blank_count(self) -> int:
        """Number of blank lines."""
        return sum(1 for line in self.lines if line.kind == "blank")


def read_ignore_file(path: Path) -> IgnoreFile:
    """Parse an ignore file. Unreadable files stay in the report as such."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return IgnoreFile(path=path, lines=(), readable=False, error=str(exc))
    text = raw.decode("utf-8", "surrogateescape")
    parsed: list[ParsedLine] = []
    for number, raw_line in enumerate(text.splitlines(), start=1):
        kind, body = classify_line(raw_line)
        parsed.append(ParsedLine(number=number, kind=kind, text=body, raw=raw_line))
    return IgnoreFile(path=path, lines=tuple(parsed), readable=True)


def classify_line(raw_line: str) -> tuple[str, str]:
    """Classify one line the way Git skips comments, blanks, and trailing spaces."""
    text = _strip_trailing_space(raw_line)
    if text == "":
        return "blank", ""
    if text.startswith("#"):
        return "comment", text
    return "pattern", text


def is_negation(pattern: str) -> bool:
    """Return whether the pattern re-includes paths."""
    return pattern.startswith("!") and not pattern.startswith(r"\!")


def pattern_body(pattern: str) -> str:
    """Return the pattern without a leading negation marker."""
    if is_negation(pattern):
        return pattern[1:]
    return pattern


def explain_constructs(pattern: str) -> list[str]:
    """Describe the constructs in one effective pattern."""
    notes: list[str] = []
    body = pattern_body(pattern)
    if is_negation(pattern):
        notes.append(
            "The leading ! is a negation. It re-includes a path that an earlier rule excluded."
        )
    if body.endswith("/") and not body.endswith(r"\/"):
        notes.append("The trailing / limits the pattern to directories.")
    if body.startswith("/"):
        notes.append(
            "The leading / anchors the pattern to the directory that contains this ignore file."
        )
    elif "/" in body.strip("/"):
        notes.append(
            "A slash inside the pattern anchors it to the directory that contains this ignore file."
        )
    else:
        notes.append(
            "The pattern is not anchored, so it can match in any directory under the ignore file."
        )
    if "**" in body:
        notes.append("** matches across directory boundaries.")
    if "*" in body.replace("**", ""):
        notes.append("* matches any string except a directory separator.")
    if "?" in body:
        notes.append("? matches a single character except a directory separator.")
    if "\\" in body:
        notes.append("A backslash escapes the next character so Git matches it literally.")
    return notes


def explains_parent_exclusion(pattern: str, relative_path: str) -> bool:
    """Return whether the effective rule excludes a parent of the target."""
    if is_negation(pattern):
        return False
    body = pattern_body(pattern).lstrip("/")
    directory = body.endswith("/") and not body.endswith(r"\/")
    literal = body[:-1] if directory else body
    if not literal or any(token in literal for token in ("*", "?", "[")):
        return directory and relative_path.rstrip("/") != literal.rstrip("/")
    relative = relative_path.strip("/")
    prefix = literal.strip("/")
    if not prefix or relative == prefix:
        return False
    return relative.startswith(prefix + "/")


def duplicate_groups(parsed: IgnoreFile) -> list[tuple[str, list[int]]]:
    """Return patterns repeated in one file, with every line number."""
    grouped: dict[str, list[int]] = {}
    for line in parsed.patterns:
        grouped.setdefault(line.text, []).append(line.number)
    return [(pattern, numbers) for pattern, numbers in grouped.items() if len(numbers) > 1]


def redundant_patterns(parsed: IgnoreFile) -> list[tuple[int, str, int, str]]:
    """Return later patterns covered by an earlier ``*`` or ``**`` in the same file.

    Each item is ``(line, pattern, broader_line, broader_pattern)``. A negation
    anywhere between the two rules suppresses the conclusion.
    """
    found: list[tuple[int, str, int, str]] = []
    broader: ParsedLine | None = None
    for line in parsed.lines:
        if line.kind != "pattern":
            continue
        if (
            broader is not None
            and not is_negation(line.text)
            and line.text != broader.text
            and _covered(broader.text, line.text)
        ):
            found.append((line.number, line.text, broader.number, broader.text))
        if is_negation(line.text):
            broader = None
            continue
        if pattern_body(line.text) in {"*", "**", "**/*"}:
            broader = line
    return found


def _covered(broader: str, later: str) -> bool:
    body = pattern_body(broader)
    later_body = pattern_body(later)
    if is_negation(later):
        return False
    if body in {"**", "**/*"}:
        return True
    if body == "*":
        return "/" not in later_body.strip("/")
    return False


def _strip_trailing_space(raw_line: str) -> str:
    end = len(raw_line)
    while end > 0 and raw_line[end - 1] == " ":
        slashes = 0
        index = end - 2
        while index >= 0 and raw_line[index] == "\\":
            slashes += 1
            index -= 1
        if slashes % 2 == 1:
            break
        end -= 1
    return raw_line[:end]


def suspicious_syntax(pattern: str) -> str | None:
    """Return a short reason when a pattern is reliably suspicious."""
    body = pattern_body(pattern)
    if pattern == "!":
        return "A negation with no pattern does not re-include anything."
    if body.startswith("./"):
        return "A leading ./ is not a directory anchor. Git treats the dot as a literal."
    if "//" in body:
        return "The pattern contains an empty path segment (//)."
    if "***" in body:
        return "The pattern contains ***, which is not a recursive-wildcard form."
    return None
