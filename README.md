# System One Code Review

How well do System One models review pull requests? A System One model answers a fixed question with
probabilities instead of writing text: TypeSafe's Jev is the first one measured here, on SWR-Bench's
1,000 real GitHub pull requests.

The results are in [`reports/jev-swrbench.md`](reports/jev-swrbench.md).

## How it works

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

## Run it

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
