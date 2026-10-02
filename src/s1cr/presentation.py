"""Readable and machine-readable output for a live PR assessment."""

from . import questions
from .github import PullRequest
from .models import ReviewResult


def document(pr: PullRequest, result: ReviewResult) -> dict:
    """Keep raw answers and supply the option mappings needed to interpret them."""
    return {
        "pull_request": {
            "url": pr.url,
            "title": pr.input.title,
            "base_sha": pr.base_sha,
            "head_sha": pr.head_sha,
        },
        **result.to_dict(),
        "file_options": {f"f{i}": file.path for i, file in enumerate(pr.input.files)},
        "category_options": {
            option: {"code": code, "name": questions.CATEGORIES[code][0]}
            for option, code in questions.OPTIONS.items()
        },
    }


def readable(pr: PullRequest, result: ReviewResult) -> str:
    answers = result.answers
    category_names = {
        option: f"{code} {questions.CATEGORIES[code][0]}" for option, code in questions.OPTIONS.items()
    }
    file_names = {f"f{i}": file.path for i, file in enumerate(pr.input.files)}
    lines = [
        f"{pr.url} — {pr.input.title}",
        f"Reviewed head: {pr.head_sha}",
        f"Base: {pr.base_sha}",
        f"Model: {result.model} | Questions: {result.questions} | Jev call: {result.ms} ms",
        "",
        f"Jev probability of changes requested: {answers['changes_requested']['noul']:.1%}",
        f"Jev probability of a functional defect: {answers['functional_defect']['noul']:.1%}",
        "",
        "Category and file choices describe where a change would most likely be needed:",
    ]

    def choice(label: str, answer: dict, names: dict[str, str]) -> None:
        lines.append(f"{label}: {names[answer['choice']]} (confidence {answer['confidence']:.1%})")
        ranked = sorted(answer["probabilities"].items(), key=lambda item: item[1], reverse=True)
        for option, probability in ranked[:3]:
            lines.append(f"  {probability:.1%}  {names[option]}")

    choice("Change category", answers["problem_type"], category_names)
    if "fault_file" in answers:
        choice("File most likely to need changes", answers["fault_file"], file_names)
    else:
        lines.append(f"Only changed file: {pr.input.files[0].path} (file-choice question not asked)")
    if result.usage:
        lines.append(
            f"Tokens: {result.usage.get('input_tokens', 'unknown')} input, "
            f"{result.usage.get('output_tokens', 'unknown')} output"
        )
    return "\n".join(lines)
