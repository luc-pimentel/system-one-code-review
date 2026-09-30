import json

import httpx
import pytest

from s1cr import jev
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


def test_rate_limits_and_server_errors_are_retried():
    assert jev.call(client([429, 503, 200]), {}, "key")["answers"] == ANSWERS


def test_other_errors_fail_at_once():
    with pytest.raises(jev.JevError, match="HTTP 400"):
        jev.call(client([400, 200]), {}, "key")


def test_a_run_resumes_and_records_what_jev_refused(tmp_path, monkeypatch):
    rows = [Row(f"o__r-{i}", "o/r", "2020", "t", "d", [FileDiff("a.py", "+x", 1)], True) for i in range(3)]
    statuses = iter([200, 400])
    monkeypatch.setattr(jev, "call", lambda c, r, k: _answer(next(statuses)))
    output = tmp_path / "answers.jsonl"
    ok, failed = jev.run(rows[:2], output, "jev-1.13.0", "key", workers=1)
    assert (ok, failed) == (1, 1)
    monkeypatch.setattr(jev, "call", lambda c, r, k: _answer(200))
    assert jev.run(rows, output, "jev-1.13.0", "key", workers=1) == (2, 0)  # the refused one is asked again
    assert set(jev.load(output)) == {r.id for r in rows}
    lines = [json.loads(line) for line in output.read_text().splitlines()]
    assert lines[0]["model"] == "jev-1.13.0" and lines[0]["questions"] == "v1"


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


def _answer(status: int) -> dict:
    if status != 200:
        raise jev.JevError(f"Jev HTTP {status}")
    return {"model": "jev-1.13.0", "answers": ANSWERS, "usage": {"input_tokens": 10, "output_tokens": 2}}
