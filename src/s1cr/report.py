"""`reports/jev-swrbench.md`: the evidence, generated from the scored runs."""

import re
from pathlib import Path

from . import questions
from .models import Question
from .score import PRICE_PER_MILLION_INPUT, Binary, Choice, Results
from .swrbench import CATEGORIES


def pct(value: float) -> str:
    return f"{value:.0%}"


def first_upper(text: str) -> str:
    return text[:1].upper() + text[1:]


def category(code: str) -> str:
    return f"{code} {CATEGORIES[code][0]}" if code in CATEGORIES else code


def interval(ci: tuple[float, float], fmt: str = ".2f") -> str:
    return f"[{ci[0]:{fmt}}, {ci[1]:{fmt}}]"


def pct_interval(ci: tuple[float, float]) -> str:
    return f"[{ci[0]:.0%}, {ci[1]:.0%}]"


def shown(name: str, value: float) -> str:
    """An extra Choice measure, formatted by what it is."""
    if name == "macro F1":  # a score, shown with two decimals
        return f"{value:.2f}"
    if name.startswith("median"):  # a count, shown whole
        return f"{value:.0f}"
    return pct(value)


def wording(question: Question) -> list[str]:
    """A yes/no question as the report quotes it: the instructions, then what counts as yes and as no."""
    criteria = question.get("criteria", {})
    lines = [f"> {question['instructions']}"]
    lines.extend(
        f">\n> *{'Yes' if answer == 'true' else 'No'}:* {criteria[answer]}"
        for answer in ("true", "false")
        if answer in criteria
    )
    return lines


def binary_section(out: list[str], b: Binary, question: Question) -> None:
    """One yes/no question's section of the report: ranking, calibration, accuracy and the groups."""
    add = out.append
    add("\n".join(wording(question)) + "\n")
    add(
        f"Asked of all {b.n} pull requests; the label is yes when {b.label} ({b.positives} of {b.n}, "
        f"{pct(b.positives / b.n)}).\n"
    )
    add("| Measure | Jev | Reference |\n|---|---:|---|")
    add(
        f"| AUROC | {b.auroc:.2f} {interval(b.auroc_ci)} | 0.50 is chance; the diff's size alone scores {b.size_auroc:.2f} |"
    )
    add(
        f"| Average precision | {b.average_precision:.2f} | {b.positives / b.n:.2f}, the share of yes cases |"
    )
    add(
        f"| Brier score | {b.brier:.3f} {interval(b.brier_ci, '.3f')} | {b.brier_prevalence:.3f} for always answering {b.positives / b.n:.2f} |"
    )
    add(f"| Calibration error (ECE) | {b.ece:.3f} | 0 when every probability matches its hit rate |")
    add(
        f"| Accuracy, yes at 0.5 or more | {pct(b.accuracy)} {pct_interval(b.accuracy_ci)} | {pct(b.majority)} for always giving the more common answer |"
    )
    add(f"| Mean probability of yes | {b.mean_yes:.2f} on yes cases, {b.mean_no:.2f} on no cases | |")
    add("")
    add("Calibration: Jev's probability against how often the answer was yes.\n")
    add("| Probability | Pull requests | Mean probability | Share that were yes |\n|---|---:|---:|---:|")
    for low, high, count, mean_p, rate in b.bins:
        add(f"| {low:.1f}–{high:.1f} | {count} | {mean_p:.2f} | {pct(rate)} |")
    add("")
    add(
        f"If Jev only answered when it was surest, taking its most confident answers first, it could handle "
        f"{pct(b.coverage_5)} of pull requests at 5% errors or less and {pct(b.coverage_10)} at 10% or less. "
        f"The area under that risk curve is {b.aurc:.3f}, against {b.aurc_random:.3f} for its answers taken in "
        "random order.\n"
    )
    eras = ", ".join(f"{era}: {auroc:.2f} ({n} pull requests)" for era, (n, auroc) in b.by_era.items())
    add(f"AUROC by era: {eras}.\n")
    add("| Repository | Pull requests | AUROC |\n|---|---:|---:|")
    for repo, (n, auroc) in sorted(b.by_repo.items(), key=lambda item: -item[1][0]):
        add(f"| `{repo}` | {n} | {'–' if auroc != auroc else f'{auroc:.2f}'} |")
    add("")


