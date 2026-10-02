"""A script with a main, shaped like scripts/decisions.py."""

from tests.fixtures.tools import two


def main(argv=None):
    if argv:  # arguments were given
        return two.count(argv)
    return 0


def spare(x):
    if x:  # only tests call this
        return 1
    return 0
