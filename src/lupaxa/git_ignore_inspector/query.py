"""Repository discovery and Git-backed ignore queries."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from lupaxa.git_ignore_inspector.errors import ToolError
from lupaxa.git_ignore_inspector.gitio import GitResult, GitRunner, decode, encode
from lupaxa.git_ignore_inspector.models import Classification, Rule
from lupaxa.git_ignore_inspector.patterns import (
    explain_constructs,
    explains_parent_exclusion,
    is_negation,
    read_ignore_file,
)

_RECORD_FIELDS = 4


@dataclass
class Repository:
    """The work tree selected for inspection."""

    root: Path
    git_dir: Path
    common_dir: Path
    exclude_file: Path
    global_excludes: Path | None
    global_configured: bool
    default_excludes: Path
    bare: bool
    worktree: bool
    ignore_case: bool | None
    runner: GitRunner


@dataclass
class PathStatus:
    """Ignore status of one user-supplied path."""

    requested: str
    classification: Classification
    tracked: bool
    ignored: bool
    relative_path: str | None
    rule: Rule | None
    explanation: list[str]
    nested_root: str | None = None
    message: str = ""


@dataclass
class ListedPath:
    """One ignored path from an enumeration."""

    path: str
    kind: str
    collapsed: bool
    rule: Rule | None


@dataclass
class SourceInfo:
    """One ignore configuration source."""

    source_type: str
    path: str
    exists: bool
    readable: bool
    patterns: int | None
    comments: int | None
    blanks: int | None
    in_working_tree: bool
    scope: str | None
    error: str | None = None
    applicable: bool | None = None


@dataclass
class Enumeration:
    """Ignored paths, with a note when the configured limit stopped the scan."""

    entries: list[ListedPath] = field(default_factory=list)
    truncated: bool = False
    total: int = 0


def open_repository(runner: GitRunner, start: Path) -> Repository:
    """Resolve ``start`` to a repository. Bare repositories are reported and rejected later."""
    if not start.exists():
        raise ToolError("INVALID_REPOSITORY", f"Repository path does not exist: {start}", 4)
    if not start.is_dir():
        raise ToolError("INVALID_REPOSITORY", f"Repository path is not a directory: {start}", 4)
    probe = runner.run(["-C", str(start), "rev-parse", "--is-inside-work-tree"])
    if probe.returncode != 0:
        detail = decode(probe.stderr).strip() or "The path is not inside a Git repository."
        raise ToolError("INVALID_REPOSITORY", detail, 4)
    bare = runner.run(["-C", str(start), "rev-parse", "--is-bare-repository"])
    is_bare = bare.text == "true"
    if is_bare:
        git_dir = _abs_path(runner, start, ["rev-parse", "--absolute-git-dir"])
        return Repository(
            root=start.resolve(),
            git_dir=git_dir,
            common_dir=git_dir,
            exclude_file=git_dir / "info" / "exclude",
            global_excludes=None,
            global_configured=False,
            default_excludes=_default_excludes(),
            bare=True,
            worktree=False,
            ignore_case=None,
            runner=runner,
        )
    root = _abs_path(runner, start, ["rev-parse", "--show-toplevel"])
    git_dir = _abs_path(runner, root, ["rev-parse", "--absolute-git-dir"])
    common = _abs_path(runner, root, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    exclude = _abs_path(
        runner,
        root,
        ["rev-parse", "--path-format=absolute", "--git-path", "info/exclude"],
    )
    global_path, configured = _global_excludes(runner, root)
    ignore_case = _ignore_case(runner, root)
    worktree = (root / ".git").is_file() or git_dir.resolve() != common.resolve()
    return Repository(
        root=root,
        git_dir=git_dir,
        common_dir=common,
        exclude_file=exclude,
        global_excludes=global_path,
        global_configured=configured,
        default_excludes=_default_excludes(),
        bare=False,
        worktree=worktree,
        ignore_case=ignore_case,
        runner=runner,
    )


def require_worktree(repo: Repository) -> None:
    """Stop commands that need a working tree when the repository is bare."""
    if repo.bare:
        raise ToolError(
            "BARE_REPOSITORY",
            "Bare repositories have no working tree, so ignore inspection is not supported.",
            4,
        )


def inspect_path(repo: Repository, requested: str, cwd: Path) -> PathStatus:
    """Classify one path. A missing path is still classified."""
    require_worktree(repo)
    absolute = _absolute(requested, cwd)
    boundary = _boundary_dir(absolute)
    try:
        inside = _is_relative_to(boundary.resolve(), repo.root.resolve())
    except OSError as exc:
        return PathStatus(
            requested=requested,
            classification=Classification.ERROR,
            tracked=False,
            ignored=False,
            relative_path=None,
            rule=None,
            explanation=[],
            message=f"Could not resolve {requested}: {exc}",
        )
    if not inside:
        return PathStatus(
            requested=requested,
            classification=Classification.OUTSIDE_REPOSITORY,
            tracked=False,
            ignored=False,
            relative_path=None,
            rule=None,
            explanation=[
                f"{requested} is outside the selected repository ({repo.root}).",
            ],
            message="The path is outside the selected repository.",
        )
    owning = _owning_root(repo.runner, boundary)
    if (
        owning is not None
        and owning.resolve() != repo.root.resolve()
        and not _same_path(absolute, owning)
    ):
        return PathStatus(
            requested=requested,
            classification=Classification.NESTED_REPOSITORY,
            tracked=False,
            ignored=False,
            relative_path=None,
            rule=None,
            nested_root=str(owning),
            explanation=[
                f"{requested} is inside a nested repository at {owning}.",
                "Nested repositories and submodules are a separate ignore boundary.",
                "Re-run with --repo pointing at that repository to inspect its rules.",
            ],
            message="The path belongs to a nested repository.",
        )
    directory = absolute.is_dir() and not absolute.is_symlink()
    relative = _relative(repo.root, absolute, directory=directory)
    tracked = _is_tracked(repo, relative)
    rule = _effective_rule(repo, relative)
    ignored = rule is not None and not rule.negation
    if tracked and ignored:
        classification = Classification.TRACKED_IGNORED
    elif tracked:
        classification = Classification.TRACKED
    elif ignored:
        classification = Classification.IGNORED
    else:
        classification = Classification.NOT_IGNORED
    return PathStatus(
        requested=requested,
        classification=classification,
        tracked=tracked,
        ignored=ignored,
        relative_path=relative,
        rule=rule,
        explanation=explain_status(repo, relative, tracked, ignored, rule),
        message=_status_message(classification, relative),
    )


def explain_status(
    repo: Repository,
    relative: str,
    tracked: bool,
    ignored: bool,
    rule: Rule | None,
) -> list[str]:
    """Build the prose explanation for one classified path."""
    tracked_text = "tracked" if tracked else "not tracked"
    if ignored:
        ignore_text = "ignored"
    elif rule is not None and rule.negation:
        ignore_text = "not ignored, because a negation is the effective rule"
    else:
        ignore_text = "not ignored"
    lines = [
        f"{relative} is {tracked_text} and is {ignore_text}.",
        f"Repository root: {repo.root}",
    ]
    if rule is None:
        lines.append("Git did not report an ignore rule for this path.")
        return lines
    lines.append(
        f"Effective pattern `{rule.pattern}` from {rule.source}:{rule.line} ({rule.source_type})."
    )
    lines.extend(explain_constructs(rule.pattern))
    if rule.negation:
        lines.append("The effective rule is a negation, so the path is not ignored.")
    if explains_parent_exclusion(rule.pattern, relative):
        lines.append(
            "This rule excludes a parent directory. A negation cannot re-include a file "
            "inside that directory unless each excluded parent is also made traversable."
        )
    if tracked and ignored:
        lines.append(
            "Ignore rules do not stop Git from tracking a file that is already in the index."
        )
    if repo.ignore_case is True:
        lines.append("This repository uses case-insensitive ignore matching (core.ignorecase).")
    elif repo.ignore_case is False:
        lines.append("This repository uses case-sensitive ignore matching.")
    return lines


def list_ignored(
    repo: Repository,
    *,
    files: bool,
    directories: bool,
    expand: bool,
    directory: str | None,
    extension: str | None,
    limit: int,
) -> Enumeration:
    """Enumerate ignored untracked paths. The default view keeps Git's collapsed directories."""
    require_worktree(repo)
    collapsed = _collapsed_ignored(repo)
    show_files = files or not directories
    show_directories = directories or not files
    expand_dirs = expand or files
    selected: list[tuple[str, str, bool]] = []
    for rel in collapsed:
        is_dir = rel.endswith("/")
        if is_dir and expand_dirs and show_files:
            for child in _walk_files(repo, rel):
                selected.append((child, "file", False))
        if is_dir and show_directories:
            selected.append((rel, "directory", not expand_dirs))
        elif not is_dir and show_files:
            selected.append((rel, "file", False))
    if directory:
        prefix = directory.strip("/")
        selected = [item for item in selected if _under(item[0], prefix)]
    if extension:
        suffix = extension if extension.startswith(".") else f".{extension}"
        selected = [item for item in selected if _extension_matches(item, suffix, repo.ignore_case)]
    total = len(selected)
    truncated = limit >= 0 and total > limit
    visible = selected if not truncated else selected[:limit]
    rules = _rules_for(repo, [item[0] for item in visible], no_index=False)
    entries = [
        ListedPath(
            path=rel,
            kind=kind,
            collapsed=collapsed_dir,
            rule=rules.get(rel.rstrip("/"), rules.get(rel)),
        )
        for rel, kind, collapsed_dir in visible
    ]
    for entry in entries:
        if entry.rule is None:
            entry.rule = rules.get(entry.path)
    return Enumeration(entries=entries, truncated=truncated, total=total)


