"""The questions every pull request is asked, frozen per version: the wording is part of the benchmark.

All of them go to Jev in one call per pull request. Two are yes/no questions (Noul: Jev returns the
probability of yes), two are Choice questions (a probability for each option). The criteria spell out what
counts as yes, and describe each option; the category definitions are SWR-Bench's own.
"""

from .models import ReviewInput

VERSION = "v1"
MAX_DESCRIPTION_CHARS = 6000

# SWR-Bench's categories, with the definitions its annotators worked from (swrbench/collect_pr_review.py).
# E is evolvability: the code works, but could be clearer or better built. F is functional: it does not work.
CATEGORIES: dict[str, tuple[str, str]] = {
    "E.1.1": (
        "Textual Changes",
        "Adjustments to comments (e.g., adding, correcting, clarifying) or identifier names (variables, "
        "functions, classes) for better clarity and consistency.",
    ),
    "E.1.2": (
        "Language Features",
        "Utilizing language-specific constructs (e.g., `final` in Java, type annotations, access modifiers) "
        "primarily to convey developer intent, constraints, or information, rather than for functional impact.",
    ),
    "E.2": (
        "Visual Representation",
        "Modifications to code formatting and layout, such as indentation, spacing, line breaks, or bracket "
        "placement, to improve visual clarity and adhere to style conventions.",
    ),
    "E.3.1": (
        "Organization",
        "Reorganizing code elements, such as removing dead (unused) code, moving functions or classes to more "
        "appropriate locations, or restructuring files/packages for better modularity.",
    ),
    "E.3.2": (
        "Solution Approach",
        "Modifying the internal implementation details or algorithms (e.g., refactoring for clarity/efficiency, "
        "updating function usage to newer patterns), or adding supporting code like tests, without altering the "
        "observable functionality.",
    ),
    "F.1": (
        "Interface",
        "Fixes related to how different code components interact, including incorrect method calls, wrong "
        "parameter types/values, violated API contracts, or incorrect event handling.",
    ),
    "F.2": (
        "Logic",
        "Corrections to errors in algorithms, conditional statements (if/else), loops, computations, or other "
        "logical constructs leading to incorrect behavior.",
    ),
    "F.3": (
        "Resource",
        "Fixes concerning the management of data, variables, or system resources, including initialization "
        "errors, memory leaks, improper resource release/acquisition, or incorrect data manipulation (e.g., "
        "concurrency issues).",
    ),
    "F.4": (
        "Check",
        "Adding or modifying validation or checks (e.g., null checks, boundary checks, state validation) for "
        "variables, parameters, or function return values to handle potential errors or invalid states correctly.",
    ),
    "F.5": (
        "Support",
        "Corrections related to the interaction with external systems, libraries, frameworks, or APIs (e.g., "
        "incorrect usage, adapting to API changes, version incompatibilities).",
    ),
    "F.6": (
        "Larger Defects",
        "Significant functional fixes that often span multiple files or components, address incompletely "
        "implemented features, fix major inconsistencies (like GUI behavior), or require broader system knowledge.",
    ),
}

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


def fault_file(row: ReviewInput) -> dict:
    """Asked when a pull request changes more than one file; each option is a file's path."""
    return {
        "type": "choice",
        "instructions": FAULT_FILE_INSTRUCTIONS,
        "criteria": {f"f{i}": f.path for i, f in enumerate(row.files)},
    }


def state(row: ReviewInput) -> dict:
    """What Jev reads: the title, the description and the diff of each file. Nothing from the review."""
    return {
        "pr_title": row.title,
        "pr_description": row.description[:MAX_DESCRIPTION_CHARS],
        "files": {f"f{i}": {"path": f.path, "diff": f.patch} for i, f in enumerate(row.files)},
    }


def questions(row: ReviewInput) -> dict:
    asked = {
        "changes_requested": CHANGES_REQUESTED,
        "functional_defect": FUNCTIONAL_DEFECT,
        "problem_type": PROBLEM_TYPE,
    }
    if len(row.files) > 1:  # with one file the answer is given
        asked["fault_file"] = fault_file(row)
    return asked


def request(row: ReviewInput, model: str) -> dict:
    return {"model": model, "state": state(row), "questions": questions(row)}
