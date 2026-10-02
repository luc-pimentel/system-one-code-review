import json

import httpx
import pytest

from s1cr import jev, provenance
from s1cr.models import ReviewConfig, ReviewInput, ReviewResult
from s1cr.swrbench import FileDiff, Row

ANSWERS = {
    "changes_requested": {"type": "noul", "noul": 0.8},
    "functional_defect": {"type": "noul", "noul": 0.3},
    "problem_type": {"type": "choice", "choice": "F2", "confidence": 0.4, "probabilities": {}},
}


def client(statuses: list[int]) -> httpx.Client:
    """A client whose server answers with these statuses in turn; 200 carries a Jev response."""
    queue = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        status = queue.pop(0)
        if status == 200:
            return httpx.Response(200, json={"model": "jev-1.13.0", "answers": ANSWERS, "usage": {}})
        return httpx.Response(status, json={"detail": {"error_type": "max_tokens_exceeded"}})

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(jev.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(provenance, "git_snapshot", lambda: {"commit": "a" * 40, "dirty": False})


def test_rate_limits_and_server_errors_are_retried():
    assert jev.call(client([429, 503, 200]), {}, "key")["answers"] == ANSWERS


def test_other_errors_fail_at_once():
    with pytest.raises(jev.JevError, match="HTTP 400"):
        jev.call(client([400, 200]), {}, "key")


def test_shared_reviewer_keeps_raw_answers_and_does_not_close_a_borrowed_client():
    review_input = ReviewInput("Add tax", "Adds tax.", [FileDiff("a.py", "+tax()", 1)])
    with client([200]) as http:
        result = jev.review(review_input, ReviewConfig("jev-1.13.0"), api_key="key", client=http)
        assert not http.is_closed
    assert isinstance(result, ReviewResult)
    assert result.questions == "v1"
    assert result.model == "jev-1.13.0"
    assert result.answers == ANSWERS
    assert result.usage == {}
    assert result.ms >= 0


def test_benchmark_invokes_the_shared_reviewer_without_labels(tmp_path, monkeypatch):
    row = Row(
        "o__r-1",
        "o/r",
        "2020",
        "Title",
        "Body",
        [FileDiff("a.py", "+x", 1)],
        True,
        categories=["F.2"],
        fault_files=["a.py"],
    )
    seen = []
    result = ReviewResult("v1", "jev-1.13.0", ANSWERS, {"input_tokens": 10}, 25)

    def review(review_input, config, *, api_key, client):
        seen.append(review_input)
        assert config.model == "jev-1.13.0"
        assert api_key == "key"
        return result

    monkeypatch.setattr(jev, "review", review)
    output = tmp_path / "answers.jsonl"
    assert jev.run([row], output, "jev-1.13.0", "key") == (1, 0)
    assert seen == [ReviewInput("Title", "Body", row.files)]
    assert not hasattr(seen[0], "categories")
    assert not hasattr(seen[0], "changes_requested")
    assert json.loads(output.read_text()) == {"id": row.id, **result.to_dict()}


@pytest.mark.parametrize("files", [[], [FileDiff("big.py", "x" * 100_000, 1)]])
def test_empty_and_oversized_inputs_fail_before_calling_jev(files):
    with client([]) as http, pytest.raises(ValueError, match=r"no file changes|too large"):
        jev.review(ReviewInput("Title", "Body", files), ReviewConfig(), api_key="key", client=http)


def test_a_run_resumes_and_records_what_jev_refused(tmp_path, monkeypatch):
    rows = [Row(f"o__r-{i}", "o/r", "2020", "t", "d", [FileDiff("a.py", "+x", 1)], True) for i in range(3)]
    statuses = iter([200, 400, 400])
    monkeypatch.setattr(jev, "call", lambda c, r, k: _answer(next(statuses)))
    output = tmp_path / "answers.jsonl"
    ok, failed = jev.run(rows, output, "jev-1.13.0", "key", workers=1)
    assert (ok, failed) == (1, 2)
    monkeypatch.setattr(jev, "call", lambda c, r, k: _answer(200))
    assert jev.run(rows, output, "jev-1.13.0", "key", workers=1) == (2, 0)  # the refused one is asked again
    assert set(jev.load(output)) == {r.id for r in rows}
    lines = [json.loads(line) for line in output.read_text().splitlines()]
    assert lines[0]["model"] == "jev-1.13.0"
    assert lines[0]["questions"] == "v1"


def test_token_limit_refusals_are_told_apart_from_other_failures(tmp_path):
    output = tmp_path / "answers.jsonl"
    lines = [
        {"id": "big", "error": 'Jev HTTP 400: {"detail":{"error_type":"max_tokens_exceeded"}}'},
        {"id": "flaky", "error": "Jev HTTP 503: down"},
        {"id": "later-ok", "error": 'Jev HTTP 400: {"detail":{"error_type":"max_tokens_exceeded"}}'},
        {"id": "later-ok", "answers": ANSWERS},
    ]
    output.write_text("".join(json.dumps(line) + "\n" for line in lines))
    assert jev.refused(output) == {"big"}


def test_run_does_not_accept_an_unexpected_model(tmp_path, monkeypatch):
    row = Row("a", "o/r", "2020", "Title", "Body", [FileDiff("a.py", "+x", 1)], False)
    monkeypatch.setattr(jev, "call", lambda *args: _answer(200) | {"model": "jev-1.14.0"})
    output = tmp_path / "answers.jsonl"
    assert jev.run([row], output, "jev-1.13.0", "key") == (0, 1)
    result = json.loads(output.read_text())
    assert result["model"] == "jev-1.14.0"
    assert "answers" not in result
    assert "requested model jev-1.13.0" in result["error"]


def test_changed_inputs_cannot_resume_or_call_the_provider(tmp_path, monkeypatch):
    row = Row("a", "o/r", "2020", "Title", "Body", [FileDiff("a.py", "+x", 1)], False)
    monkeypatch.setattr(jev, "call", lambda *args: _answer(200))
    output = tmp_path / "answers.jsonl"
    jev.run([row], output, "jev-1.13.0", "key")
    original = output.read_bytes()
    row.description = "Changed inputs"
    monkeypatch.setattr(jev, "call", lambda *args: pytest.fail("must not call Jev"))
    with pytest.raises(ValueError, match="use a new run name"):
        jev.run([row], output, "jev-1.13.0", "key")
    assert output.read_bytes() == original


def _answer(status: int) -> dict:
    if status != 200:
        raise jev.JevError(f"Jev HTTP {status}")
    return {"model": "jev-1.13.0", "answers": ANSWERS, "usage": {"input_tokens": 10, "output_tokens": 2}}
