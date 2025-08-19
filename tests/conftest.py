import pytest


def pytest_configure(config):
    # Register custom marks used by some tests to silence warnings
    config.addinivalue_line(
        "markers",
        "slow: marks tests as slow (deselect with '-m \"not slow\"')",
    )

import os
import sys


def _add_repo_root_to_path():
    here = os.path.dirname(__file__)
    root = os.path.abspath(os.path.join(here, ".."))
    if root not in sys.path:
        sys.path.insert(0, root)


_add_repo_root_to_path()

# Ensure default docker runtime is standard runc during tests unless overridden
os.environ.setdefault("RAY_DOCKER_RUNTIME", "runc")


