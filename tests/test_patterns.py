"""Pattern notes that do not need a repository."""

from __future__ import annotations

from lupaxa.git_ignore_inspector.patterns import (
    classify_line,
    duplicate_groups,
    explain_constructs,
    explains_parent_exclusion,
    read_ignore_file,
    redundant_patterns,
)


def test_classify_line_strips_unescaped_trailing_space(tmp_path) -> None:
    assert classify_line("  ") == ("blank", "")
    assert classify_line("# note") == ("comment", "# note")
    assert classify_line("*.log ")[0] == "pattern"
    assert classify_line("*.log ")[1] == "*.log"
    assert classify_line(r"file\ ")[1] == r"file\ "

    parsed = read_ignore_file(_write(tmp_path, "# c\n\n*.log\n*.log \n"))
    assert parsed.comment_count == 1
    assert parsed.blank_count == 1
    assert [line.text for line in parsed.patterns] == ["*.log", "*.log"]


def test_duplicates_and_redundant_rules(tmp_path) -> None:
    parsed = read_ignore_file(_write(tmp_path, "*\n*.log\n!keep.log\n*.tmp\n*.tmp\n"))
    groups = duplicate_groups(parsed)
    assert groups == [("*.tmp", [4, 5])]
    redundant = redundant_patterns(parsed)
    assert redundant[0][1] == "*.log"
    assert all(item[1] != "*.tmp" for item in redundant)


def test_explain_constructs_cover_the_common_forms() -> None:
    notes = " ".join(explain_constructs("!/build/**/cache/?"))
    assert "negation" in notes
    assert "anchors" in notes
    assert "**" in notes
    assert "*" in notes or "single character" in notes
    assert explains_parent_exclusion("logs/", "logs/keep.txt")
    assert not explains_parent_exclusion("!logs/keep.txt", "logs/keep.txt")


def _write(tmp_path, text: str):
    path = tmp_path / "ignore"
    path.write_text(text, encoding="utf-8")
    return path
