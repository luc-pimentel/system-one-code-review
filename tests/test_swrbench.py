import pytest

from s1cr.swrbench import FileDiff, Row, changed_lines, commit_files, fault_files, to_row

PATCH = (
    "@@ -1,3 +1,3 @@\n context\n-old = compute_total(items)\n+new = compute_total(items, tax=True)\n context"
)


def commit(sha: str, files: dict[str, str], message: str = "Change things") -> dict:
    return {"sha": sha, "message": message, "diff": [{"file": f, "patch": p} for f, p in files.items()]}


def record(commits: list[dict], changes: list[dict] | None = None, **fields) -> dict:
    base = {
        "instance_id": "owner__repo-1",
        "repo": "owner/repo",
        "created_at": "2024-03-01T00:00:00+00:00",
        "pr_title": "Add tax",
        "pr_statement": "Adds tax to totals.",
        "change_introduced": bool(changes),
        "base_commit": "b" * 40,
        "changes": changes or [],
        "pr_commits": commits,
    }
    return base | fields


def change(code: str, snippet: str) -> dict:
    return {"change_type": code, "change_introducing": {"code_snippet": snippet, "commit_sha": "c" * 40}}


def no_compare(repo, base, head):
    raise AssertionError("a single commit needs no comparison")


def test_changed_lines_skip_the_file_headers():
    assert changed_lines("--- a/x\n+++ b/x\n" + PATCH) == 2


def test_a_commit_s_parts_are_grouped_per_file():
    files = commit_files(commit("1", {"a.py": PATCH}) | {"diff": [{"file": "a.py", "patch": PATCH}] * 2})
    assert [(f.path, f.changed) for f in files] == [("a.py", 4)]


def test_a_single_commit_is_the_reviewed_diff_and_labels_carry_over():
    row = to_row(record([commit("1", {"a.py": PATCH})], [change("F.2 Logic", "a.py\n" + PATCH)]), no_compare)
    assert row.excluded is None
    assert [f.path for f in row.files] == ["a.py"]
    assert row.changes_requested
    assert row.functional
    assert row.categories == ["F.2"]
    assert row.fault_files == ["a.py"]
    assert row.era == "2024–25"


def test_several_commits_are_compared_on_github():
    seen = []

    def compare(repo, base, head):
        seen.append((repo, base, head))
        return [FileDiff("a.py", PATCH, 2)], 2

    commits = [commit("1", {"a.py": PATCH}), commit("2", {"a.py": PATCH})]
    row = to_row(record(commits, [change("E.1.1 Textual Changes", "a.py\n" + PATCH)]), compare)
    assert seen == [("owner/repo", "b" * 40, "2")]
    assert row.excluded is None
    assert not row.functional


def test_pull_requests_that_cannot_be_read_as_reviewed_are_left_out():
    two = [commit("1", {"a.py": PATCH}), commit("2", {"a.py": PATCH})]
    assert (
        "merge commit"
        in to_row(record([commit("1", {"a.py": PATCH}, "Merge branch 'main'")]), no_compare).excluded
    )

    def mismatch(repo, base, head):
        return [FileDiff("a.py", PATCH, 2)], 5

    assert "holds 5 commits" in to_row(record(two), mismatch).excluded

    def gone(repo, base, head):
        raise RuntimeError("HTTP 404")

    assert "could not compare" in to_row(record(two), gone).excluded
    huge = "@@ -1 +1 @@\n+" + "x" * 100_000
    assert to_row(record([commit("1", {"a.py": huge})]), no_compare).excluded == "too large for one Jev call"


def test_fault_files_come_from_the_snippet_path_or_a_line_only_one_file_has():
    files = [FileDiff("a.py", PATCH, 2), FileDiff("b.py", "@@ -1 +1 @@\n+print('unrelated line here')", 1)]
    by_path = record([], [change("F.2 Logic", "b.py\n@@ -1 +1 @@")])
    assert fault_files(by_path, files) == ["b.py"]
    by_line = record([], [change("F.2 Logic", "+new = compute_total(items, tax=True)")])
    assert fault_files(by_line, files) == ["a.py"]
    shared = [FileDiff("a.py", PATCH, 2), FileDiff("c.py", PATCH, 2)]
    assert fault_files(by_line, shared) == []  # the line is in both files


def test_rows_survive_a_round_trip_through_json():
    row = to_row(record([commit("1", {"a.py": PATCH})], [change("F.4 Check", "a.py\n")]), no_compare)
    assert Row.from_json(row.to_json()) == row


@pytest.mark.parametrize(("year", "era"), [("2011", "2011–23"), ("2023", "2011–23"), ("2025", "2024–25")])
def test_eras_split_at_2024(year, era):
    row = to_row(
        record([commit("1", {"a.py": PATCH})], created_at=f"{year}-01-01T00:00:00+00:00"), no_compare
    )
    assert row.era == era
