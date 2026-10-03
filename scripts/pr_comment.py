"""Post the checks, what the branch changes in the app, and the changed functions' decisions as one
sticky pull request comment.

The mechanics follow contribution-engine's scripts/pr_comment.py: find the comment by its marker, PATCH
it or POST a new one, and append the body to the job summary. The summary gets the whole body; the
comment is cut to fit GitHub's limit. Outside a pull request only the summary is written. The check
outputs come from files the workflow captures with `tee`.
"""

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from scripts import decisions, flow

MARKER = "<!-- checks -->"
STATUS = {"success": "⚪ pass", "failure": "🔴 fail"}
CHECKS = ("ruff", "format", "types", "tests")
COMMENT_LIMIT = 60_000  # GitHub takes 65,536 characters; the checks and the legend need the rest
FLOW_LIMIT = 25_000  # what the app's changes may take; the changed functions get what is left


class Comment(TypedDict):
    """The fields of a GitHub issue comment that this script reads."""

    id: int  # the comment's id, to update it
    body: str  # its markdown


@dataclass
class Parts:
    """What the comment shows besides the checks, gathered once for both the comment and the summary."""

    changed: str  # the changed functions, decision by decision
    top: tuple[int, str, int] | None  # the function with the most decisions, and the phrases missing
    flow: str  # what the branch changes in the app
    gaps: int | None  # things new or edited code leaves unnamed; None with no base to compare with
    limits: dict[str, int]  # the limits ruff enforces


def gh_api(endpoint: str, *args: str, payload: dict[str, str] | None = None) -> Any:
    """One GitHub API call through the GitHub CLI, as parsed JSON."""
    command = ["gh", "api", endpoint, *args]
    if payload is not None:  # a body goes in through stdin
        command += ["--input", "-"]
    stdin = json.dumps(payload) if payload is not None else None
    return json.loads(subprocess.run(command, input=stdin, capture_output=True, text=True, check=True).stdout)


def find_comment(repo: str, pr: str) -> Comment | None:
    """The sticky comment on this pull request, found by its marker."""
    pages: list[list[Comment]] = gh_api(f"repos/{repo}/issues/{pr}/comments", "--paginate", "--slurp")
    comments = (comment for page in pages for comment in page)
    return next((comment for comment in comments if MARKER in comment["body"]), None)


def upsert_comment(repo: str, pr: str, comment: Comment | None, body: str) -> str:
    """Update the sticky comment, or post it when there is none yet; returns where it is shown."""
    path = f"issues/comments/{comment['id']}" if comment else f"issues/{pr}/comments"
    result = gh_api(
        f"repos/{repo}/{path}", "--method", "PATCH" if comment else "POST", payload={"body": body}
    )
    return str(result["html_url"])


def steps(env: dict) -> dict[str, str]:
    """Each check step's outcome, from STEP_RESULTS like `ruff=success format=failure tests=success`."""
    return dict(item.split("=", 1) for item in env.get("STEP_RESULTS", "").split())


def captured(checks_dir: Path, name: str) -> str | None:
    path = checks_dir / f"{name}.txt"
    return path.read_text() if path.exists() else None


def ruff_findings(text: str | None) -> int:
    return len(re.findall(r"^\S+:\d+:\d+: [A-Z]+\d+ ", text or "", re.MULTILINE))


def last_line(text: str | None) -> str:
    lines = [line.strip(" =") for line in (text or "").splitlines() if line.strip(" =")]
    return lines[-1] if lines else ""


def pytest_result(text: str | None) -> str:
    """The totals pytest prints last, such as `81 passed` or `1 failed, 80 passed`."""
    found = re.search(
        r"(\d+ (?:passed|failed|error|skipped|xfailed|xpassed|deselected)[^=\n]*?)(?: in [\d.]+s)?$",
        last_line(text),
    )
    return found[1].strip() if found else "no test result"


def previous_run(body: str | None) -> dict | None:
    """What the first line of the previous comment said, so the new one can say what moved."""
    found = re.search(
        r"\*\*Checks:\*\* (pass|fail)\. (\d+) ruff findings, (.+?) on `\w+` \(\[run\]\((\S+)\)\)", body or ""
    )
    return {"verdict": found[1], "findings": found[2], "tests": found[3], "run": found[4]} if found else None


