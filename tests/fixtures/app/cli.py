"""A small CLI shaped like s1cr's, for the entry-order tests."""

import argparse

from . import work
from .work import finish


def fetch(args):
    return work.download(args)


def run(args):
    if args.limit:  # a limit was given
        return work.load(args)[: args.limit]
    return finish(work.load(args))


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    fetch_cmd = commands.add_parser("fetch")
    fetch_cmd.set_defaults(func=fetch)
    run_cmd = commands.add_parser("run")
    run_cmd.add_argument("--limit", type=int)
    run_cmd.set_defaults(func=run)
    args = parser.parse_args(argv)
    args.func(args)
