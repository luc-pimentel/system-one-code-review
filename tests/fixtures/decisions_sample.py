"""Sample functions for the decisions tests: every construct the walker handles."""

from contextlib import nullcontext


def guarded(workers: int, path) -> str:
    """Create a receipt once."""
    if workers < 1:  # fewer than one worker was asked for
        raise ValueError("workers must be at least 1")
    if path.exists():  # a receipt already exists -> reuse it
        saved = path.read_text()
        if not saved:  # the saved receipt is empty
            raise ValueError(f"{path} is empty")
        return saved
    return "new"


def looping(files, rows):
    total = 0
    for file in files:
        if not file:  # a blank entry
            continue
        total += 1
    # the rows run out before the files do
    while total < len(rows):
        total += 1
    return total


def attempts(call):
    try:
        value = call()
    except (OSError, ValueError) as error:  # the call failed
        raise SystemExit(1) from error
    else:
        value += 1
    finally:
        print("done")
    return value


def branches(kind):
    result = 0
    if kind == "a":  # the first kind
        result = 1
    elif kind == "b":
        result = 2
    else:
        if kind:  # anything else that is set
            result = 3
    return result


def matching(command):
    match command:
        case "run":  # a run was asked for
            return 1
        case _:
            return 0


def nesting(items):
    def helper(item):
        return item

    with nullcontext(items) as handle:
        for item in items:
            helper(item)
    return handle


class Holder:
    def method(self, flag):
        if flag:
            print("set")
