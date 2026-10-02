"""Post the checks and the changed functions' decisions as one sticky pull request comment.

The mechanics follow contribution-engine's scripts/pr_comment.py: find the comment by its marker, PATCH
it or POST a new one, and append the same body to the job summary. Outside a pull request only the
summary is written. The check outputs come from files the workflow captures with `tee`.
"""

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from scripts import decisions

MARKER = "<!-- checks -->"
STATUS = {"success": "⚪ pass", "failure": "🔴 fail"}


def gh_api(endpoint: str, *args: str, payload: dict | None = None) -> dict | list:
    command = ["gh", "api", endpoint, *args]
    if payload is not None:  # a body goes in through stdin
        command += ["--input", "-"]
    stdin = json.dumps(payload) if payload is not None else None
    return json.loads(subprocess.run(command, input=stdin, capture_output=True, text=True, check=True).stdout)


def find_comment(repo: str, pr: str) -> dict | None:
    pages = gh_api(f"repos/{repo}/issues/{pr}/comments", "--paginate", "--slurp")
    comments = (comment for page in pages for comment in page)
    return next((comment for comment in comments if MARKER in comment["body"]), None)


def upsert_comment(repo: str, pr: str, comment: dict | None, body: str) -> str:
    path = f"issues/comments/{comment['id']}" if comment else f"issues/{pr}/comments"
    result = gh_api(
        f"repos/{repo}/{path}", "--method", "PATCH" if comment else "POST", payload={"body": body}
    )
    return result["html_url"]


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


def cap(changed: str, limit: int = 50_000) -> str:
    """The changed functions, cut when the comment would not fit GitHub's 65,536 characters."""
    if len(changed) <= limit:  # it fits
        return changed
    return changed[:limit].rsplit("\n\n", 1)[0] + "\n\n… cut here; the job summary has the full list."


def fold(changed: str) -> str:
    """The whole list behind one fold that names its size; each entry point folds again inside it."""
    count = sum(line.startswith(("⚪", "🟡", "🔴", "−")) for line in changed.splitlines())
    if not count:  # nothing changed: one line says so
        return changed
    entry_points = changed.count("<details><summary>")
    summary = f"{count} function" + ("s" if count != 1 else "")
    if entry_points:  # the inner folds are entry points
        summary += f" in {entry_points} entry point" + ("s" if entry_points != 1 else "")
    state = " open" if count <= 5 else ""
    return f"<details{state}><summary>{summary}</summary>\n\n{changed}\n\n</details>"


def render(
    env: dict,
    checks_dir: Path,
    previous: str | None,
    *,
    changed: str,
    top: tuple[int, str, int] | None,
    limits: dict,
) -> str:
    """The comment body: a verdict line, what moved since last run, the checks table, the changed functions."""
    outcomes = steps(env)
    sha = env.get("HEAD_SHA") or env.get("GITHUB_SHA", "")
    run_url = f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"
    findings = ruff_findings(captured(checks_dir, "ruff"))
    tests = pytest_result(captured(checks_dir, "pytest"))
    passed = all(outcomes.get(name) == "success" for name in ("ruff", "format", "tests"))
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
        f"| pytest | {status('tests')} | {last_line(captured(checks_dir, 'pytest'))} |",
        complexity_row(top, limits["decisions"]),
    ]
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
            "**Functions this PR touched**, by entry point, in the order it reaches them, "
            "each with its decisions as they are now",
            "",
            fold(cap(changed)),
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


def changed_section(env: dict, limits: dict, link_base: str) -> str:
    base = env.get("BASE_SHA", "")
    known = subprocess.run(["git", "cat-file", "-e", f"{base}^{{commit}}"], capture_output=True, check=False)
    if not base or known.returncode != 0:  # no base to compare with, as on a push to a new branch
        return "No base commit to compare with."
    return decisions.changed_report(base, ["src", "scripts"], limits["decisions"], link_base, fold=True)


def build(env: dict, previous: str | None) -> str:
    limits = decisions.limits()
    sha = env.get("HEAD_SHA") or env.get("GITHUB_SHA", "")
    link_base = f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}/blob/{sha}/"
    changed = changed_section(env, limits, link_base)
    return render(
        env,
        Path(env.get("CHECKS_DIR", "checks")),
        previous,
        changed=changed,
        top=highest(["src", "scripts"]),
        limits=limits,
    )


def main() -> int:
    env = dict(os.environ)
    repo, pr = env["GITHUB_REPOSITORY"], env.get("PR_NUMBER") or None
    comment = find_comment(repo, pr) if pr else None
    body = build(env, comment["body"] if comment else None)
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:  # the job summary shows the same body
        with Path(summary).open("a") as stream:
            stream.write(body + "\n")
    if pr:  # a pull request gets the sticky comment
        print(upsert_comment(repo, pr, comment, body))
    else:
        print("No pull request in this event; summary only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