def collect_sources(repo: Repository, target: str | None = None) -> list[SourceInfo]:
    """List ignore sources. ``target`` marks which nested gitignore files apply to a path."""
    require_worktree(repo)
    sources: list[SourceInfo] = []
    root_ignore = repo.root / ".gitignore"
    sources.append(_file_source("gitignore", root_ignore, in_working_tree=True, scope=""))
    for path in _nested_gitignores(repo.root):
        scope = path.parent.relative_to(repo.root).as_posix()
        sources.append(_file_source("gitignore", path, in_working_tree=True, scope=scope))
    sources.append(_file_source("exclude", repo.exclude_file, in_working_tree=False, scope=None))
    if repo.global_configured and repo.global_excludes is not None:
        sources.append(
            _file_source("global", repo.global_excludes, in_working_tree=False, scope=None)
        )
    else:
        sources.append(
            _file_source("global", repo.default_excludes, in_working_tree=False, scope=None)
        )
    if target is not None:
        relative = target.strip("/")
        for source in sources:
            source.applicable = _source_applies(source, relative)
    return sources


def ignored_census(repo: Repository) -> tuple[int, int, list[str]]:
    """Count collapsed ignored files and directories, and return those paths."""
    require_worktree(repo)
    collapsed = _collapsed_ignored(repo)
    directories = sum(1 for path in collapsed if path.endswith("/"))
    return len(collapsed) - directories, directories, collapsed


