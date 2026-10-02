"""Calling TypeSafe's Jev, and running every pull request through it.

Retries follow worktree-repos' scripts/review/jev-review.mjs: up to three attempts, backing off on rate
limits (429) and server errors, giving up at once on any other error.
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from . import questions
from .models import MAX_STATE_CHARS, ReviewConfig, ReviewInput, ReviewResult

if TYPE_CHECKING:
    from .swrbench import Row

API_URL = "https://api.typesafe.ai/v1/systemone"


class JevError(Exception):
    pass


def call(client: httpx.Client, request: dict, api_key: str, attempts: int = 3) -> dict:
    error = "no attempt made"
    for attempt in range(attempts):
        response = client.post(API_URL, json=request, headers={"Authorization": f"Bearer {api_key}"})
        if response.status_code == 200:
            return response.json()
        error = f"Jev HTTP {response.status_code}: {response.text[:300]}"
        if response.status_code != 429 and response.status_code < 500:
            break
        time.sleep(0.5 * (attempt + 1))
    raise JevError(error)


def review(
    review_input: ReviewInput,
    config: ReviewConfig,
    *,
    api_key: str,
    client: httpx.Client | None = None,
) -> ReviewResult:
    """Review one PR with the frozen v1 questions, from either GitHub or a benchmark.

    A caller may share an HTTP client across reviews; otherwise this call owns its client.
    The size check matches the original benchmark's preflight check exactly.
    """
    if not api_key:
        raise ValueError("TYPESAFE_API_KEY is not set")
    if not review_input.files:
        raise ValueError("no file changes")
    if (
        sum(len(f.patch) + len(f.path) for f in review_input.files) + len(review_input.title)
        > MAX_STATE_CHARS
    ):
        raise ValueError("too large for one Jev call")
    started = time.perf_counter()
    with nullcontext(client) if client is not None else httpx.Client(timeout=120) as http:
        response = call(http, questions.request(review_input, config.model), api_key)
    return ReviewResult(
        questions=questions.VERSION,
        model=response.get("model"),
        answers=response["answers"],
        usage=response.get("usage"),
        ms=round((time.perf_counter() - started) * 1000),
    )


def done(output: Path) -> set[str]:
    """Pull requests that already have an answer in this run."""
    if not output.exists():
        return set()
    lines = [json.loads(line) for line in output.read_text().splitlines() if line]
    return {line["id"] for line in lines if "answers" in line}


def run(rows: list["Row"], output: Path, model: str, api_key: str, workers: int = 4) -> tuple[int, int]:
    """Ask every row's questions once, appending one JSON line per answer to `output`. A rerun picks up
    where the last one stopped. Returns how many calls succeeded and failed."""
    from .provenance import prepare

    prepare(output.parent, rows, model, workers)
    completed = done(output)
    todo = [row for row in rows if row.excluded is None and row.id not in completed]
    lock = threading.Lock()
    ok = failed = 0
    config = ReviewConfig(model=model)

    def ask(client: httpx.Client, row: "Row") -> dict:
        try:
            result = review(row.review_input(), config, api_key=api_key, client=client)
            if result.model != model:
                return {
                    "id": row.id,
                    "error": f"requested model {model}, but Jev returned {result.model}",
                    "model": result.model,
                    "usage": result.usage,
                    "ms": result.ms,
                }
        except (JevError, httpx.HTTPError, ValueError) as error:
            return {"id": row.id, "error": str(error)}
        return {"id": row.id, **result.to_dict()}

    with httpx.Client(timeout=120) as client, ThreadPoolExecutor(workers) as pool:
        for future in as_completed(pool.submit(ask, client, row) for row in todo):
            result = future.result()
            with lock, output.open("a") as out:
                out.write(json.dumps(result) + "\n")
            if "answers" in result:
                ok += 1
            else:
                failed += 1
            if (ok + failed) % 50 == 0:
                print(f"  {ok + failed}/{len(todo)} ({failed} failed)")
    return ok, failed


def load(output: Path) -> dict[str, dict]:
    """The answered calls of one run, by pull request id; later lines win over earlier ones."""
    answered: dict[str, dict] = {}
    for line in output.read_text().splitlines():
        if line:
            result = json.loads(line)
            if "answers" in result:
                answered[result["id"]] = result
    return answered


def refused(output: Path) -> set[str]:
    """Pull requests Jev would not take because they are over its token limit, which the character count
    in `swrbench.MAX_STATE_CHARS` only approximates."""
    latest: dict[str, dict] = {}
    for line in output.read_text().splitlines():
        if line:
            result = json.loads(line)
            latest[result["id"]] = result
    return {i for i, result in latest.items() if "max_tokens_exceeded" in result.get("error", "")}
