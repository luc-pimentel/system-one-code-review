"""The questions every pull request is asked, frozen per version: the wording is part of the benchmark.

All of them go to Jev in one call per pull request. Two are yes/no questions (Noul: Jev returns the
probability of yes), two are Choice questions (a probability for each option). The criteria spell out what
counts as yes, and describe each option; the category definitions are SWR-Bench's own.
"""

from .swrbench import CATEGORIES, Row

VERSION = "v1"
MAX_DESCRIPTION_CHARS = 6000

CHANGES_REQUESTED = {
    "type": "noul",
    "instructions": (
        "Would a careful reviewer ask the author to change something in this pull request before merging it? "
        "The pull request is `pr_title`, `pr_description` and the diffs in `files`."
    ),
    "criteria": {
        "true": (
            "The reviewer would ask for at least one change: a fix to a functional defect, or an improvement to "
            "comments, names, formatting, code organization, implementation approach or tests."
        ),
        "false": "The reviewer would approve the pull request as it is.",
    },
}

FUNCTIONAL_DEFECT = {
    "type": "noul",
    "instructions": "Do the diffs in `files` introduce a functional defect: code that would behave incorrectly?",
    "criteria": {
        "true": (
            "At least one of: a wrong method call or violated API contract, a logic error, mishandled data or "
            "resources, a missing or wrong check, incorrect use of an external library or API, or an incomplete "
            "or inconsistent feature."
        ),
        "false": (
            "The code behaves correctly. Comments, names, formatting, organization and refactoring suggestions "
            "do not count."
        ),
    },
}

# Option names are category codes with the dots dropped (E.1.1 -> E11), so they are plain identifiers.
OPTIONS = {code.replace(".", ""): code for code in CATEGORIES}

PROBLEM_TYPE = {
    "type": "choice",
    "instructions": (
        "Suppose a reviewer asks the author for one change to this pull request. Which kind of change is it "
        "most likely to be?"
    ),
    "criteria": {option: f"{CATEGORIES[code][0]}: {CATEGORIES[code][1]}" for option, code in OPTIONS.items()},
}


FAULT_FILE_INSTRUCTIONS = (
    "Which file in `files` holds the code a reviewer is most likely to ask the author to change?"
)


def fault_file(row: Row) -> dict:
    """Asked when a pull request changes more than one file; each option is a file's path."""
    return {
        "type": "choice",
        "instructions": FAULT_FILE_INSTRUCTIONS,
        "criteria": {f"f{i}": f.path for i, f in enumerate(row.files)},
    }


def state(row: Row) -> dict:
    """What Jev reads: the title, the description and the diff of each file. Nothing from the review."""
    return {
        "pr_title": row.title,
        "pr_description": row.description[:MAX_DESCRIPTION_CHARS],
        "files": {f"f{i}": {"path": f.path, "diff": f.patch} for i, f in enumerate(row.files)},
    }


def questions(row: Row) -> dict:
    asked = {
        "changes_requested": CHANGES_REQUESTED,
        "functional_defect": FUNCTIONAL_DEFECT,
        "problem_type": PROBLEM_TYPE,
    }
    if len(row.files) > 1:  # with one file the answer is given
        asked["fault_file"] = fault_file(row)
    return asked


def request(row: Row, model: str) -> dict:
    return {"model": model, "state": state(row), "questions": questions(row)}
