import json

import pytest

from s1cr import cli, github, jev, questions
from s1cr.models import FileDiff, ReviewInput

URL = "https://github.com/owner/repo/pull/123"


@pytest.fixture
def review_setup(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    pr = github.PullRequest(
        URL,
        "b" * 40,
        "h" * 40,
        ReviewInput(
            "Fix tax",
            "Handle empty inputs.",
            [
                FileDiff("tax.py", "@@ -1 +1 @@\n-old\n+new", 2),
                FileDiff("test_tax.py", "@@ -0,0 +1 @@\n+test()", 1),
            ],
        ),
    )
    monkeypatch.setattr(github, "pull_request", lambda url: pr)
    calls = []
    answers = {
        "changes_requested": {"type": "noul", "noul": 0.87654321},
        "functional_defect": {"type": "noul", "noul": 0.2},
        "problem_type": {
            "type": "choice",
            "choice": "F2",
            "confidence": 0.6,
            "probabilities": {"F4": 0.3, "F2": 0.7},
        },
        "fault_file": {
            "type": "choice",
            "choice": "f1",
            "confidence": 0.8,
            "probabilities": {"f0": 0.2, "f1": 0.8},
        },
    }

    def call(client, request, key):
        calls.append(request)
        assert key == "test-key"
        response_answers = {k: v for k, v in answers.items() if k in request["questions"]}
        return {
            "model": "jev-1.13.0",
            "answers": response_answers,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }

    monkeypatch.setattr(jev, "call", call)
    return pr, answers, calls


def test_json_command_uses_shared_request_and_keeps_full_precision(review_setup, capsys):
    pr, answers, calls = review_setup
    cli.main(["review", URL, "--json", "--model", "jev-1.13.0"])
    output = capsys.readouterr()
    assert output.err == ""
    data = json.loads(output.out)
    assert data["pull_request"] == {
        "url": URL,
        "title": "Fix tax",
        "base_sha": "b" * 40,
        "head_sha": "h" * 40,
    }
    assert data["answers"] == answers
    assert data["file_options"] == {"f0": "tax.py", "f1": "test_tax.py"}
    assert data["category_options"]["F2"] == {"code": "F.2", "name": "Logic"}
    assert data["questions"] == "v1"
    assert data["model"] == "jev-1.13.0"
    assert data["usage"]["input_tokens"] == 100
    assert data["ms"] >= 0
    assert calls == [questions.request(pr.input, "jev-1.13.0")]


def test_readable_command_shows_scores_and_resolves_choice_options(review_setup, capsys):
    _pr, _answers, calls = review_setup
    cli.main(["review", URL])
    output = capsys.readouterr().out
    assert "Reviewed head: " + "h" * 40 in output
    assert "changes requested: 87.7%" in output
    assert "functional defect: 20.0%" in output
    assert "Change category: F.2 Logic (confidence 60.0%)" in output
    assert "File most likely to need changes: test_tax.py (confidence 80.0%)" in output
    assert output.index("70.0%  F.2") < output.index("30.0%  F.4")
    assert "Tokens: 100 input, 20 output" in output
    assert calls[0]["model"] == "jev-latest"


def test_single_file_does_not_invent_a_file_choice_score(review_setup, capsys):
    pr, _, calls = review_setup
    pr.input.files = pr.input.files[:1]
    cli.main(["review", URL])
    assert "Only changed file: tax.py (file-choice question not asked)" in capsys.readouterr().out
    assert "fault_file" not in calls[0]["questions"]


def test_missing_key_fails_before_fetching_a_pr(monkeypatch, capsys):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(github, "pull_request", lambda url: pytest.fail("should not fetch without a key"))
    with pytest.raises(SystemExit, match="TYPESAFE_API_KEY is not set"):
        cli.main(["review", URL, "--json"])
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("error", [github.GitHubError("HTTP 404"), jev.JevError("Jev HTTP 429")])
def test_review_failures_exit_with_no_success_json(review_setup, monkeypatch, capsys, error):
    def fail(*args, **kwargs):
        raise error

    if isinstance(error, github.GitHubError):
        monkeypatch.setattr(github, "pull_request", fail)
    else:
        monkeypatch.setattr(jev, "call", fail)
    with pytest.raises(SystemExit, match=f"error: {error}"):
        cli.main(["review", URL, "--json"])
    assert capsys.readouterr().out == ""
