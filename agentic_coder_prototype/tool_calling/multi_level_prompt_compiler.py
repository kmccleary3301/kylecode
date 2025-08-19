"""
Multi-Level Prompt Compiler for Advanced Tool Calling Modes

Supports granular prompt compilation with three levels:
1. System-level: Base system prompt compilation
2. Tool-level: Compiled tool availability and format information  
3. Per-turn: Dynamic per-turn tool availability

Modes supported:
- sys_compiled_per_turn_persistent--long-medium
- sys_compiled_per_turn_persistent--short-long
- sys_compiled_per_turn_persistent--medium-short
- sys_compiled_per_turn_temporary--long-medium
- sys_compiled_per_turn_temporary--short-long
- sys_compiled_per_turn_temporary--medium-short
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import List, Dict, Any, Set, Optional, Tuple
from enum import Enum
from dataclasses import dataclass

from .core import ToolDefinition
from .system_prompt_compiler import SystemPromptCompiler


class PromptLength(Enum):
    """Prompt length variations"""
    SHORT = "short"
    MEDIUM = "medium"  
    LONG = "long"


class PersistenceMode(Enum):
    """Per-turn persistence modes"""
    PERSISTENT = "persistent"
    TEMPORARY = "temporary"


@dataclass
class PromptCompilationConfig:
    """Configuration for multi-level prompt compilation"""
    # Base mode (always sys_compiled_per_turn for these variants)
    base_mode: str = "sys_compiled_per_turn"
    
    # Persistence mode for per-turn additions
    persistence: PersistenceMode = PersistenceMode.PERSISTENT
    
    # System prompt length (compiled and cached)
    system_length: PromptLength = PromptLength.LONG
    
    # Per-turn prompt length (tool availability info)
    per_turn_length: PromptLength = PromptLength.MEDIUM
    
    # Provider and model info
    provider_id: str = "openai"
    model_id: str = "gpt-4"
    
    def to_mode_string(self) -> str:
        """Convert config to mode string"""
        return f"{self.base_mode}_{self.persistence.value}--{self.system_length.value}-{self.per_turn_length.value}"
    
    @classmethod
    def from_mode_string(cls, mode_string: str) -> 'PromptCompilationConfig':
        """Parse mode string into config"""
        # Example: sys_compiled_per_turn_persistent--long-medium
        
        if not mode_string.startswith("sys_compiled_per_turn_"):
            raise ValueError(f"Unsupported mode: {mode_string}")
        
        # Extract persistence and lengths
        parts = mode_string.split("--")
        if len(parts) != 2:
            raise ValueError(f"Invalid mode format: {mode_string}")
            
        base_and_persistence = parts[0]  # sys_compiled_per_turn_persistent
        lengths = parts[1]  # long-medium
        
        # Extract persistence
        if base_and_persistence.endswith("_persistent"):
            persistence = PersistenceMode.PERSISTENT
        elif base_and_persistence.endswith("_temporary"):
            persistence = PersistenceMode.TEMPORARY
        else:
            raise ValueError(f"Unknown persistence mode in: {mode_string}")
        
        # Extract lengths
        length_parts = lengths.split("-")
        if len(length_parts) != 2:
            raise ValueError(f"Invalid length format: {lengths}")
            
        try:
            system_length = PromptLength(length_parts[0])
            per_turn_length = PromptLength(length_parts[1])
        except ValueError as e:
            raise ValueError(f"Invalid length values: {lengths}") from e
            
        return cls(
            persistence=persistence,
            system_length=system_length,
            per_turn_length=per_turn_length
        )


class MultiLevelPromptCompiler:
    """
    Advanced prompt compiler supporting multiple levels and granularity.
    
    Handles three-level compilation:
    1. System Level: Base behavior and general instructions
    2. Tool Level: Tool definitions and format specifications
    3. Per-Turn Level: Dynamic tool availability for each turn
    """
    
    def __init__(self, cache_dir: str = "implementations/tooling_sys_prompts_cached"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Legacy compiler for compatibility
        self.legacy_compiler = SystemPromptCompiler(str(cache_dir))
        
        # Length-specific templates
        self.system_templates = {
            PromptLength.SHORT: self._get_short_system_template(),
            PromptLength.MEDIUM: self._get_medium_system_template(),
            PromptLength.LONG: self._get_long_system_template()
        }
        
        self.per_turn_templates = {
            PromptLength.SHORT: self._get_short_per_turn_template(),
            PromptLength.MEDIUM: self._get_medium_per_turn_template(),
            PromptLength.LONG: self._get_long_per_turn_template()
        }
    
    def _get_short_system_template(self) -> str:
        """Short system template - minimal instructions"""
        return """# Tool Usage System

