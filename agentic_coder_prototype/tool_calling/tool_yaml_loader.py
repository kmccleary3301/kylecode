"""
YAML Tool Loader

Loads individual YAML tool definitions from `implementations/tools/defs/` and
validates them into EnhancedToolDefinition instances with provider routing and
manipulation metadata.

YAML schema (per file):

id: run_shell
name: run_shell
description: Execute a shell command
type_id: python
manipulations:
  - shell.exec
syntax_formats_supported: [native_function_calling, json_block, yaml_command]
preferred_formats: [yaml_command]
parameters:
  - name: command
    type: string
    description: The shell command to run
    required: true
  - name: timeout
    type: integer
    required: false
    default: 30
execution:
  blocking: true
  max_per_turn: 1
provider_routing:
  openai:
    native_primary: true
    fallback_formats: [json_block, yaml_command]

"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import yaml

from .enhanced_base_dialect import (
    EnhancedToolDefinition,
    EnhancedToolParameter,
)


@dataclass
class LoadedTools:
    tools: List[EnhancedToolDefinition]
    manipulations_by_id: Dict[str, List[str]]


REQUIRED_TOP_LEVEL_FIELDS = ["id", "name", "description", "parameters"]


def _validate_tool_dict(tool_data: Dict[str, Any], file_path: str) -> None:
    for field in REQUIRED_TOP_LEVEL_FIELDS:
        if field not in tool_data:
            raise ValueError(f"Tool YAML missing required field '{field}' in {file_path}")


def _to_enhanced_params(params: List[Dict[str, Any]]) -> List[EnhancedToolParameter]:
    result: List[EnhancedToolParameter] = []
    for p in params or []:
        result.append(
            EnhancedToolParameter(
                name=p.get("name"),
                type=p.get("type"),
                description=p.get("description"),
                default=p.get("default"),
                required=bool(p.get("required", False)),
                validation_rules=p.get("validation", {}),
                examples=p.get("examples", []),
            )
        )
    return result


def _to_enhanced_tool(tool_data: Dict[str, Any]) -> EnhancedToolDefinition:
    execution = tool_data.get("execution", {})
    provider_routing = tool_data.get("provider_routing", {})

    supported_formats = set(tool_data.get("syntax_formats_supported", []))
    preferred_formats = list(tool_data.get("preferred_formats", []))

    return EnhancedToolDefinition(
        name=tool_data.get("name"),
        description=tool_data.get("description"),
        parameters=_to_enhanced_params(tool_data.get("parameters", [])),
        type_id=tool_data.get("type_id", "python"),
        blocking=bool(execution.get("blocking", False)),
        supported_formats=supported_formats,
        preferred_formats=preferred_formats,
        max_per_turn=execution.get("max_per_turn"),
        provider_settings=provider_routing or {},
    )


def load_yaml_tools(defs_dir: str = "implementations/tools/defs") -> LoadedTools:
    """
    Load all YAML tool definitions from the given directory.

    Returns:
        LoadedTools: list of EnhancedToolDefinition and manipulations mapping.
    """
    if not os.path.isdir(defs_dir):
        raise FileNotFoundError(f"Tools definitions directory not found: {defs_dir}")

    tools: List[EnhancedToolDefinition] = []
    manipulations_by_id: Dict[str, List[str]] = {}

    for fname in sorted(os.listdir(defs_dir)):
        if not fname.endswith(".yaml") and not fname.endswith(".yml"):
            continue
        path = os.path.join(defs_dir, fname)
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        _validate_tool_dict(data, path)
        tool = _to_enhanced_tool(data)
        tools.append(tool)

        tool_id = data.get("id") or data.get("name")
        manipulations_by_id[tool_id] = list(data.get("manipulations", []))

    return LoadedTools(tools=tools, manipulations_by_id=manipulations_by_id)


