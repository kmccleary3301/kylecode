"""
Unified diff dialect implementation.

Implements the high-performance unified diff format discovered by Aider research,
achieving 61% success rate compared to 20% for SEARCH/REPLACE patterns.
"""

from __future__ import annotations

import re
import difflib
from typing import Any, Dict, List, Optional
import tempfile
import os

from ..enhanced_base_dialect import (
    EnhancedBaseDialect,
    ToolCallFormat,
    TaskType,
    EnhancedToolDefinition,
    ParsedToolCall
)


class UnifiedDiffDialect(EnhancedBaseDialect):
    """Unified diff dialect for high-precision code editing."""
    
    def __init__(self):
        super().__init__()
        self.type_id = "unified_diff"
        self.format = ToolCallFormat.UNIFIED_DIFF
        self.provider_support = ["*"]  # Universal format
        self.performance_baseline = 0.61  # Aider research data
        self.use_cases = [
            TaskType.CODE_EDITING,
            TaskType.FILE_MODIFICATION,
            TaskType.GENERAL
        ]
    
    def get_system_prompt_section(self) -> str:
        """Get system prompt section for unified diff format."""
        return """When editing files, use unified diff format for precise changes. Create a diff block like this:

```diff
--- a/path/to/file.py
+++ b/path/to/file.py
@@ -10,7 +10,7 @@ def function_name():
     existing_line_1
     existing_line_2
-    old_line_to_remove
+    new_line_to_add
     existing_line_3
     existing_line_4
```

Use this format for file modifications, code edits, and content changes. The unified diff format provides precise context and minimizes ambiguity."""
    
    def parse_tool_calls(self, content: str) -> List[ParsedToolCall]:
        """Parse unified diff blocks from content."""
        parsed_calls = []
        
        # Pattern to match unified diff blocks
        diff_pattern = r'```diff\s*\n(.*?)\n```'
        diff_matches = re.findall(diff_pattern, content, re.DOTALL | re.MULTILINE)
        
        for diff_content in diff_matches:
            try:
                parsed_diff = self._parse_unified_diff(diff_content)
                if parsed_diff:
                    parsed_calls.append(ParsedToolCall(
                        function="apply_diff",
                        arguments=parsed_diff,
                        raw_content=f"```diff\n{diff_content}\n```",
                        format=self.format.value,
                        confidence=0.9  # High confidence for structured diff
                    ))
            except Exception:
                continue
        
        # Also look for inline diff patterns without code blocks
        # Remove fenced blocks to avoid double-parsing
        content_without_blocks = re.sub(r'```diff\s*\n.*?\n```', '', content, flags=re.DOTALL | re.MULTILINE)
        inline_diff_pattern = r'(?:^|\n)(---\s+[^\n]+\n\+\+\+\s+[^\n]+\n@@[^@]*@@.*?)(?=\n(?:---|$)|\Z)'
        inline_matches = re.findall(inline_diff_pattern, content_without_blocks, re.DOTALL | re.MULTILINE)
        
        for diff_content in inline_matches:
            try:
                parsed_diff = self._parse_unified_diff(diff_content)
                if parsed_diff:
                    parsed_calls.append(ParsedToolCall(
                        function="apply_diff",
                        arguments=parsed_diff,
                        raw_content=diff_content,
                        format=self.format.value,
                        confidence=0.85  # Slightly lower confidence for inline
                    ))
            except Exception:
                continue
        
        return parsed_calls
    
    def _parse_unified_diff(self, diff_content: str) -> Optional[Dict[str, Any]]:
        """Parse a unified diff into structured data."""
        lines = diff_content.strip().split('\n')
        
        if len(lines) < 3:
            return None
        
        # Extract file paths
        file_a_line = next((line for line in lines if line.startswith('---')), None)
        file_b_line = next((line for line in lines if line.startswith('+++')), None)
        
        if not file_a_line or not file_b_line:
            return None
        
        # Extract file paths
        file_a_match = re.search(r'---\s+(?:a/)?(.+?)(?:\s+\d{4}-\d{2}-\d{2}|\s*$)', file_a_line)
        file_b_match = re.search(r'\+\+\+\s+(?:b/)?(.+?)(?:\s+\d{4}-\d{2}-\d{2}|\s*$)', file_b_line)
        
        if not file_a_match or not file_b_match:
            return None
        
        file_path = file_b_match.group(1)
        
        # Parse hunks
        hunks = []
        current_hunk = None
        
        for line in lines:
            # Hunk header
            hunk_match = re.match(r'@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(?:\s+(.*))?', line)
            if hunk_match:
                if current_hunk:
                    hunks.append(current_hunk)
                
                old_start = int(hunk_match.group(1))
                old_count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
                new_start = int(hunk_match.group(3))
                new_count = int(hunk_match.group(4)) if hunk_match.group(4) else 1
                context = hunk_match.group(5) or ""
                
                current_hunk = {
                    "old_start": old_start,
                    "old_count": old_count,
                    "new_start": new_start,
                    "new_count": new_count,
                    "context": context,
                    "changes": []
                }
                continue
            
            # Hunk content
            if current_hunk is not None:
                if line.startswith(' '):
                    # Context line
                    current_hunk["changes"].append({
                        "type": "context",
                        "content": line[1:]
                    })
                elif line.startswith('-'):
                    # Deleted line
                    current_hunk["changes"].append({
                        "type": "delete",
                        "content": line[1:]
                    })
                elif line.startswith('+'):
                    # Added line
                    current_hunk["changes"].append({
                        "type": "add", 
                        "content": line[1:]
                    })
        
        # Add final hunk
        if current_hunk:
            hunks.append(current_hunk)
        
        if not hunks:
            return None
        
        return {
            "file_path": file_path,
            "hunks": hunks,
            "operation": "edit_file"
        }
    
    def format_tools_for_prompt(self, tools: List[EnhancedToolDefinition]) -> str:
        """Format tools for unified diff prompt."""
        sections = [
            "# Unified Diff Tool Format",
            "",
            "Use unified diff format for file modifications. Example:",
            "",
            "```diff",
            "--- a/example.py",
            "+++ b/example.py", 
            "@@ -5,10 +5,10 @@ def example_function():",
            "     # Context line",
            "     old_value = 1",
            "-    # Remove this line",
            "+    # Add this new line",
            "     # More context",
            "```",
            "",
            "Benefits of unified diff format:",
            "- Precise line-by-line changes",
            "- Clear context around modifications",
            "- Reduced ambiguity compared to search/replace",
            "- Industry standard for version control",
            ""
        ]
        
        # Add available file editing tools
        file_tools = [tool for tool in tools if "file" in tool.name.lower() or "edit" in tool.name.lower()]
        
        if file_tools:
            sections.append("Available file operations:")
            for tool in file_tools:
                sections.append(f"- {tool.name}: {tool.description}")
        
        return "\n".join(sections)
    
    def create_diff_from_changes(self, 
                                file_path: str,
                                original_content: str,
                                new_content: str) -> str:
        """Create a unified diff from original and new content."""
        
        original_lines = original_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        
        diff = difflib.unified_diff(
            original_lines,
            new_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            lineterm=""
        )
        
        return "".join(diff)
    
    def apply_diff_to_content(self, original_content: str, diff_data: Dict[str, Any]) -> str:
        """Apply parsed diff to original content."""
        lines = original_content.splitlines()
        result_lines = []
        
        current_line_idx = 0
        
        for hunk in diff_data["hunks"]:
            # Add lines before this hunk
            while current_line_idx < hunk["old_start"] - 1:
                result_lines.append(lines[current_line_idx])
                current_line_idx += 1
            
            # Process hunk changes
            old_line_idx = current_line_idx
            
            for change in hunk["changes"]:
                if change["type"] == "context":
                    # Context line - should match original
                    if old_line_idx < len(lines):
                        result_lines.append(lines[old_line_idx])
                        old_line_idx += 1
                elif change["type"] == "delete":
                    # Skip the deleted line in original
                    old_line_idx += 1
                elif change["type"] == "add":
                    # Add the new line
                    result_lines.append(change["content"])
            
            current_line_idx = old_line_idx
        
        # Add remaining lines
        while current_line_idx < len(lines):
            result_lines.append(lines[current_line_idx])
            current_line_idx += 1
        
        return "\n".join(result_lines)
    
    def validate_diff(self, diff_data: Dict[str, Any], original_content: str) -> bool:
        """Validate that a diff can be applied to original content."""
        try:
            # Attempt to apply diff
            result = self.apply_diff_to_content(original_content, diff_data)
            return True
        except Exception:
            return False
    
    def estimate_token_overhead(self, num_tools: int) -> int:
        """Estimate token overhead for unified diff format."""
        base_overhead = 150  # Format explanation and examples
        per_tool_overhead = 20  # Minimal per-tool overhead
        return base_overhead + (num_tools * per_tool_overhead)
    
    def create_patch_file(self, diff_data: Dict[str, Any]) -> str:
        """Create a patch file from diff data."""
        lines = []
        
        lines.append(f"--- a/{diff_data['file_path']}")
        lines.append(f"+++ b/{diff_data['file_path']}")
        
        for hunk in diff_data["hunks"]:
            # Hunk header
            old_range = f"{hunk['old_start']},{hunk['old_count']}" if hunk['old_count'] != 1 else str(hunk['old_start'])
            new_range = f"{hunk['new_start']},{hunk['new_count']}" if hunk['new_count'] != 1 else str(hunk['new_start'])
            
            header = f"@@ -{old_range} +{new_range} @@"
            if hunk.get("context"):
                header += f" {hunk['context']}"
            lines.append(header)
            
            # Hunk content
            for change in hunk["changes"]:
                prefix = {
                    "context": " ",
                    "delete": "-",
                    "add": "+"
                }[change["type"]]
                lines.append(f"{prefix}{change['content']}")
        
        return "\n".join(lines)
    
    def supports_preview_mode(self) -> bool:
        """Check if this dialect supports preview before applying changes."""
        return True
    
    def generate_preview(self, diff_data: Dict[str, Any], original_content: str) -> Dict[str, Any]:
        """Generate a preview of changes without applying them."""
        try:
            new_content = self.apply_diff_to_content(original_content, diff_data)
            
            # Calculate change statistics
            original_lines = original_content.splitlines()
            new_lines = new_content.splitlines()
            
            stats = {
                "lines_added": 0,
                "lines_removed": 0,
                "lines_modified": 0
            }
            
            for hunk in diff_data["hunks"]:
                for change in hunk["changes"]:
                    if change["type"] == "add":
                        stats["lines_added"] += 1
                    elif change["type"] == "delete":
                        stats["lines_removed"] += 1
            
            return {
                "success": True,
                "new_content": new_content,
                "statistics": stats,
                "file_path": diff_data["file_path"]
            }
        
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "file_path": diff_data["file_path"]
            }