def choice_section(out: list[str], c: Choice, instructions: str, asked: str) -> None:
    """One Choice question's section of the report: accuracy against the baselines, and the mistakes."""
    add = out.append
    add(f"> {instructions}\n")
    add(f"{asked} Jev's most likely option counts as right when it is {c.label}.\n")
    add("| | Accuracy |\n|---|---:|")
    add(f"| Jev | {pct(c.accuracy)} {pct_interval(c.accuracy_ci)} |")
    for name, value in c.baselines.items():
        add(f"| {first_upper(name)} | {pct(value)} |")
    add(f"| Jev, its more confident half | {pct(c.confident[0])} |")
    add(f"| Jev, its less confident half | {pct(c.confident[1])} |")
    for name, value in c.extra.items():
        add(f"| {first_upper(name)} | {shown(name, value)} |")
    add("")
    if c.confusion:  # there were mistakes to show
        add("Its most common mistakes:\n")
        add("| Reviewers asked for | Jev picked | Times |\n|---|---|---:|")
        for truth, picked, n in c.confusion:
            add(f"| {category(truth)} | {category(picked)} | {n} |")
        add("")


def data_section(out: list[str], r: Results) -> None:
    add = out.append
    add("## 1. The data\n")
    eras = ", ".join(f"{n} from {era}" for era, n in sorted(r.eras.items()))
    add(
        f"SWR-Bench holds {r.total} pull requests from 12 Python projects. {r.kept} are scored ({eras}); "
        f"{r.total - r.kept} are left out:\n"
    )
    for reason, n in sorted(r.excluded.items(), key=lambda item: -item[1]):
        add(f"- {n}: {reason}")
    add("")
    add(
        "Jev sees each pull request as its first reviewer did: the title, the description (up to "
        f"{questions.MAX_DESCRIPTION_CHARS:,} characters) and the diff of every file, as of the last commit "
        "before the first review. It never sees the review, the later commits or the fixes. GitHub's own diff "
        "of a pull request shows its final state, fixes included, so for pull requests with several commits "
        "the reviewed diff comes from comparing the base commit with the last commit before the review.\n"
    )
    add(
        "The labels are what the reviewers asked for. A pull request counts as *changes requested* when a "
        "reviewer asked for at least one change the author then made; SWR-Bench's annotators sorted each "
        "request into functional (the code does not work) or evolvability (it works, but could be clearer or "
        "better built) categories.\n"
    )


