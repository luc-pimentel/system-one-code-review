"""The diff between two commits, from GitHub's compare API, cached on disk.

A pull request with several commits before its first review has no single commit whose diff is what the
reviewer saw. GitHub's diff of the pull request is no substitute: it shows the final state, fixes included.
Comparing the base commit with the last commit before the review gives exactly the reviewed change.
"""

import json
import subprocess
from pathlib import Path

import httpx

from .swrbench import FileDiff, changed_lines


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
