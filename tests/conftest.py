"""Shared fixtures.

`workdir` exists because pytest's `tmp_path` is unusable on this machine:
directories created with mode 0o700 (what `tempfile.mkdtemp` uses) end up
inaccessible to the very process that created them, so every tmp_path-based
test fails at setup. We create a plain directory inside the workspace instead.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

ARTIFACTS = Path(".cache") / "test-artifacts"


@pytest.fixture()
def workdir(request: pytest.FixtureRequest) -> Iterator[Path]:
    safe_name = request.node.name.replace("[", "_").replace("]", "").replace("/", "_")
    base = ARTIFACTS / safe_name
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)