def tracked_matches(repo: Repository, *, limit: int) -> tuple[list[tuple[str, Rule]], bool, int]:
    """Return tracked paths whose effective rule would ignore them if they were untracked."""
    require_worktree(repo)
    paths = _tracked_paths(repo)
    truncated_input = limit >= 0 and len(paths) > limit
    scanned = paths if not truncated_input else paths[:limit]
    rules = _rules_for(repo, scanned, no_index=True)
    matched = [
        (path, rule)
        for path, rule in ((item, rules.get(item)) for item in scanned)
        if rule is not None and not rule.negation
    ]
    total = len(matched)
    truncated = truncated_input or (limit >= 0 and total > limit)
    visible = matched if limit < 0 or total <= limit else matched[:limit]
    return visible, truncated, total


def explain_status_message(status: PathStatus) -> str:
    """One-line summary used by the text renderer."""
    return status.message or status.classification.value


def _status_message(classification: Classification, relative: str) -> str:
    messages = {
        Classification.IGNORED: f"{relative} is ignored and is not tracked.",
        Classification.NOT_IGNORED: f"{relative} is not ignored and is not tracked.",
        Classification.TRACKED: f"{relative} is tracked by Git.",
        Classification.TRACKED_IGNORED: (
            f"{relative} is tracked by Git and would match an ignore rule if it were untracked."
        ),
    }
    return messages.get(classification, relative)


