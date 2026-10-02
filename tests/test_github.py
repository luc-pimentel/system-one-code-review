import json
import subprocess

import pytest

from s1cr import github
from s1cr.models import FileDiff, ReviewInput

URL = "https://github.com/owner/repo/pull/123"
PR = {
    "title": "Fix tax",
    "body": "Handle the empty case.",
    "changed_files": 2,
    "base": {"sha": "b" * 40},
    "head": {"sha": "h" * 40},
}
FILES = [
    {"filename": "space name.py", "patch": "@@ -1 +1 @@\n-old\n+new", "changes": 2},
    {"filename": "tests/test_tax.py", "patch": "@@ -0,0 +1 @@\n+test()", "changes": 1},
]


def gh_responses(monkeypatch, *responses):
    queue = iter(responses)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(next(queue)))

    monkeypatch.setattr(github.subprocess, "run", run)
    return commands


def test_fetches_all_pages_preserves_paths_and_records_the_reviewed_commits(monkeypatch):
    commands = gh_responses(monkeypatch, PR, [[FILES[0]], [FILES[1]]], PR)
    pr = github.pull_request(URL + "/files?diff=split#diff-1")
    assert pr.url == URL
    assert pr.head_sha == "h" * 40
    assert pr.base_sha == "b" * 40
    assert pr.input == ReviewInput(
        "Fix tax",
        "Handle the empty case.",
        [
            FileDiff("space name.py", FILES[0]["patch"], 2),
            FileDiff("tests/test_tax.py", FILES[1]["patch"], 1),
        ],
    )
    assert commands == [
        ["gh", "api", "repos/owner/repo/pulls/123"],
        ["gh", "api", "repos/owner/repo/pulls/123/files?per_page=100", "--paginate", "--slurp"],
        ["gh", "api", "repos/owner/repo/pulls/123"],
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/owner/repo/pull/123",
        "https://github.com/owner/repo/issues/123",
        "https://github.com/owner/repo/pull/0",
        "owner/repo#123",
        "https://github.com/owner/repo/pull/1/garbage",
    ],
)
def test_invalid_urls_fail_before_running_gh(monkeypatch, url):
    commands = gh_responses(monkeypatch)
    with pytest.raises(ValueError, match="expected a pull request URL"):
        github.pull_request(url)
    assert not commands


@pytest.mark.parametrize(
    "update",
    [
        {"head": {"sha": "new-head"}},
        {"base": {"sha": "new-base"}},
        {"body": "Edited description"},
        {"title": "Edited title"},
        {"changed_files": 3},
    ],
)
def test_pr_updates_during_fetch_are_rejected(monkeypatch, update):
    gh_responses(monkeypatch, PR, [FILES], PR | update)
    with pytest.raises(github.GitHubError, match="changed while fetching"):
        github.pull_request(URL)


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ([FILES[0]], "incomplete file list"),
        ([FILES[0], FILES[0]], "incomplete file list"),
        ([FILES[0], {"filename": "image.png", "changes": 0}], "did not return a text patch for image.png"),
        ([FILES[0], FILES[1] | {"changes": 200}], "incomplete patch for tests/test_tax.py"),
    ],
)
def test_incomplete_diffs_are_rejected(monkeypatch, files, message):
    gh_responses(monkeypatch, PR, [files], PR)
    with pytest.raises(github.GitHubError, match=message):
        github.pull_request(URL)


def test_null_body_becomes_empty_description(monkeypatch):
    pr = PR | {"body": None}
    gh_responses(monkeypatch, pr, [FILES], pr)
    assert github.pull_request(URL).input.description == ""


def test_no_file_changes_fails_before_fetching_files(monkeypatch):
    commands = gh_responses(monkeypatch, PR | {"changed_files": 0})
    with pytest.raises(github.GitHubError, match="no file changes"):
        github.pull_request(URL)
    assert len(commands) == 1


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (FileNotFoundError(), "install gh"),
        (subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 404: Not Found"), "HTTP 404"),
    ],
)
def test_gh_errors_are_actionable(monkeypatch, error, message):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(github.subprocess, "run", fail)
    with pytest.raises(github.GitHubError, match=message):
        github.pull_request(URL)
