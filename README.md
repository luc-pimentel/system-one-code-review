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
TYPESAFE_API_KEY=... uv run s1cr run r1 --model jev-1.13.0
uv run s1cr score                                     # reports/jev-swrbench.md
uv run pytest
```

`runs/<name>/answers.jsonl` keeps every raw answer, so `score` needs no API calls. A run that stops can be
started again with the same name and picks up where it left off.
