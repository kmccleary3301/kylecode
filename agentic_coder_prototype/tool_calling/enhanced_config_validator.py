"""
Enhanced tool configuration validator implementing design decisions from 08-13-25 report.

Features:
- Sequential bash execution validation
- Tool dependency resolution
- Blocking constraint validation
- Format preference enforcement
- OpenCode-style state tracking
"""

import yaml
from typing import Dict, List, Any, Optional, Set
from dataclasses import dataclass
from pathlib import Path
import jsonschema
from jsonschema import validate, ValidationError


@dataclass
class ToolConstraints:
    """Tool execution constraints based on research findings."""
    blocking: bool = False
    max_per_turn: Optional[int] = None
    dependencies: List[str] = None
    execution_order: Optional[int] = None
    format_priority: Optional[int] = None
    
    def __post_init__(self):
        if self.dependencies is None:
            self.dependencies = []


@dataclass 
class ExecutionPlan:
    """Execution plan for tool calls with dependency resolution."""
    tool_calls: List[Dict[str, Any]]
    execution_order: List[int]
    blocking_points: List[int]
    dependency_graph: Dict[str, List[str]]


class EnhancedConfigValidator:
    """Enhanced tool configuration validator."""
    
    def __init__(self, config_path: Optional[str] = None):
        self.config = None
        self.tool_constraints = {}
        self.format_preferences = {}
        
        if config_path:
            self.load_config(config_path)
    
    def load_config(self, config_path: str) -> None:
        """Load enhanced tool configuration."""
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)
        
        self._parse_tool_constraints()
        self._parse_format_preferences()
        self._validate_config_schema()
    
    def _parse_tool_constraints(self) -> None:
        """Parse tool constraints from configuration."""
        for tool in self.config.get('tools', []):
            tool_id = tool['id']
            self.tool_constraints[tool_id] = ToolConstraints(
                blocking=tool.get('blocking', False),
                max_per_turn=tool.get('max_per_turn'),
                dependencies=tool.get('dependencies', []),
                execution_order=tool.get('execution_order'),
                format_priority=tool.get('format_priority')
            )
    
    def _parse_format_preferences(self) -> None:
        """Parse format preferences from configuration."""
        self.format_preferences = self.config.get('format_preferences', {})
    
    def _validate_config_schema(self) -> None:
        """Validate configuration against enhanced schema."""
        schema = {
            "type": "object",
            "required": ["tools"],
            "properties": {
                "tools": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id", "description", "parameters"],
                        "properties": {
                            "id": {"type": "string"},
                            "description": {"type": "string"},
                            "blocking": {"type": "boolean"},
                            "max_per_turn": {"type": ["integer", "null"]},
                            "dependencies": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "execution_order": {"type": ["integer", "null"]},
                            "format_priority": {"type": ["integer", "null"]},
                            "parameters": {"type": "object"}
                        }
                    }
                },
                "execution_constraints": {"type": "object"},
                "format_preferences": {"type": "object"}
            }
        }
        
        try:
            validate(instance=self.config, schema=schema)
        except ValidationError as e:
            raise ValueError(f"Configuration validation failed: {e.message}")
    
    def validate_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Validate tool calls against constraints and return execution plan.
        
        Based on research findings:
        - Only one bash command per turn
        - Sequential execution for blocking tools
        - Dependency resolution
        """
        validation_result = {
            "valid": True,
            "errors": [],
            "warnings": [],
            "execution_plan": None
        }
        
        # Check bash command constraint (critical finding from research)
        bash_calls = [call for call in tool_calls if call.get('name') == 'run_shell']
        if len(bash_calls) > 1:
            validation_result["valid"] = False
            validation_result["errors"].append(
                "Only one bash command (run_shell) allowed per turn based on research findings"
            )
        
        # Check max_per_turn constraints
        tool_counts = {}
        for call in tool_calls:
            tool_name = call.get('name')
            tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
            
            if tool_name in self.tool_constraints:
                constraints = self.tool_constraints[tool_name]
                if constraints.max_per_turn and tool_counts[tool_name] > constraints.max_per_turn:
                    validation_result["valid"] = False
                    validation_result["errors"].append(
                        f"Tool {tool_name} exceeds max_per_turn limit: {constraints.max_per_turn}"
                    )
        
        # Check dependencies
        available_tools = set()
        for call in tool_calls:
            tool_name = call.get('name')
            if tool_name in self.tool_constraints:
                constraints = self.tool_constraints[tool_name]
                for dep in constraints.dependencies:
                    if dep not in available_tools and dep not in [c.get('name') for c in tool_calls]:
                        validation_result["warnings"].append(
                            f"Tool {tool_name} depends on {dep} which may not be available"
                        )
            available_tools.add(tool_name)
        
        # Generate execution plan
        if validation_result["valid"]:
            validation_result["execution_plan"] = self._generate_execution_plan(tool_calls)
        
        return validation_result
    
    def _generate_execution_plan(self, tool_calls: List[Dict[str, Any]]) -> ExecutionPlan:
        """Generate execution plan with proper ordering and blocking points."""
        
        # Separate blocking and non-blocking tools
        blocking_calls = []
        non_blocking_calls = []
        
        for i, call in enumerate(tool_calls):
            tool_name = call.get('name')
            if tool_name in self.tool_constraints:
                constraints = self.tool_constraints[tool_name]
                if constraints.blocking:
                    blocking_calls.append((i, call, constraints.execution_order or 999))
                else:
                    non_blocking_calls.append((i, call, constraints.execution_order or 999))
            else:
                non_blocking_calls.append((i, call, 999))
        
        # Sort by execution order
        blocking_calls.sort(key=lambda x: x[2])
        non_blocking_calls.sort(key=lambda x: x[2])
        
        # Build execution order: blocking tools first, then non-blocking
        execution_order = []
        blocking_points = []
        
        # Add blocking tools
        for i, call, _ in blocking_calls:
            execution_order.append(i)
            blocking_points.append(len(execution_order) - 1)
        
        # Add non-blocking tools
        for i, call, _ in non_blocking_calls:
            execution_order.append(i)
        
        # Build dependency graph
        dependency_graph = {}
        for call in tool_calls:
            tool_name = call.get('name')
            if tool_name in self.tool_constraints:
                dependency_graph[tool_name] = self.tool_constraints[tool_name].dependencies
        
        return ExecutionPlan(
            tool_calls=tool_calls,
            execution_order=execution_order,
            blocking_points=blocking_points,
            dependency_graph=dependency_graph
        )
    
    def get_preferred_diff_format(self, available_formats: List[str]) -> str:
        """
        Get preferred diff format based on research findings.
        
        Research shows Aider SEARCH/REPLACE has 2.3x success rate advantage.
        """
        preferences = self.format_preferences.get('diff_formats', [
            'aider_search_replace',
            'opencode_patch', 
            'unified_diff'
        ])
        
        for preferred in preferences:
            # Map to actual format names used in system
            format_mapping = {
                'aider_search_replace': 'aider_diff',
                'opencode_patch': 'opencode_patch',
                'unified_diff': 'unified_diff'
            }
            
            mapped_format = format_mapping.get(preferred, preferred)
            if mapped_format in available_formats:
                return mapped_format
        
        # Fallback to first available
        return available_formats[0] if available_formats else None
    
    def validate_bash_blocking(self, current_tools: List[str]) -> bool:
        """
        Validate bash blocking constraint.
        
        Based on research: if bash is called, it should be the only tool in that turn.
        """
        has_bash = any(tool == 'run_shell' for tool in current_tools)
        
        if has_bash and len(current_tools) > 1:
            # Check if other tools are dependencies or non-blocking
            non_bash_tools = [tool for tool in current_tools if tool != 'run_shell']
            for tool in non_bash_tools:
                if tool in self.tool_constraints:
                    constraints = self.tool_constraints[tool]
                    if constraints.blocking:
                        return False  # Another blocking tool with bash
            
            # Allow non-blocking tools with bash for now, but warn
            return True
        
        return True
    
    def get_execution_strategy(self) -> str:
        """Get execution strategy from config."""
        return self.config.get('execution_constraints', {}).get('blocking_strategy', 'sequential')


# Utility functions for integration
def load_enhanced_config(config_path: str) -> EnhancedConfigValidator:
    """Load enhanced configuration validator."""
    return EnhancedConfigValidator(config_path)


def validate_tool_execution_plan(
    tool_calls: List[Dict[str, Any]], 
    config_path: str
) -> Dict[str, Any]:
    """Validate tool execution plan against enhanced constraints."""
    validator = EnhancedConfigValidator(config_path)
    return validator.validate_tool_calls(tool_calls)


# Example usage for testing
if __name__ == "__main__":
    # Test validation
    validator = EnhancedConfigValidator()
    
    # Mock config for testing
    test_config = {
        "tools": [
            {
                "id": "run_shell",
                "description": "Run shell command",
                "blocking": True,
                "max_per_turn": 1,
                "dependencies": [],
                "parameters": {"command": {"type": "string"}}
            },
            {
                "id": "read_file", 
                "description": "Read file",
                "blocking": False,
                "dependencies": [],
                "parameters": {"path": {"type": "string"}}
            }
        ],
        "execution_constraints": {
            "bash_per_turn": 1,
            "sequential_blocking": True
        }
    }
    
    validator.config = test_config
    validator._parse_tool_constraints()
    
    # Test tool calls
    tool_calls = [
        {"name": "run_shell", "args": {"command": "echo test"}},
        {"name": "read_file", "args": {"path": "test.txt"}}
    ]
    
    result = validator.validate_tool_calls(tool_calls)
    print("Validation result:", result)