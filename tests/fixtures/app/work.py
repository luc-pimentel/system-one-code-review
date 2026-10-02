"""Functions the sample CLI reaches, plus one it does not."""


def download(args):
    if not args:  # nothing was asked for
        return []
    return [args]


def parse(item):
    if item is None:  # nothing to parse
        return ""
    return str(item)


def load(args):
    for item in download(args):  # each downloaded item
        if parse(item):  # a real item
            return [item]
    return []


def finish(items):
    return len(items)


def unused(x):
    if x:  # never reached from the CLI
        return 1
    return 0