You have access to tools. Use them efficiently and report results concisely.

## Core Principles
- Direct execution without excessive explanation
- Use research-preferred formats (Aider SEARCH/REPLACE when available)
- Sequential tool execution (max 1 bash per turn)
- Complete tasks end-to-end

Tools available per turn will be specified in user messages."""
    
    def _get_medium_system_template(self) -> str:
        """Medium system template - balanced detail"""
        return """# Enhanced Tool Usage System

You have access to various tools for code manipulation and system interaction. Focus on efficient, reliable execution.

## Core Principles (Research-Based)
- **Direct execution**: Use tools immediately without excessive planning text
- **Format preferences**: Prioritize Aider SEARCH/REPLACE (2.3x success rate advantage)
- **Sequential execution**: Only ONE bash command per turn allowed
- **Proactive completion**: Mark tasks complete when finished
- **Concise communication**: Brief explanations, focus on actions

## Execution Pattern
1. Brief acknowledgment of task
2. Execute appropriate tools in sequence
3. Confirm completion with summary

## Tool Selection Priority
1. Aider SEARCH/REPLACE - Highest success rate for file modifications
2. OpenCode formats - Good for structured operations  
3. Unified diff - Use only as fallback

Available tools and formats will be specified in each user message."""
    
    def _get_long_system_template(self) -> str:
        """Long system template - comprehensive instructions"""
        return """# Advanced Tool Calling System

You are an AI assistant with access to a comprehensive toolkit for software development and system interaction. Your approach should be methodical, efficient, and grounded in research-validated best practices.

## Core Principles (Research-Validated)

### Execution Philosophy
- **Ownership mindset**: Treat each task as yours end-to-end. Plan, implement, validate, and iterate until completion.
- **Evidence-based decisions**: Never invent APIs, files, or results. Verify by reading files and running commands.
- **Minimal prose**: Prefer actions (tool calls) over lengthy explanations. Add brief notes only when they clarify decision points.
- **Surgical precision**: Keep edits focused and reversible. Preserve unrelated code and formatting.
- **Reproducible outcomes**: Make outputs deterministic. Pin versions and capture exact commands when relevant.

### Research-Based Tool Selection
Based on comprehensive analysis of production AI coding systems:

1. **PREFERRED: Aider SEARCH/REPLACE** - 2.3x higher success rate (59% vs 26%)
   - Use for all file modifications when possible
   - Exact text matching reduces errors significantly
   - Simple syntax minimizes model confusion

2. **GOOD: OpenCode structured formats** - Moderate success rate but versatile
   - Use for complex multi-file operations
   - Good for adding new files with proper structure
   - Clear block-based syntax

3. **FALLBACK: Unified diff patches** - Lower success rate for smaller models  
   - Use only when other formats unavailable
   - Higher complexity increases error probability
   - Better suited for larger, more capable models

## Execution Constraints (Critical)

### Tool Execution Rules
- **BASH CONSTRAINT**: Only ONE bash command per assistant turn allowed
- **SEQUENTIAL EXECUTION**: Tools execute in order; some tools block others
- **DEPENDENCY AWARENESS**: Consider tool execution dependencies
- **TIMEOUT MANAGEMENT**: Respect execution timeouts and cancellation

### Response Patterns
1. **Initial acknowledgment**: Briefly state what you will accomplish
2. **Sequential tool execution**: Execute tools in logical dependency order
3. **Incremental progress**: Provide brief updates for long-running operations
4. **Completion confirmation**: Summarize results and mark task complete

### Error Handling
- **Graceful degradation**: If preferred tools fail, fall back to alternatives
- **Clear error reporting**: Explain what went wrong and proposed solutions
- **Recovery strategies**: Attempt automatic recovery when possible
- **User guidance**: Provide actionable next steps for unrecoverable errors

## Quality Standards

### Code Standards
- Follow existing project conventions and style
- Maintain backward compatibility unless explicitly requested otherwise
- Include appropriate error handling and edge case management
- Write clear, self-documenting code with meaningful variable names

### Testing and Validation
- Run tests after making changes
- Validate that code compiles and executes without errors
- Check that changes don't break existing functionality
- Use example inputs to verify behavior

