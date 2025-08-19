import json

from tool_calling.tool_yaml_loader import load_yaml_tools
from tool_calling.provider_schema import build_openai_tools_schema_from_yaml


def test_provider_schema_names_roundtrip():
    """Ensure YAML -> OpenAI tools schema preserves tool names we expect to execute."""
    loaded = load_yaml_tools("implementations/tools/defs")
    tools_schema = build_openai_tools_schema_from_yaml(loaded.tools)
    names = [t["function"]["name"] for t in tools_schema]
    # sanity
    assert "run_shell" in names
    assert "read_file" in names


