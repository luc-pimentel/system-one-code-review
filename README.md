# System One Code Review

How well do System One models review pull requests? A System One model answers a fixed question with
probabilities instead of writing text: TypeSafe's Jev is the first one measured here, on SWR-Bench's
1,000 real GitHub pull requests.

The results are in [`reports/jev-swrbench.md`](reports/jev-swrbench.md). The same reviewer can assess a live
GitHub pull request from the command line.

## Review a pull request

Needs [uv](https://docs.astral.sh/uv/), a logged-in [GitHub CLI](https://cli.github.com/) (`gh auth login`),
and `TYPESAFE_API_KEY` in the environment.

```sh
uv sync
export TYPESAFE_API_KEY=...
uv run s1cr review https://github.com/owner/repo/pull/123
uv run s1cr review https://github.com/owner/repo/pull/123 --json
uv run s1cr review https://github.com/owner/repo/pull/123 --model jev-1.13.0
```

`review` reads the PR's current title, description (up to 6,000 characters), and per-file diff, then asks
the frozen v1 questions in one Jev call. It defaults to `jev-latest`; use `--model jev-1.13.0` to use the
model in the published baseline. It records the base and head commits and checks that the PR did not
change while its files were fetched.

The readable output shows Jev's probabilities of changes requested and a functional defect, the likely
change category and file, their top-ranked options, token usage, and Jev-call latency. This is a structured
assessment, not generated explanations or line-level findings. Category and file choices indicate where
a change would most likely be needed; they do not establish that a defect exists. A single-file PR has no
file-choice score.

`--json` writes one JSON object to stdout with `pull_request` metadata, the raw `answers`, `questions`
version, resolved `model`, `usage`, `ms`, and `file_options` / `category_options` mappings. Probabilities
keep their full precision. Successful assessments exit 0 regardless of their scores; errors exit nonzero
with a message on stderr. The command only reads GitHub and prints the assessment locally.

The initial reviewer requires a complete text diff. It reports an error when GitHub omits or truncates a
patch (including binary or rename-only files), returns an incomplete file list, or the PR has no changes.
It uses the benchmark's 90,000-character diff/title preflight limit; Jev may also reject an input that
exceeds its token limit.

### Shared reviewer

Both `s1cr review` and the benchmark runner call `s1cr.jev.review`:

```python
from s1cr.jev import review
from s1cr.models import FileDiff, ReviewConfig, ReviewInput

result = review(
    ReviewInput("Fix tax", "Handle empty inputs.", [FileDiff("tax.py", "@@ -1 +1 @@\n-old\n+new", 2)]),
    ReviewConfig(model="jev-1.13.0"),
    api_key="...",
)
print(result.to_dict())
```

`ReviewInput` contains only title, description, and file diffs. SWR-Bench labels stay in its `Row` and
are stripped by `Row.review_input()`. `ReviewResult` preserves the raw Jev answers and execution metadata;
the benchmark adds the row ID when saving it to the existing `answers.jsonl` format. The retry policy
continues to follow [`worktree-repos/scripts/review/jev-review.mjs`](https://github.com/luc-pimentel/worktree-repos/blob/main/scripts/review/jev-review.mjs).

## Checks

Every pull request runs `ruff check`, `ruff format --check` and `pytest`, and gets one comment that
sums them up and lists the functions the pull request touched, decision by decision. A decision is
what ruff's complexity rule counts: an `if`, a loop, an `except`. Each one in `src/` and `scripts/` carries a
comment that reads as its condition in plain words, so the list reads like prose:

```sh
uv run python -m scripts.decisions src/s1cr/github.py   # one file, top to bottom
uv run python -m scripts.decisions                       # src and scripts, in the order their entry points reach them
uv run python -m scripts.decisions --changed main        # what this branch changed
```

The package and the pull request comment list functions in the order their entry point reaches them:
`s1cr`'s subcommands for `src/`, each script's `main` for `scripts/`, with the caller named when it is not
the entry point itself. Functions nothing reaches come last.

## How the benchmark works

- **Data:** [SWR-Bench](https://github.com/ZZR0/SWRench) (MIT), pinned to one commit: 500 pull requests
  reviewers asked to change and 500 they approved as they were, from 12 Python projects, with every
  problem the reviewers found and its category.
- **What the model reads:** the title, the description and the diff as of the last commit before the
  first review. Never the review, the later commits or the fixes. Pull requests with several commits are
  rebuilt with GitHub's compare API, because GitHub's own diff shows the final state, fixes included.
- **Questions** (frozen as v1 in `src/s1cr/questions.py`): would a reviewer ask for changes, does it
  introduce a functional defect (both yes/no), which kind of change (SWR-Bench's 11 categories) and which
  file (both Choice).
- **Scores:** AUROC, average precision, Brier score and calibration for the yes/no questions, risk against
  coverage for acting only on confident answers, accuracy against baselines for the Choice questions,
  run-to-run stability, latency and cost. Intervals come from bootstrapping the pull requests.

## Run the benchmark

Needs [uv](https://docs.astral.sh/uv/), a logged-in GitHub CLI (for the compare API) and a TypeSafe key.

```sh
uv sync
uv run s1cr fetch                                     # SWR-Bench into data/
uv run s1cr build                                     # data/rows.jsonl, one row per pull request
TYPESAFE_API_KEY=... uv run s1cr run baseline --model jev-1.13.0
uv run s1cr score --primary baseline                  # reports/jev-swrbench.md
uv run pytest
```

`runs/<name>/answers.jsonl` keeps every raw answer, so `score` needs no API calls. New runs also save
`run.json`: the source Git commit, pinned model, question version, worker count, UTC creation time, exact
selected/eligible PR IDs, and a SHA-256 fingerprint of their inputs and labels. Git is the implementation's
version; run names identify executions, including repeated executions of the same commit.

Benchmark runs require committed source and an explicit pinned `--model` such as `jev-1.13.0`.
`jev-latest` remains available for live reviews. Git state is read from the checkout containing the
executed `s1cr` code, including detached worktrees. Changes under `runs/`, `reports/`, and `data/` are
excluded from the clean-source check so saved results do not block another run.

Repeat the same command to resume an interrupted run. The commit, model, selected cases, inputs, labels,
and worker count must still match the receipt; changing any of them requires a new run name. Failed cases
are retried, successful cases are skipped, and any failed calls produce a nonzero exit status. Jev must
return the requested model version for an answer to count as successful.

The historical `r1`, `r2`, and `r3` remain readable with `s1cr score`. They have no Git receipts and cannot
be resumed or used in the new comparison command; start fresh recorded runs rather than invent their
provenance. `score` groups new repeatability runs by matching commit and execution settings, separately
from historical runs.

## Compare Git revisions

Commit the candidate implementation on an experiment branch. Use normal Git worktrees for separate
baseline and candidate checkouts:

```sh
git worktree add --detach ../s1cr-baseline <baseline-sha>
git worktree add --detach ../s1cr-candidate <candidate-sha>
```

Both revisions must support recorded runs. In each checkout, install its locked dependencies with
`uv sync --locked`, prepare the same benchmark rows with `s1cr fetch` / `s1cr build`, and run:

```sh
uv run --locked s1cr run trial --model jev-1.13.0
```

Then compare their saved run directories from one evaluator checkout:

```sh
uv run s1cr compare ../s1cr-baseline/runs/trial ../s1cr-candidate/runs/trial
uv run s1cr compare baseline candidate --json
```

Arguments can be names under `runs/` or paths to run directories. `--rows /path/to/rows.jsonl` selects
the benchmark inputs and labels (default `data/rows.jsonl`). The comparison verifies their fingerprint
against both receipts and evaluates both sets of raw answers with the current evaluator; its Git commit
and dirty status are included in the output. Comparing requires no API key or network calls.

The table shows functional-defect AUROC, accuracy at 0.5, Brier score, changes-requested AUROC,
category/file accuracy on their eligible subsets, latency, token counts, and estimated input cost.
Delta means candidate minus baseline. Cost uses the same quoted input price as the original report;
output tokens and failed/retried calls are not priced.

Completion, failed, pending, and excluded counts are always shown. Scores, latency, and cost use only
PRs answered by both runs, with each metric's case count displayed. JSON also includes the matched IDs
and IDs answered by only one run. Empty subsets and undefined metrics are `null` in JSON / `n/a` in text.
Use this command for small or unfinished trials as well as full runs; a lower completion rate remains
visible alongside quality on the matched cases.
