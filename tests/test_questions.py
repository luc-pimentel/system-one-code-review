import hashlib
import json

from s1cr import questions
from s1cr.swrbench import CATEGORIES, FileDiff, Row


def row(paths: list[str], description: str = "Adds tax.") -> Row:
    files = [FileDiff(p, "@@ -1 +1 @@\n+x = 1", 1) for p in paths]
    return Row("o__r-1", "o/r", "2020-01-01", "Add tax", description, files, changes_requested=True)


def test_every_pull_request_gets_the_yes_no_questions_and_the_kind_of_change():
    request = questions.request(row(["a.py"]), "jev-1.13.0")
    assert request["model"] == "jev-1.13.0"
    assert set(request["questions"]) == {"changes_requested", "functional_defect", "problem_type"}
    for key in ("changes_requested", "functional_defect"):
        asked = request["questions"][key]
        assert asked["type"] == "noul" and set(asked["criteria"]) == {"true", "false"}


def test_the_file_question_is_asked_only_when_there_is_a_choice():
    request = questions.request(row(["a.py", "b.py"]), "jev-1.13.0")
    assert request["questions"]["fault_file"]["criteria"] == {"f0": "a.py", "f1": "b.py"}


def test_kinds_of_change_are_swr_bench_s_categories_with_its_definitions():
    criteria = questions.PROBLEM_TYPE["criteria"]
    assert len(criteria) == len(CATEGORIES) == 11
    assert questions.OPTIONS["F2"] == "F.2" and criteria["F2"].startswith("Logic: Corrections to errors")


def test_jev_reads_the_pull_request_and_nothing_from_its_review():
    state = questions.state(row(["a.py"], description="d" * 10_000))
    assert set(state) == {"pr_title", "pr_description", "files"}
    assert len(state["pr_description"]) == questions.MAX_DESCRIPTION_CHARS
    assert state["files"] == {"f0": {"path": "a.py", "diff": "@@ -1 +1 @@\n+x = 1"}}


def test_v1_request_stays_identical_to_the_original_benchmark():
    original = Row(
        "o__r-1",
        "o/r",
        "2020",
        "Add tax",
        "d" * 10_000,
        [
            FileDiff("a.py", "@@ -1 +1 @@\n-old\n+new", 2),
            FileDiff("space name.py", "@@ -0,0 +1 @@\n+test()", 1),
        ],
        True,
        categories=["F.2"],
        fault_files=["a.py"],
    )
    request = questions.request(original.review_input(), "jev-1.13.0")
    # Captured from the pre-extraction implementation: wording, criteria, order of
    # file options, state shape, and description truncation are all part of v1.
    digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    assert digest == "bd299b14c0281588d76836f82eeb755da886b0b9d43f1c4414637556ce8ab775"
