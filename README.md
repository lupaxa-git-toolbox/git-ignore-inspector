<p align="center">
  <a href="https://github.com/lupaxa-git-toolbox">
    <img src="https://raw.githubusercontent.com/the-lupaxa-project/brand-assets/master/logos/organisations/git-toolbox/readme-logo.png" alt="Git Toolbox" />
  </a>
</p>

<h1 align="center">Git Ignore Inspector</h1>

Git Ignore Inspector is a read-only command-line tool that shows why a path is
ignored, which rule matched, and where that rule is defined. It can also list
ignored paths, name the ignore sources in use, find tracked files that match
ignore rules, and audit the rules for problems.

Git's own matching is the authority. The tool does not implement a second
ignore engine, and it does not modify ignore files, Git configuration, the
index, or the work tree.

The PyPI name is `lupaxa-git-ignore-inspector`. The import path is
`lupaxa.git_ignore_inspector`. `lupaxa` is a namespace package. Installing
the package adds two commands for the same program: `git-ignore-inspector`
and `gii`.

## Install

```bash
pip install lupaxa-git-ignore-inspector
```

Requires Python 3.11 or newer and Git 2.30 or newer. There are no runtime
Python dependencies.

You can also run `python -m lupaxa.git_ignore_inspector`.

## Commands

No command is implied. `git-ignore-inspector` with no command prints help and
exits 3.

| Command   | What it Shows                                                              |
| --------- | -------------------------------------------------------------------------- |
| `check`   | Whether a path is ignored, tracked, outside the repository, or nested      |
| `explain` | The effective rule and what its pattern constructs mean                    |
| `list`    | Ignored untracked paths, with Git's collapsed directories by default       |
| `sources` | Root and nested `.gitignore` files, the exclude file, and global excludes  |
| `tracked` | Tracked files that would match an ignore rule if they were untracked       |
| `audit`   | Duplicates, reliable redundant rules, unused rules, and confusing patterns |
| `stats`   | Source, rule, and match counts from the same collapsed enumeration         |

An ignored file is a normal `check` result. It does not by itself produce a
non-zero exit status.

## Options

Global options work before or after the command.

```bash
git-ignore-inspector --repo ~/src/app check config/local.env
git-ignore-inspector explain src/cache/data.json --json
git-ignore-inspector list --directory src/ --extension .log
git-ignore-inspector list --files --group-by pattern
```

`--repo` selects a repository. Paths are still resolved from the current
working directory. `--json` writes one JSON document to stdout. `--verbose`
prints the Git invocations on stderr. `--quiet` shortens the text report.
`--color` is `auto`, `always`, or `never`. `NO_COLOR` and a non-interactive
terminal turn colour off.

`list --files` walks ignored directories and reports individual files.
`list --directories` reports ignored directories. The default listing keeps
the directory entries Git collapsed, and the text says so.

## Configuration

The tool runs without a configuration file. A TOML file can set the output
format, colour, audit checks, suppressed finding ids, and the enumeration
limit. It cannot change Git's ignore decision.

Search order: `--config`, `GIT_IGNORE_INSPECTOR_CONFIG`,
`./git-ignore-inspector.toml`, `<repository>/git-ignore-inspector.toml`, then
`$XDG_CONFIG_HOME/git-ignore-inspector/config.toml`.

```toml
output = "text"
color = "auto"
max_results = 10000
expand_ignored_directories = false
unused_rule_analysis = true

[audit]
enabled = ["duplicates", "redundant", "unused", "tracked", "confusing"]
suppressed = []
```

## Exit Codes

| Code | Meaning                                         |
| ---- | ----------------------------------------------- |
| 0    | Finished with no warnings or confirmed failures |
| 1    | Finished with warnings                          |
| 2    | Finished with a confirmed diagnostic failure    |
| 3    | Invalid arguments or configuration              |
| 4    | The command could not complete                  |

`list`, `sources`, and `stats` treat ordinary ignore data as information.
`tracked` reports matching files as warnings. `audit` uses warnings for
potential problems and failures for confirmed problems such as an unreadable
ignore file.

## JSON Output

Every command accepts `--json`. The document includes `version`, `timestamp`,
`repository`, `command`, `result`, `findings`, and `summary`. Findings use
`id`, `category`, `status`, `message`, `details`, and `recommendation`.
Status is `PASS`, `WARN`, `FAIL`, or `SKIP`.

<a href="https://github.com/the-lupaxa-project">
  <img src="https://raw.githubusercontent.com/the-lupaxa-project/brand-assets/master/logos/components/footer-for-child-orgs.svg" alt="The Lupaxa Project Footer" width="100%" />
</a>
