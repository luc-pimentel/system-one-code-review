"""Reading and writing demo's files, and asking its service."""

import json
import os
import subprocess
from pathlib import Path

import httpx

from . import stores
from .models import Item, Score


def load(path: Path = stores.ITEMS) -> list[Item]:
    """The items demo keeps."""
    if not path.exists():  # nothing was kept yet
        raise FileNotFoundError(f"{path} is missing")
    return [Item(**json.loads(line)) for line in path.read_text().splitlines()]


def save(scores: list[Score], run: Path) -> None:
    """Append the scores to the run's results."""
    output = stores.results(run)
    with output.open("a") as stream:
        stream.write("".join(json.dumps(score) + "\n" for score in scores))


def ask(client: httpx.Client, item: Item) -> Score:
    """The service's score for one item."""
    response = client.post(stores.API, json={"name": item.name})
    return {"item": item.name, "value": response.json()["value"]}


def version() -> str:
    """The tool's version."""
    return subprocess.run([stores.TOOL, "--version"], capture_output=True, text=True, check=True).stdout


def token() -> str:
    """The token demo was given."""
    return os.environ.get("DEMO_TOKEN", "")


def stray(path: Path) -> str:
    """A file the catalog does not name."""
    return path.read_text()
