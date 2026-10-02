"""Live pull requests through the GitHub CLI, and cached benchmark commit comparisons.

A pull request with several commits before its first review has no single commit whose diff is what the
reviewer saw. GitHub's diff of the pull request is no substitute: it shows the final state, fixes included.
Comparing the base commit with the last commit before the review gives exactly the reviewed change.
"""

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .models import FileDiff, ReviewInput
from .swrbench import changed_lines


class GitHubError(Exception):
    pass


@dataclass
class PullRequest:
    url: str
    base_sha: str
    head_sha: str
    input: ReviewInput


def parse_pr_url(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    match = re.fullmatch(r"/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)(?:/(?:files|commits))?/?", parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or not match:
        raise ValueError("expected a pull request URL: https://github.com/owner/repo/pull/123")
    return match[1], int(match[2])


def _api(endpoint: str, *, paginate: bool = False) -> dict | list:
    command = ["gh", "api", endpoint]
    if paginate:
        command.extend(["--paginate", "--slurp"])
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as error:
        raise GitHubError("GitHub CLI is required; install gh and run `gh auth login`") from error
    except subprocess.CalledProcessError as error:
        raise GitHubError(f"GitHub request failed: {error.stderr.strip()}") from error
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise GitHubError("GitHub CLI returned invalid JSON") from error


def pull_request(url: str) -> PullRequest:
    """Fetch the current PR diff, paginating files and checking the snapshot stayed put.

    GitHub can omit or truncate patches and caps this endpoint at 3,000 files. Refuse
    an incomplete input rather than present a review of only part of the PR.
    """
    repo, number = parse_pr_url(url)
    endpoint = f"repos/{repo}/pulls/{number}"
    before = _api(endpoint)
    if not before["changed_files"]:
        raise GitHubError("pull request has no file changes")
    pages = _api(f"{endpoint}/files?per_page=100", paginate=True)
    files = [file for page in pages for file in page]
    after = _api(endpoint)

    def snapshot(pr: dict) -> tuple:
        return pr["base"]["sha"], pr["head"]["sha"], pr["title"], pr["body"], pr["changed_files"]

    if snapshot(before) != snapshot(after):
        raise GitHubError("pull request changed while fetching its diff; run the review again")
    if len(files) != before["changed_files"] or len({f["filename"] for f in files}) != len(files):
        raise GitHubError("GitHub returned an incomplete file list; cannot review the whole pull request")
    diffs = []
    for file in files:
        patch = file.get("patch")
        if not patch:
            raise GitHubError(
                f"GitHub did not return a text patch for {file['filename']}; "
                "binary, rename-only, and oversized changes are not supported by this diff-only reviewer"
            )
        # The files API returns hunks without ---/+++ headers, so every +/- line counts.
        changed = sum(line.startswith(("+", "-")) for line in patch.split("\n"))
        if changed != file["changes"]:
            raise GitHubError(f"GitHub returned an incomplete patch for {file['filename']}")
        diffs.append(FileDiff(file["filename"], patch, changed))
    return PullRequest(
        url=f"https://github.com/{repo}/pull/{number}",
        base_sha=before["base"]["sha"],
        head_sha=before["head"]["sha"],
        input=ReviewInput(before["title"], before["body"] or "", diffs),
    )


def token() -> str:
    """The GitHub CLI's token, so the API allows 5,000 requests an hour instead of 60."""
    return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()


class Comparer:
    def __init__(self, cache: Path, github_token: str) -> None:
        self.cache = cache
        self.client = httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=60,
        )

    def __call__(self, repo: str, base: str, head: str) -> tuple[list[FileDiff], int]:
        path = self.cache / f"{repo.replace('/', '__')}__{base[:12]}__{head[:12]}.json"
        if path.exists():
            data = json.loads(path.read_text())
        else:
            response = self.client.get(f"/repos/{repo}/compare/{base}...{head}")
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            full = response.json()
            data = {
                "ahead_by": full["ahead_by"],
                "files": [
                    {"filename": f["filename"], "patch": f.get("patch", ""), "changes": f["changes"]}
                    for f in full.get("files", [])
                ],
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
        files = [
            FileDiff(f["filename"], f["patch"], changed_lines(f["patch"]) if f["patch"] else f["changes"])
            for f in data["files"]
        ]
        return files, data["ahead_by"]
