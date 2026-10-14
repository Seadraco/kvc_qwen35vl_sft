"""Print the maintained standalone Dockerfile for compatibility with old tooling.

The root Dockerfile is now edited directly. This command no longer generates
embedded dependency locks or diagnostic scripts. It only writes to stdout.
The project's own root Dockerfile is the canonical company image recipe.
"""
from pathlib import Path
import sys


WORKSPACE = Path(__file__).resolve().parents[1]


def render():
    return (WORKSPACE / "Dockerfile").read_text()


if __name__ == "__main__":
    sys.stdout.write(render())