def write_report(r: Results, output: Path) -> None:
    """Write the benchmark report: what was measured, each question's section, and what it adds up to."""
    out: list[str] = []
    add = out.append
    runs = r.stability.runs if r.stability else [r.primary]
    add("# Jev on SWR-Bench\n")
    add(
        "How well does TypeSafe's Jev, a System One model that answers with probabilities instead of writing "
        f"text, review pull requests? Generated by `uv run s1cr score` from SWR-Bench (`{r.source}`) and "
        f"{len(runs)} runs of `{r.model}`, questions {questions.VERSION}. The headline numbers are from run "
        f"`{r.primary}`.\n"
    )

    add("## Summary\n")
    add("| Question | Cases | Jev | Reference |\n|---|---:|---|---|")
    c, f = r.changes, r.functional
    add(
        f"| Would a reviewer ask for changes? | {c.n} ({c.positives} yes) | AUROC {c.auroc:.2f}, accuracy "
        f"{pct(c.accuracy)} | chance 0.50; diff size {c.size_auroc:.2f}; majority {pct(c.majority)} |"
    )
    add(
        f"| Does it introduce a functional defect? | {f.n} ({f.positives} yes) | AUROC {f.auroc:.2f}, accuracy "
        f"{pct(f.accuracy)} | chance 0.50; diff size {f.size_auroc:.2f}; majority {pct(f.majority)} |"
    )
    t, w = r.problem_type, r.fault_file
    add(
        f"| Which kind of change? | {t.n} | accuracy {pct(t.accuracy)} | "
        + "; ".join(f"{name} {pct(value)}" for name, value in t.baselines.items())
        + " |"
    )
    add(
        f"| Which file? | {w.n} | accuracy {pct(w.accuracy)} | "
        + "; ".join(f"{name} {pct(value)}" for name, value in w.baselines.items())
        + " |"
    )
    if r.stability:  # more than one run was scored
        s = r.stability
        add(
            f"| Asked again | {s.n} × {len(s.runs)} runs | yes/no answers flip for "
            f"{pct(s.flips['changes_requested'])} and {pct(s.flips['functional_defect'])} | |"
        )
    add(
        f"| Speed and cost | {r.cost.calls} calls | median {r.cost.ms_median:.0f} ms, "
        f"${r.cost.dollars:.2f} of input tokens | at TypeSafe's quoted ${PRICE_PER_MILLION_INPUT} per million |"
    )
    add("")

    findings(out, r)
    data_section(out, r)

    add("## 2. Would a reviewer ask for changes?\n")
    binary_section(out, r.changes, questions.CHANGES_REQUESTED)

    add("## 3. Does it introduce a functional defect?\n")
    binary_section(out, r.functional, questions.FUNCTIONAL_DEFECT)
    v = r.functional_vs_clean
    add(
        f"Leaving out the pull requests whose only problems were evolvability ones, so that functional problems "
        f"face only pull requests approved as they were: AUROC {v.auroc:.2f} {interval(v.auroc_ci)} over "
        f"{v.n} pull requests.\n"
    )

    add("## 4. Which kind of change?\n")
    choice_section(
        out,
        r.problem_type,
        questions.PROBLEM_TYPE["instructions"],
        f"Scored on the {t.n} pull requests where reviewers asked for exactly one change. The options are "
        f"SWR-Bench's {len(questions.OPTIONS)} categories, each described with its annotators' definition.",
    )

    add("## 5. Which file?\n")
    choice_section(
        out,
        r.fault_file,
        questions.FAULT_FILE_INSTRUCTIONS,
        f"Asked whenever a pull request changes more than one file, with each file's path as an option; scored "
        f"on the {w.n} such pull requests where reviewers asked for changes and the problem's file is known.",
    )

    if r.stability:  # more than one run was scored
        s = r.stability
        add("## 6. Asking again\n")
        add(
            f"Every pull request went to Jev {len(s.runs)} times with the same input ({s.n} answered every "
            "time). Jev's answers are not identical from one call to the next:\n"
        )
        add("| Question | Mean change between two runs | 95% of spreads within | Answer at 0.5 flips |")
        add("|---|---:|---:|---:|")
        for key, name in (
            ("changes_requested", "Changes requested"),
            ("functional_defect", "Functional defect"),
        ):
            add(f"| {name} | {s.mean_change[key]:.3f} | {s.spread_95[key]:.3f} | {pct(s.flips[key])} |")
        add("")
        add(
            "Choice questions picked the same top option in every run for "
            + " and ".join(
                f"{pct(value)} of the {name.replace('_', ' ')} answers" for name, value in s.agreement.items()
            )
            + ".\n"
        )

    add("## 7. Speed and cost\n")
    models = ", ".join(f"`{m}` ({n})" for m, n in r.cost.models.items())
    add(
        f"Run `{r.primary}`: {r.cost.calls} calls, one per pull request with every question in it, answered by "
        f"{models}. Median call {r.cost.ms_median:.0f} ms, 90th percentile {r.cost.ms_90:.0f} ms, measured "
        f"from this machine with 4 calls in flight. {r.cost.input_tokens:,} input tokens (median "
        f"{r.cost.median_input:.0f} per call) and {r.cost.output_tokens:,} output tokens. At TypeSafe's quoted "
        f"${PRICE_PER_MILLION_INPUT} per million input tokens that is ${r.cost.dollars:.2f}; output tokens are "
        "not priced here.\n"
    )

    add("## What this does not show\n")
    add(
        "- **Reviewers miss things.** An approved pull request can still hold a bug nobody caught, and whether "
        "an evolvability change is worth asking for is partly taste.\n"
        "- **The data is public.** These pull requests are on GitHub, so Jev may have seen them in training. "
        "The 2024–25 slice is the least exposed.\n"
        "- **One wording.** Every question has one frozen wording (questions "
        f"{questions.VERSION}); other wordings would score differently.\n"
        "- **Diff only.** Jev sees the diff, not the rest of the repository, which human reviewers could read.\n"
    )

    add("## Reproduce\n")
    add(
        "```sh\n"
        "uv sync\n"
        "uv run s1cr fetch                  # SWR-Bench at the pinned commit\n"
        "uv run s1cr build                  # rows as Jev reads them; GitHub CLI login needed\n"
        f"TYPESAFE_API_KEY=... uv run s1cr run {r.primary} --model {r.model}\n"
        "uv run s1cr score                  # rewrites this report\n"
        "```"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(re.sub(r"\n{3,}", "\n\n", "\n".join(out)) + "\n")


def findings(out: list[str], r: Results) -> None:
    """The numbers in words. Every sentence is chosen by the numbers, so a rerun on another model or
    version keeps the words true."""
    add = out.append
    c, f, t, w = r.changes, r.functional, r.problem_type, r.fault_file
    add("## What the numbers say\n")

    def ranking(b: Binary) -> str:
        low, _high = b.auroc_ci
        if low > 0.5:  # the interval clears chance
            return (
                f"ranks them above chance but not by much (AUROC {b.auroc:.2f})"
                if b.auroc < 0.7
                else (f"ranks them well (AUROC {b.auroc:.2f})")
            )
        return f"ranks them no better than chance (AUROC {b.auroc:.2f}, interval reaching 0.50)"

    def probabilities(b: Binary) -> str:
        if b.brier_ci[0] > b.brier_prevalence:  # even the interval's low end is worse than a constant
            return (
                f"its probabilities are worse than a constant: always answering {b.positives / b.n:.2f} scores a "
                f"Brier of {b.brier_prevalence:.3f} against Jev's {b.brier:.3f}"
            )
        if b.brier_ci[1] < b.brier_prevalence:  # even the interval's high end beats a constant
            return f"its probabilities beat a constant (Brier {b.brier:.3f} against {b.brier_prevalence:.3f})"
        return f"its probabilities are no better than a constant (Brier {b.brier:.3f} against {b.brier_prevalence:.3f})"

    add(
        f"- **Whether a reviewer would ask for changes:** Jev {ranking(c)}, and {probabilities(c)}. It gives "
        f"pull requests reviewers asked to change a mean {c.mean_yes:.2f} and approved ones {c.mean_no:.2f}."
    )
    at_half = (
        "at 0.5 it is no more accurate than always answering no"
        if f.accuracy <= f.majority + 0.005
        else f"at 0.5 it is right {pct(f.accuracy)} of the time, against {pct(f.majority)} for always answering no"
    )
    add(
        f"- **Whether it introduces a functional defect:** Jev {ranking(f)}; {probabilities(f)}, and {at_half}."
    )
    best_constant = max(t.baselines.values())
    add(
        f"- **Which kind of change:** right {pct(t.accuracy)} of the time, "
        f"{t.accuracy / best_constant:.1f} times the best fixed answer ({pct(best_constant)}), and "
        f"{pct(t.extra['functional or evolvability right'])} of the time at least on functional against "
        "evolvability."
    )
    best_file = max(w.baselines.values())
    add(
        f"- **Which file:** right {pct(w.accuracy)} of the time, against {pct(best_file)} for the best rule that "
        f"reads no code; on its more confident half, {pct(w.confident[0])}."
    )
    usable = max(c.coverage_10, f.coverage_10)
    add(
        f"- **Acting on its own:** on neither yes/no question does trusting only Jev's surest answers keep "
        f"errors at 10% or less for more than {pct(usable)} of pull requests. On this data its yes/no answers "
        "are not reliable enough to approve or block a pull request alone."
        if usable < 0.25
        else f"- **Acting on its own:** trusting only its surest answers keeps errors at 10% or less for up to "
        f"{pct(usable)} of pull requests."
    )
    if r.stability:  # more than one run was scored
        s = r.stability
        add(
            f"- **Consistency:** asked again, its yes/no answers flip for "
            f"{pct(max(s.flips.values()))} of pull requests or fewer, and its Choice answers agree "
            f"{pct(min(s.agreement.values()))} of the time or more."
        )
    add(
        f"- **Cost:** {r.cost.calls} pull requests for ${r.cost.dollars:.2f} of input tokens, "
        f"median {r.cost.ms_median:.0f} ms each.\n"
    )