### Communication Standards
- Be concise but complete in explanations
- Focus on what changed and why, not implementation details
- Highlight important caveats or limitations
- Provide clear next steps when applicable

Available tools and formats for each turn will be specified in user messages. Tool availability may vary based on context, provider capabilities, and security constraints."""
    
    def _get_short_per_turn_template(self) -> str:
        """Short per-turn template - minimal tool list"""
        return """Available: {tool_list}"""
    
    def _get_medium_per_turn_template(self) -> str:
        """Medium per-turn template - structured tool availability"""
        return """<TOOLS_AVAILABLE>
{tool_details}
</TOOLS_AVAILABLE>"""
    
    def _get_long_per_turn_template(self) -> str:
        """Long per-turn template - comprehensive tool documentation"""
        return """<TOOLS_AVAILABLE>
The following tools are available for this turn:

{detailed_tool_docs}

Format Preferences (Research-Based):
- **PRIMARY**: Aider SEARCH/REPLACE (2.3x success rate)
- **SECONDARY**: OpenCode structured formats
- **FALLBACK**: Unified diff patches

Execution Constraints:
- Maximum 1 bash command per turn
- Sequential execution required
- 60-second timeout per tool

{format_examples}
</TOOLS_AVAILABLE>"""
    
    def _compute_compilation_hash(self, config: PromptCompilationConfig, tools: List[ToolDefinition], 
                                 dialects: List[str], primary_prompt: str = "") -> str:
        """Compute hash for compiled prompt caching"""
        hash_input = {
            "config": {
                "mode": config.to_mode_string(),
                "provider": config.provider_id,
                "model": config.model_id
            },
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": [p.name for p in (t.parameters or [])]
                }
                for t in sorted(tools, key=lambda x: x.name)
            ],
            "dialects": sorted(dialects),
            "primary_prompt": primary_prompt.strip()
        }
        
        hash_str = json.dumps(hash_input, sort_keys=True)
        return hashlib.sha256(hash_str.encode()).hexdigest()[:16]
    
    def compile_system_prompt(self, config: PromptCompilationConfig, tools: List[ToolDefinition],
                            dialects: List[str], primary_prompt_path: str = None) -> Tuple[str, str]:
        """
        Compile system-level prompt based on configuration.
        Returns (system_prompt, compilation_hash)
        """
        # Load primary prompt from file
        if not primary_prompt_path:
            primary_prompt_path = "implementations/system_prompts/default.md"
        
        primary_prompt = ""
        if Path(primary_prompt_path).exists():
            primary_prompt = Path(primary_prompt_path).read_text(encoding='utf-8')
        
        # Get system template based on length
        system_template = self.system_templates[config.system_length]
        
        # Compute hash for caching
        compilation_hash = self._compute_compilation_hash(config, tools, dialects, primary_prompt)
        
        # Check cache
        cache_file = self.cache_dir / f"sys_{config.to_mode_string()}_{compilation_hash}.md"
        if cache_file.exists():
            return cache_file.read_text(encoding='utf-8'), compilation_hash
        
        # Compile system prompt
        compiled_sections = []
        
        # Primary prompt first
        if primary_prompt.strip():
            compiled_sections.append(primary_prompt.strip())
            compiled_sections.append("")
        
        # System template
        compiled_sections.append(system_template)
        compiled_sections.append("")
        
        # For persistent modes, include tool format information in system prompt
        if config.persistence == PersistenceMode.PERSISTENT:
            tool_format_info = self._compile_tool_format_info(tools, dialects, config.system_length)
            if tool_format_info:
                compiled_sections.append(tool_format_info)
                compiled_sections.append("")
        
        system_prompt = "\n".join(compiled_sections)
        
        # Cache the compiled prompt
        cache_file.write_text(system_prompt, encoding='utf-8')
        
        return system_prompt, compilation_hash
    
    def compile_per_turn_prompt(self, config: PromptCompilationConfig, available_tools: List[str],
                              available_dialects: List[str], user_message: str) -> str:
        """
        Compile per-turn tool availability information.
        Returns user_message with appended tool info.
        """
        if config.per_turn_length == PromptLength.SHORT:
            # Minimal tool list
            tool_list = ", ".join(available_tools)
            tool_section = self.per_turn_templates[PromptLength.SHORT].format(tool_list=tool_list)
            
        elif config.per_turn_length == PromptLength.MEDIUM:
            # Structured tool list
            tool_details = []
            for tool in available_tools:
                tool_details.append(f"{tool} - [TYPE: python inside <TOOL_CALL> XML]")
            
            # Add format information
            format_info = []
            if "aider_diff" in available_dialects:
                format_info.append("*Aider SEARCH/REPLACE* - [TYPE: Aider SEARCH/REPLACE format]")
            if "opencode_patch" in available_dialects:
                format_info.append("*OpenCode Add File* - [TYPE: OpenCode Add File format]")
            if "unified_diff" in available_dialects:
                format_info.append("*Unified Diff Git-like* - [TYPE: Unified Diff Git-like format]")
            
            all_details = tool_details + format_info
            tool_section = self.per_turn_templates[PromptLength.MEDIUM].format(
                tool_details="\n".join(all_details)
            )
            
        else:  # LONG
            # Comprehensive documentation
            detailed_docs = []
            for tool in available_tools:
                detailed_docs.append(f"**{tool}**: Available for execution")
            
            # Format examples (updated per user request - no more `make j2`)
            format_examples = """
