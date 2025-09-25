import pytest


def pytest_configure(config):
    # Register custom marks used by some tests to silence warnings
    config.addinivalue_line(
        "markers",
        "slow: marks tests as slow (deselect with '-m \"not slow\"')",
    )

import sys, os

# Ensure project root is on sys.path so legacy test imports work
ROOT = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(ROOT, os.pardir))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

# Also add agentic_coder_prototype and kylecode for direct imports
ACP = os.path.join(PROJ, 'agentic_coder_prototype')
if ACP not in sys.path:
    sys.path.insert(0, ACP)
KYLE = os.path.join(PROJ, 'kylecode')
if KYLE not in sys.path:
    sys.path.insert(0, KYLE)

# Ensure default docker runtime is standard runc during tests unless overridden
os.environ.setdefault("RAY_DOCKER_RUNTIME", "runc")

# Skip LSP-heavy tests when flagged (avoids long waits and external tool deps)
_SKIP_LSP = os.environ.get("RAY_SCE_SKIP_LSP", "0") == "1"
_LSP_FILES = {
    "tests/test_lsp_integration.py",
    "tests/test_lsp_manager_v2.py",
    "tests/test_lsp_symbols.py",
    "tests/test_sandbox_lsp_integration.py",
    # Agent session/storage tests rely on LSP actors; skip under LSP skip flag
    "tests/test_agent_session.py",
    "tests/test_agent_storage.py",
    # LSP deployment suite depends on sandbox_lsp_integration; skip under flag
    "tests/test_lsp_deployment.py",
}

def pytest_collection_modifyitems(session, config, items):
    if not _SKIP_LSP:
        return
    skip_marker = pytest.mark.skip(reason="Skipping LSP tests (RAY_SCE_SKIP_LSP=1)")
    for item in items:
        fpath = str(item.fspath)
        for lsp in _LSP_FILES:
            if fpath.endswith(lsp):
                item.add_marker(skip_marker)
                break