def complexity_row(top: tuple[int, str, int] | None, limit: int) -> str:
    if top is None:  # the source could not be analysed
        return "| decisions | ⚫ not run | |"
    count, label, missing = top
    if count > limit:  # ruff fails this
        status = "🔴 over"
    elif count >= limit - 2 or missing:  # worth a look
        status = "🟡 watch"
    else:
        status = "⚪ pass"
    detail = f"highest {count} of {limit} (`{label}`)" + (
        f", {missing} decisions without a phrase" if missing else ""
    )
    return f"| decisions | {status} | {detail} |"


def names_row(gaps: int | None) -> str:
    """The row that says how much new or edited code leaves unnamed."""
    if gaps is None:  # nothing to compare with
        return "| names | ⚫ not run | |"
    if gaps:  # something is left to name
        return (
            f"| names | 🟡 watch | {gaps} thing{'s' if gaps != 1 else ''} new or edited code leaves unnamed |"
        )
    return "| names | ⚪ pass | new and edited code names its data, files and services |"


def cap(text: str, limit: int) -> str:
    """The text, cut at a paragraph when it would not fit, every fold it was cut inside closed again."""
    if len(text) <= limit:  # it fits
        return text
    kept = text[:limit].rsplit("\n\n", 1)[0]
    unclosed = kept.count("<details") - kept.count("</details>")
    return kept + "\n\n… cut here; the job summary has the full list." + "\n\n</details>" * unclosed


def fold(changed: str, whole: str = "") -> str:
    """The whole list behind one fold that names its size; each entry point folds again inside it.
    When the list was cut to fit, `whole` is the uncut list its size is counted from."""
    counted = whole or changed
    count = sum(line.startswith(("⚪", "🟡", "🔴", "−")) for line in counted.splitlines())
    if not count:  # nothing changed: one line says so
        return changed
    entry_points = counted.count("<details><summary>")
    summary = f"{count} function" + ("s" if count != 1 else "")
    if entry_points:  # the inner folds are entry points
        summary += f" in {entry_points} entry point" + ("s" if entry_points != 1 else "")
    state = " open" if count <= 5 else ""
    return f"<details{state}><summary>{summary}</summary>\n\n{changed}\n\n</details>"


def render(
    env: dict[str, str],
    checks_dir: Path,
    previous: str | None,
    *,
    changed: str,
    top: tuple[int, str, int] | None,
    limits: dict[str, int],
    flow: str = "",
    gaps: int | None = None,
    limit: int | None = COMMENT_LIMIT,
) -> str:
    """The comment body: a verdict line, what moved since last run, the checks table, what the branch
    changes in the app, and the changed functions; cut to `limit` characters unless it is None."""
    outcomes = steps(env)
    sha = env.get("HEAD_SHA") or env.get("GITHUB_SHA", "")
    run_url = f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"
    findings = ruff_findings(captured(checks_dir, "ruff"))
    tests = pytest_result(captured(checks_dir, "pytest"))
    passed = all(outcomes.get(name) == "success" for name in CHECKS)
    verdict = "pass" if passed else "fail"
    before = previous_run(previous)
    if before:  # there was a comment to compare with
        since = (
            f"Since last run: ruff {before['findings']} → {findings} findings, tests {before['tests']} → {tests}. "
            f"Previous: {before['verdict']} ([run]({before['run']}))."
        )
    else:
        since = "First run."

    def status(name: str) -> str:
        return STATUS.get(outcomes.get(name, ""), "⚫ not run")

    rows = [
        f"| ruff check | {status('ruff')} | {findings} findings |",
        f"| ruff format | {status('format')} | {last_line(captured(checks_dir, 'format'))} |",
        f"| mypy | {status('types')} | {last_line(captured(checks_dir, 'mypy'))} |",
        f"| pytest | {status('tests')} | {last_line(captured(checks_dir, 'pytest'))} |",
        complexity_row(top, limits["decisions"]),
        names_row(gaps),
    ]
    shown = cap(flow, FLOW_LIMIT) if limit else flow
    functions = cap(changed, limit - len(shown)) if limit else changed
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return "\n".join(
        [
            f"{'🟢' if passed else '🔴'} **Checks:** {verdict}. {findings} ruff findings, {tests} on `{sha[:7]}` ([run]({run_url})).",
            since,
            "",
            "| Check | Status | Detail |",
            "| --- | --- | --- |",
            *rows,
            "",
            *([shown, ""] if shown else []),
            "**Functions this PR touched**, by entry point, in the order it reaches them, "
            "each with its decisions as they are now",
            "",
            fold(functions, changed),
            "",
            decisions.LEGEND,
            f"Limits: {limits['decisions']} decisions per function (ruff mccabe {limits['complexity']}), "
            f"{limits['branches']} branches, {limits['statements']} statements. Updated {stamp}.",
            MARKER,
        ]
    )


