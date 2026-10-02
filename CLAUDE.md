# system-one-code-review

- Before pushing, run `uv run ruff check`, `uv run ruff format --check` and `uv run pytest`. CI runs the same and posts one sticky comment per pull request.
- Ruff fails a function with more than 9 decisions (mccabe complexity 10). Split the function; do not add `# noqa`.
- Every counted decision in `src/` (an `if`, `elif`, `except` or `while`) carries a comment that reads as its condition in plain words: `if workers < 1:  # fewer than one worker was asked for`. Put it on the line above when the line would get long. The comment may name the outcome after `->`: `# a receipt already exists -> reuse it`.
- `uv run python -m scripts.decisions src/s1cr/github.py` renders a file's functions with their decisions; `--changed main` shows only what a branch changed. Packages and the PR comment follow the order `s1cr` reaches the functions, grouped by subcommand; one file lists top to bottom. A decision without a phrase shows 🟡.
