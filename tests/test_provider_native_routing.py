from tool_calling.tool_yaml_loader import load_yaml_tools
from tool_calling.provider_schema import filter_tools_for_provider_native, build_openai_tools_schema_from_yaml


def test_native_filter_and_schema_build():
    loaded = load_yaml_tools("implementations/tools/defs")
    native = filter_tools_for_provider_native(loaded.tools, "openai")
    # Ensure at least one tool opts into native (run_shell and read_file do)
    names = {t.name for t in native}
    assert "read_file" in names or "run_shell" in names

    schema = build_openai_tools_schema_from_yaml(native)
    assert isinstance(schema, list) and schema
    s_names = {s["function"]["name"] for s in schema}
    assert names.issuperset(s_names)