def highest(paths: list[str]) -> tuple[int, str, int] | None:
    """The function with the most decisions under `paths`, and how many decisions lack a phrase."""
    found = [
        fn for path in decisions.python_files(paths) for fn in decisions.functions(path, path.read_text())
    ]
    if not found:  # nothing to measure
        return None
    top = max(found, key=lambda fn: fn.count)
    return top.count, top.label, sum(fn.missing for fn in found)


def known_base(env: dict[str, str]) -> str:
    """The base commit to compare with; empty when there is none, as on a push to a new branch."""
    base = env.get("BASE_SHA", "")
    if not base:  # no base was given
        return ""
    known = subprocess.run(["git", "cat-file", "-e", f"{base}^{{commit}}"], capture_output=True, check=False)
    return base if known.returncode == 0 else ""


def changed_section(env: dict[str, str], limits: dict[str, int], link_base: str) -> str:
    """The functions the branch changed, decision by decision."""
    base = known_base(env)
    if not base:  # no base to compare with
        return "No base commit to compare with."
    return decisions.changed_report(base, ["src", "scripts"], limits["decisions"], link_base, fold=True)


def flow_section(env: dict[str, str]) -> tuple[str, int | None]:
    """What the branch changes in the app, and how much new or edited code leaves unnamed."""
    base = known_base(env)
    if not base:  # no base to compare with
        return "", None
    with tempfile.TemporaryDirectory() as folder:
        shown, gaps = flow.changed(flow.checkout(base, Path(folder)), Path.cwd())
    return shown, gaps


def gather(env: dict[str, str]) -> Parts:
    """Everything the comment shows besides the checks, computed once."""
    limits = decisions.limits()
    sha = env.get("HEAD_SHA") or env.get("GITHUB_SHA", "")
    link_base = f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}/blob/{sha}/"
    shown, gaps = flow_section(env)
    return Parts(changed_section(env, limits, link_base), highest(["src", "scripts"]), shown, gaps, limits)


def compose(env: dict[str, str], previous: str | None, parts: Parts, limit: int | None) -> str:
    """The body from the gathered parts, cut to `limit` characters unless it is None."""
    return render(
        env,
        Path(env.get("CHECKS_DIR", "checks")),
        previous,
        changed=parts.changed,
        top=parts.top,
        limits=parts.limits,
        flow=parts.flow,
        gaps=parts.gaps,
        limit=limit,
    )


def main() -> int:
    """Write the job summary, and on a pull request post or update the sticky comment."""
    env = dict(os.environ)
    repo, pr = env["GITHUB_REPOSITORY"], env.get("PR_NUMBER") or None
    comment = find_comment(repo, pr) if pr else None
    previous = comment["body"] if comment else None
    parts = gather(env)
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:  # the job summary gets the whole body, uncut
        with Path(summary).open("a") as stream:
            stream.write(compose(env, previous, parts, None) + "\n")
    if pr:  # a pull request gets the sticky comment
        print(upsert_comment(repo, pr, comment, compose(env, previous, parts, COMMENT_LIMIT)))
    else:
        print("No pull request in this event; summary only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