def _file_source(
    source_type: str, path: Path, *, in_working_tree: bool, scope: str | None
) -> SourceInfo:
    if not path.exists():
        return SourceInfo(
            source_type=source_type,
            path=str(path),
            exists=False,
            readable=False,
            patterns=0,
            comments=0,
            blanks=0,
            in_working_tree=in_working_tree,
            scope=scope,
        )
    parsed = read_ignore_file(path)
    if not parsed.readable:
        return SourceInfo(
            source_type=source_type,
            path=str(path),
            exists=True,
            readable=False,
            patterns=None,
            comments=None,
            blanks=None,
            in_working_tree=in_working_tree,
            scope=scope,
            error=parsed.error,
        )
    return SourceInfo(
        source_type=source_type,
        path=str(path),
        exists=True,
        readable=True,
        patterns=len(parsed.patterns),
        comments=parsed.comment_count,
        blanks=parsed.blank_count,
        in_working_tree=in_working_tree,
        scope=scope,
    )


def _source_applies(source: SourceInfo, relative: str) -> bool:
    if source.source_type in {"exclude", "global"}:
        return True
    scope = source.scope or ""
    if scope == "":
        return True
    return relative == scope or relative.startswith(scope + "/")


def _nested_gitignores(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        kept: list[str] = []
        for name in dirnames:
            child = current / name
            if name == ".git" or (child / ".git").exists():
                continue
            kept.append(name)
        dirnames[:] = kept
        if current != root and (current / ".git").exists():
            dirnames[:] = []
            continue
        if ".gitignore" in filenames and current != root:
            found.append(current / ".gitignore")
    found.sort()
    return found


def _collapsed_ignored(repo: Repository) -> list[str]:
    result = repo.runner.run(
        [
            "-C",
            str(repo.root),
            "-c",
            "core.quotepath=false",
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
        ]
    )
    _raise_git(result, "Could not list ignored paths.")
    return _split_paths(result.stdout)


def _tracked_paths(repo: Repository) -> list[str]:
    result = repo.runner.run(["-C", str(repo.root), "-c", "core.quotepath=false", "ls-files", "-z"])
    _raise_git(result, "Could not list tracked paths.")
    return _split_paths(result.stdout)


def _walk_files(repo: Repository, directory: str) -> list[str]:
    base = repo.root / directory
    found: list[str] = []
    if not base.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(base):
        current = Path(dirpath)
        dirnames[:] = [
            name for name in dirnames if name != ".git" and not (current / name / ".git").exists()
        ]
        for name in filenames:
            full = current / name
            if full.is_symlink() and not _is_relative_to(
                full.parent.resolve(), repo.root.resolve()
            ):
                continue
            found.append(_relative(repo.root, full, directory=False))
    found.sort()
    return found


def _is_tracked(repo: Repository, relative: str) -> bool:
    key = relative.rstrip("/")
    if key in {"", "."}:
        return False
    result = repo.runner.run(
        ["-C", str(repo.root), "-c", "core.quotepath=false", "ls-files", "-z", "--", key]
    )
    _raise_git(result, "Could not tell whether the path is tracked.")
    return key in set(_split_paths(result.stdout))


def _effective_rule(repo: Repository, relative: str) -> Rule | None:
    rules = _rules_for(repo, [relative], no_index=True)
    return rules.get(relative.rstrip("/")) or rules.get(relative)


def _rules_for(repo: Repository, paths: Iterable[str], *, no_index: bool) -> dict[str, Rule]:
    unique = list(dict.fromkeys(paths))
    found: dict[str, Rule] = {}
    step = 2000
    for offset in range(0, len(unique), step):
        chunk = unique[offset : offset + step]
        if not chunk:
            continue
        args = ["-C", str(repo.root), "check-ignore", "-v", "-z", "--stdin"]
        if no_index:
            args.insert(-1, "--no-index")
        payload = b"\0".join(encode(path) for path in chunk) + b"\0"
        result = repo.runner.run(args, stdin=payload)
        if result.returncode not in (0, 1):
            _raise_git(result, "git check-ignore failed.")
        for source, line, pattern, pathname in _parse_records(result.stdout):
            found[pathname] = _make_rule(repo, source, line, pattern)
            found[pathname.rstrip("/")] = found[pathname]
    return found


def _parse_records(payload: bytes) -> list[tuple[str, int, str, str]]:
    parts = payload.split(b"\0")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    records: list[tuple[str, int, str, str]] = []
    if len(parts) % _RECORD_FIELDS != 0:
        raise ToolError(
            "GIT_FAILED",
            "Git returned a check-ignore record that could not be parsed.",
            4,
        )
    for index in range(0, len(parts), _RECORD_FIELDS):
        source = decode(parts[index])
        line_text = decode(parts[index + 1])
        pattern = decode(parts[index + 2])
        pathname = decode(parts[index + 3])
        try:
            line = int(line_text)
        except ValueError as exc:
            raise ToolError(
                "GIT_FAILED",
                f"Git returned a non-numeric ignore line number: {line_text}",
                4,
            ) from exc
        records.append((source, line, pattern, pathname))
    return records


def _make_rule(repo: Repository, source: str, line: int, pattern: str) -> Rule:
    return Rule(
        source=source,
        source_type=_source_type(repo, source),
        line=line,
        pattern=pattern,
        negation=is_negation(pattern),
    )


def _source_type(repo: Repository, source: str) -> str:
    resolved = _resolve_source(repo, source)
    if _same_path(resolved, repo.exclude_file):
        return "exclude"
    global_path = repo.global_excludes if repo.global_configured else repo.default_excludes
    if global_path is not None and _same_path(resolved, global_path):
        return "global"
    if resolved.name == ".gitignore" or source.endswith(".gitignore"):
        return "gitignore"
    lowered = source.replace("\\", "/")
    if lowered.endswith("info/exclude"):
        return "exclude"
    return "global"


def _resolve_source(repo: Repository, source: str) -> Path:
    path = Path(source)
    if path.is_absolute():
        return path
    return repo.root / path


def _split_paths(payload: bytes) -> list[str]:
    return [decode(part) for part in payload.split(b"\0") if part]


def _raise_git(result: GitResult, message: str) -> None:
    if result.returncode == 0:
        return
    detail = decode(result.stderr).strip()
    if detail:
        message = f"{message} {detail}"
    raise ToolError("GIT_FAILED", message, 4)


def _abs_path(runner: GitRunner, cwd: Path, args: list[str]) -> Path:
    result = runner.run(["-C", str(cwd), *args])
    _raise_git(result, "Could not resolve a Git path.")
    return Path(result.text)


def _global_excludes(runner: GitRunner, root: Path) -> tuple[Path | None, bool]:
    result = runner.run(["-C", str(root), "config", "--get", "core.excludesFile"])
    if result.returncode != 0 or not result.text:
        return None, False
    raw = result.text
    if raw.startswith("~/"):
        return Path.home() / raw[2:], True
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    return path, True


def _ignore_case(runner: GitRunner, root: Path) -> bool | None:
    result = runner.run(["-C", str(root), "config", "--get", "core.ignorecase"])
    if result.returncode != 0 or not result.text:
        return None
    return result.text == "true"


def _default_excludes() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "git" / "ignore"


def _absolute(requested: str, cwd: Path) -> Path:
    path = Path(requested)
    if not path.is_absolute():
        path = cwd / path
    return Path(os.path.abspath(path))


def _boundary_dir(path: Path) -> Path:
    if path.is_symlink():
        return path.parent
    if path.exists():
        return path if path.is_dir() else path.parent
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def _owning_root(runner: GitRunner, directory: Path) -> Path | None:
    if not directory.exists():
        return None
    result = runner.run(["-C", str(directory), "rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        return None
    return Path(result.text)


def _relative(root: Path, path: Path, *, directory: bool) -> str:
    relative = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    if relative == ".":
        return "./"
    posix = Path(relative).as_posix()
    if directory and not posix.endswith("/"):
        posix += "/"
    return posix


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return os.path.abspath(left) == os.path.abspath(right)


def _under(relative: str, prefix: str) -> bool:
    path = relative.strip("/")
    return path == prefix or path.startswith(prefix + "/")


def _extension_matches(item: tuple[str, str, bool], suffix: str, ignore_case: bool | None) -> bool:
    relative, kind, _collapsed = item
    if kind == "directory":
        return False
    name = Path(relative).name
    if ignore_case:
        return name.casefold().endswith(suffix.casefold())
    return name.endswith(suffix)