Example Usage:
<TOOL_CALL>
run_shell
command='echo "This is an example bash command"'
</TOOL_CALL>

<SEARCH_REPLACE>
file_path='example.py'
search_block='''def old_function():
    return "old"'''
replace_block='''def new_function():
    return "new"'''
</SEARCH_REPLACE>"""
            
            tool_section = self.per_turn_templates[PromptLength.LONG].format(
                detailed_tool_docs="\n".join(detailed_docs),
                format_examples=format_examples
            )
        
        # Combine user message with tool information
        return f"{user_message}\n\n{tool_section}"
    
    def _compile_tool_format_info(self, tools: List[ToolDefinition], dialects: List[str],
                                 system_length: PromptLength) -> str:
        """Compile tool format information for system prompt (persistent modes only)"""
        if system_length == PromptLength.SHORT:
            return ""  # No tool info in short system prompts
            
        sections = []
        
        if system_length == PromptLength.MEDIUM:
            sections.append("## Available Tool Formats")
            sections.append("- Python tools: Use <TOOL_CALL> XML format")
            if "aider_diff" in dialects:
                sections.append("- File editing: Use Aider SEARCH/REPLACE (preferred)")
            if "bash_block" in dialects:
                sections.append("- Shell commands: Use <BASH> XML format")
            
        else:  # LONG
            sections.append("## Tool Format Specifications")
            sections.append("")
            sections.append("### Python Tools")
            sections.append("Use <TOOL_CALL> XML format for Python tool execution:")
            sections.append("```xml")
            sections.append("<TOOL_CALL>")
            sections.append("tool_name")
            sections.append("parameter='value'")
            sections.append("</TOOL_CALL>")
            sections.append("```")
            sections.append("")
            
            if "aider_diff" in dialects:
                sections.append("### File Editing (PREFERRED)")
                sections.append("Use Aider SEARCH/REPLACE format for file modifications:")
                sections.append("```xml")
                sections.append("<SEARCH_REPLACE>")
                sections.append("file_path='path/to/file.py'")
                sections.append("search_block='''exact text to find'''")
                sections.append("replace_block='''replacement text'''")
                sections.append("</SEARCH_REPLACE>")
                sections.append("```")
                sections.append("")
            
            if "bash_block" in dialects:
                sections.append("### Shell Commands")
                sections.append("Use <BASH> XML format for shell execution:")
                sections.append("```xml")
                sections.append("<BASH>")
                sections.append('echo "This is an example bash command"')
                sections.append("</BASH>")
                sections.append("```")
        
        return "\n".join(sections)
    
    def get_supported_modes(self) -> List[str]:
        """Get list of all supported compilation modes"""
        modes = []
        for persistence in PersistenceMode:
            for sys_length in PromptLength:
                for turn_length in PromptLength:
                    config = PromptCompilationConfig(
                        persistence=persistence,
                        system_length=sys_length,
                        per_turn_length=turn_length
                    )
                    modes.append(config.to_mode_string())
        return sorted(modes)
    
    def validate_mode(self, mode_string: str) -> bool:
        """Validate if a mode string is supported"""
        try:
            PromptCompilationConfig.from_mode_string(mode_string)
            return True
        except ValueError:
            return False


# Global compiler instance
_multi_level_compiler = None

def get_multi_level_compiler() -> MultiLevelPromptCompiler:
    """Get global multi-level prompt compiler instance"""
    global _multi_level_compiler
    if _multi_level_compiler is None:
        _multi_level_compiler = MultiLevelPromptCompiler()
    return _multi_level_compiler