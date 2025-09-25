"""
YAML command dialect implementation.

Implements a human-readable YAML-based tool calling format, estimated at
~65% success rate with excellent readability and debugging capabilities.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:
    yaml = None

from .enhanced_base_dialect import (
    EnhancedBaseDialect,
    ToolCallFormat,
    TaskType,
    EnhancedToolDefinition,
    ParsedToolCall
)


class YAMLCommandDialect(EnhancedBaseDialect):
    """YAML command dialect for human-readable tool calling."""
    
    def __init__(self):
        super().__init__()
        self.type_id = "yaml_command"
        self.format = ToolCallFormat.YAML_COMMAND
        self.provider_support = ["*"]  # Universal format
        self.performance_baseline = 0.65  # Estimated based on readability
        self.use_cases = [
            TaskType.CONFIGURATION,
            TaskType.SHELL_COMMANDS,
            TaskType.GENERAL,
            TaskType.FILE_MODIFICATION
        ]
    
    def get_system_prompt_section(self) -> str:
        """Get system prompt section for YAML command format."""
        return """When you need to use tools, format your commands as YAML blocks like this:

```yaml
tools:
  - name: tool_name
    args:
      param1: value1
      param2: value2
  - name: another_tool
    args:
      param3: value3
```

Use clear, readable YAML structure for tool invocations."""
    
    def parse_tool_calls(self, content: str) -> List[ParsedToolCall]:
        """Parse tool calls from YAML command format."""
        parsed_calls = []
        
        # Look for YAML code blocks
        yaml_pattern = r'```yaml\s*\n(.*?)\n```'
        yaml_blocks = re.findall(yaml_pattern, content, re.DOTALL | re.IGNORECASE)
        
        for yaml_content in yaml_blocks:
            try:
                calls = self._parse_yaml_content(yaml_content)
                for call in calls:
                    call.raw_content = f"```yaml\n{yaml_content}\n```"
                    call.format = self.format.value
                    call.confidence = 0.85
                parsed_calls.extend(calls)
            except Exception:
                continue
        
        # Also look for yml blocks
        yml_pattern = r'```yml\s*\n(.*?)\n```'
        yml_blocks = re.findall(yml_pattern, content, re.DOTALL | re.IGNORECASE)
        
        for yml_content in yml_blocks:
            try:
                calls = self._parse_yaml_content(yml_content)
                for call in calls:
                    call.raw_content = f"```yml\n{yml_content}\n```"
                    call.format = self.format.value
                    call.confidence = 0.85
                parsed_calls.extend(calls)
            except Exception:
                continue
        
        return parsed_calls
    
    def _parse_yaml_content(self, yaml_content: str) -> List[ParsedToolCall]:
        """Parse YAML content into tool calls."""
        if yaml is None:
            # Fallback to manual parsing if PyYAML not available
            return self._parse_yaml_manual(yaml_content)
        
        try:
            data = yaml.safe_load(yaml_content)
        except yaml.YAMLError:
            # Try manual parsing
            return self._parse_yaml_manual(yaml_content)
        
        parsed_calls = []
        
        # Handle different YAML structures
        if isinstance(data, dict):
            if "tools" in data:
                # Standard format: tools: [...]
                tools_data = data["tools"]
                if isinstance(tools_data, list):
                    for tool_data in tools_data:
                        call = self._extract_call_from_tool_data(tool_data)
                        if call:
                            parsed_calls.append(call)
            
            elif "commands" in data:
                # Alternative format: commands: [...]
                commands_data = data["commands"]
                if isinstance(commands_data, list):
                    for cmd_data in commands_data:
                        call = self._extract_call_from_tool_data(cmd_data)
                        if call:
                            parsed_calls.append(call)
            
            elif "name" in data:
                # Single tool format
                call = self._extract_call_from_tool_data(data)
                if call:
                    parsed_calls.append(call)
        
        elif isinstance(data, list):
            # Direct list of tools
            for tool_data in data:
                call = self._extract_call_from_tool_data(tool_data)
                if call:
                    parsed_calls.append(call)
        
        return parsed_calls
    
    def _extract_call_from_tool_data(self, tool_data: Dict[str, Any]) -> Optional[ParsedToolCall]:
        """Extract ParsedToolCall from tool data dictionary."""
        if not isinstance(tool_data, dict):
            return None
        
        # Get function name
        function_name = None
        for name_key in ["name", "tool", "function", "cmd", "command"]:
            if name_key in tool_data:
                function_name = tool_data[name_key]
                break
        
        if not function_name:
            return None
        
        # Get arguments
        arguments = {}
        for args_key in ["args", "arguments", "params", "parameters", "inputs"]:
            if args_key in tool_data:
                args_data = tool_data[args_key]
                if isinstance(args_data, dict):
                    arguments = args_data
                break
        
        return ParsedToolCall(
            function=str(function_name),
            arguments=arguments
        )
    
    def _parse_yaml_manual(self, yaml_content: str) -> List[ParsedToolCall]:
        """Manual YAML parsing fallback."""
        parsed_calls = []
        lines = yaml_content.strip().split('\n')
        
        current_tool = None
        current_args = {}
        in_args = False
        
        for line in lines:
            line = line.rstrip()
            
            # Skip empty lines and comments
            if not line or line.strip().startswith('#'):
                continue
            
            # Detect tool definition
            if line.startswith('- name:') or line.startswith('  - name:'):
                # Save previous tool
                if current_tool:
                    parsed_calls.append(ParsedToolCall(
                        function=current_tool,
                        arguments=current_args
                    ))
                
                # Start new tool
                current_tool = line.split(':', 1)[1].strip()
                current_args = {}
                in_args = False
            
            elif 'name:' in line and not line.startswith(' '):
                # Single tool format
                if current_tool:
                    parsed_calls.append(ParsedToolCall(
                        function=current_tool,
                        arguments=current_args
                    ))
                
                current_tool = line.split(':', 1)[1].strip()
                current_args = {}
                in_args = False
            
            # Detect args section
            elif 'args:' in line or 'arguments:' in line:
                in_args = True
            
            # Parse argument lines
            elif in_args and ':' in line:
                # Simple key: value parsing
                indent_level = len(line) - len(line.lstrip())
                if indent_level > 0:  # Indented line
                    key, value = line.split(':', 1)
                    key = key.strip()
                    value = value.strip()
                    
                    # Convert value type
                    current_args[key] = self._convert_yaml_value(value)
        
        # Save final tool
        if current_tool:
            parsed_calls.append(ParsedToolCall(
                function=current_tool,
                arguments=current_args
            ))
        
        return parsed_calls
    
    def _convert_yaml_value(self, value: str) -> Any:
        """Convert YAML string value to appropriate Python type."""
        value = value.strip()
        
        # Remove quotes
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            return value[1:-1]
        
        # Boolean values
        if value.lower() in ('true', 'yes', 'on'):
            return True
        elif value.lower() in ('false', 'no', 'off'):
            return False
        
        # Numeric values
        if value.isdigit():
            return int(value)
        
        try:
            if '.' in value:
                return float(value)
        except ValueError:
            pass
        
        return value
    
    def format_tools_for_prompt(self, tools: List[EnhancedToolDefinition]) -> str:
        """Format tools for YAML command prompt."""
        if not tools:
            return ""
        
        sections = [
            "# Available Tools",
            "",
            "You have access to the following tools. Use them with the YAML format shown:",
            ""
        ]
        
        for tool in tools:
            if not tool.supports_format(self.format):
                continue
            
            sections.append(f"## {tool.name}")
            sections.append(f"**Description**: {tool.description}")
            
            if tool.parameters:
                sections.append("**Parameters**:")
                for param in tool.parameters:
                    required_text = " (required)" if param.required else " (optional)"
                    param_type = param.type or "string"
                    sections.append(f"- `{param.name}` ({param_type}){required_text}: {param.description or ''}")
            
            sections.append("")
            sections.append("**Usage Example**:")
            
            # Generate YAML example
            sections.append("```yaml")
            sections.append("tools:")
            sections.append(f"  - name: {tool.name}")
            
            if tool.parameters:
                sections.append("    args:")
                for param in tool.parameters[:3]:  # Show first 3 params
                    example_value = self._get_example_value(param)
                    sections.append(f"      {param.name}: {example_value}")
            
            sections.append("```")
            sections.append("")
        
        return "\n".join(sections)
    
    def _get_example_value(self, param) -> str:
        """Get example value for parameter."""
        if param.examples:
            return param.examples[0]
        
        if param.default is not None:
            return str(param.default)
        
        param_type = (param.type or "string").lower()
        
        type_examples = {
            "string": "example_value",
            "str": "example_value",
            "integer": "42",
            "int": "42", 
            "number": "3.14",
            "float": "3.14",
            "boolean": "true",
            "bool": "true",
            "array": "[item1, item2]",
            "list": "[item1, item2]",
            "object": "{key: value}",
            "dict": "{key: value}"
        }
        
        return type_examples.get(param_type, "example_value")
    
    def create_tool_call_yaml(self, function_name: str, arguments: Dict[str, Any]) -> str:
        """Create YAML tool call from function name and arguments."""
        if yaml is not None:
            data = {
                "tools": [
                    {
                        "name": function_name,
                        "args": arguments
                    }
                ]
            }
            return yaml.dump(data, default_flow_style=False, indent=2)
        else:
            # Manual YAML generation
            lines = ["tools:"]
            lines.append(f"  - name: {function_name}")
            if arguments:
                lines.append("    args:")
                for key, value in arguments.items():
                    lines.append(f"      {key}: {value}")
            return "\n".join(lines)
    
    def create_multi_tool_yaml(self, tool_calls: List[tuple[str, Dict[str, Any]]]) -> str:
        """Create YAML for multiple tool calls."""
        if yaml is not None:
            tools_data = []
            for function_name, arguments in tool_calls:
                tools_data.append({
                    "name": function_name,
                    "args": arguments
                })
            
            data = {"tools": tools_data}
            return yaml.dump(data, default_flow_style=False, indent=2)
        else:
            # Manual YAML generation
            lines = ["tools:"]
            for function_name, arguments in tool_calls:
                lines.append(f"  - name: {function_name}")
                if arguments:
                    lines.append("    args:")
                    for key, value in arguments.items():
                        lines.append(f"      {key}: {value}")
            return "\n".join(lines)
    
    def estimate_token_overhead(self, num_tools: int) -> int:
        """Estimate token overhead for YAML command format."""
        base_overhead = 100  # YAML format explanation
        per_tool_overhead = 35  # YAML is readable but slightly verbose
        return base_overhead + (num_tools * per_tool_overhead)
    
    def supports_comments(self) -> bool:
        """Check if format supports inline comments."""
        return True  # YAML supports comments with #
    
    def format_for_provider(self, provider: str, tools: List[EnhancedToolDefinition]) -> Dict[str, Any]:
        """Format tools for provider API."""
        # YAML command format is provider-agnostic
        return {
            "system_prompt_addition": self.format_tools_for_prompt(tools),
            "requires_user_injection": True,
            "supports_parallel_calls": True,
            "provider_metadata": {
                "format_type": "yaml_command",
                "human_readable": True,
                "supports_comments": True,
                "structured": True
            }
        }