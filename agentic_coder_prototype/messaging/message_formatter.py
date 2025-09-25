"""
Message formatting for tool outputs and provider interactions
"""

import json
from pathlib import Path
from typing import Any, Dict, List


class MessageFormatter:
    """Handles formatting of tool outputs and messages for provider interactions"""
    
    def __init__(self, workspace: str):
        self.workspace = workspace
    
    def virtualize_path(self, path: str) -> str:
        """Virtualize path by removing workspace prefix"""
        if not path:
            return path

        try:
            ws = Path(self.workspace).resolve()
            candidate = Path(path)
            if candidate.is_absolute():
                try:
                    rel = candidate.resolve(strict=False).relative_to(ws)
                    return str(rel)
                except Exception:
                    pass
        except Exception:
            pass

        normalized = str(path).replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        ws_name = Path(self.workspace).name
        segments = [seg for seg in normalized.split("/") if seg and seg != "."]
        while segments and segments[0] == ws_name:
            segments.pop(0)
        rel_path = "/".join(segments)
        return rel_path or normalized or path
    
    def format_tree_structure(self, tree_data: List[Dict], prefix: str = "") -> str:
        """Format tree structure for display"""
        lines = []
        for i, item in enumerate(tree_data):
            is_last = i == len(tree_data) - 1
            current_prefix = "└── " if is_last else "├── "
            name = item["name"]
            if item["is_dir"]:
                name += "/"
            lines.append(prefix + current_prefix + name)
            
            # Add children if present
            if item.get("children"):
                child_prefix = prefix + ("    " if is_last else "│   ")
                child_lines = self.format_tree_structure(item["children"], child_prefix)
                lines.append(child_lines)
        
        return "\n".join(lines)
    
    def format_tool_output(self, tool_name: str, result: Dict, args: Dict) -> str:
        """Format tool output in the TOOL_OUTPUT format"""
        if tool_name == "list_dir":
            path = args.get("path", ".")
            depth = args.get("depth", 1)
            
            if result.get("error"):
                return f"<TOOL_OUTPUT tool=\"list_dir\" path=\"{path}\">\nError: {result['error']}\n</TOOL_OUTPUT>"
            
            if result.get("tree_format"):
                # Tree structure format
                tree_display = self.format_tree_structure(result.get("tree_structure", []))
                return f"<TOOL_OUTPUT tool=\"list_dir\" path=\"{path}\" depth={depth}>\n{tree_display}\n</TOOL_OUTPUT>"
            else:
                # Simple list format
                items = result.get("items", [])
                items_display = "\n".join(items)
                return f"<TOOL_OUTPUT tool=\"list_dir\" path=\"{path}\">\n{items_display}\n</TOOL_OUTPUT>"
        
        elif tool_name == "read_file":
            path = args.get("path", "")
            content = result.get("content", "")
            rel_path = self.virtualize_path(path)
            return f"<TOOL_OUTPUT tool=\"read_file\" file=\"{rel_path}\">\n{content}\n</TOOL_OUTPUT>"
        
        elif tool_name in ["create_file", "create_file_from_block", "apply_unified_patch", "apply_search_replace"]:
            # File operation results with LSP integration
            path = result.get("path", args.get("path", args.get("file_name", "unknown")))
            rel_path = self.virtualize_path(path) if isinstance(path, str) else "unknown"
            
            # Check for LSP diagnostics in result
            lsp_feedback = result.get("lsp_feedback", "")
            if lsp_feedback:
                if "0 linter errors" in lsp_feedback:
                    return f"<PATCH_RESULT file=\"{rel_path}\">\nFile successfully {'created' if 'create' in tool_name else 'patched'} with 0 linter errors.\n</PATCH_RESULT>"
                else:
                    return f"<PATCH_RESULT file=\"{rel_path}\">\nFile successfully {'created' if 'create' in tool_name else 'patched'}, but with linter errors:\n\n{lsp_feedback}\n</PATCH_RESULT>"
            else:
                # Fallback without LSP
                return f"<PATCH_RESULT file=\"{rel_path}\">\nFile successfully {'created' if 'create' in tool_name else 'patched'} with 0 linter errors.\n</PATCH_RESULT>"
        
        elif tool_name == "run_shell":
            stdout = result.get("stdout", "")
            stderr = result.get("stderr", "")
            exit_code = result.get("exit", 0)
            
            output_lines = []
            if stdout:
                output_lines.append(stdout.rstrip())
            if stderr:
                output_lines.append(f"stderr: {stderr.rstrip()}")
            if exit_code != 0:
                output_lines.append(f"exit code: {exit_code}")
            
            output_content = "\n".join(output_lines) if output_lines else "(no output)"
            return f"<BASH_RESULT>\n{output_content}\n</BASH_RESULT>"
        
        elif tool_name == "mark_task_complete":
            return f"<TOOL_OUTPUT tool=\"mark_task_complete\">\nTask marked as complete. Ending session.\n</TOOL_OUTPUT>"
        
        else:
            # Fallback to JSON for unknown tools
            try:
                return json.dumps(result)
            except Exception:
                return f"TOOL {tool_name} OUTPUT"

    def write_tool_result_file(self, root_dir: str, turn: int, seq: int, tool_name: str, result: Dict) -> str:
        """Persist a simple text artifact for a tool result and return relative path."""
        from pathlib import Path
        p = Path(root_dir) / f"artifacts/tool_results/turn_{turn}/{seq}_{tool_name}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Prefer formatted output
            content = self.format_tool_output(tool_name, result, {})
            p.write_text(content, encoding="utf-8")
            # Return path relative to run dir
            return str(p.relative_to(Path(root_dir)))
        except Exception:
            return ""
    
    def format_tool_error(self, tool_name: str, tool_result: Dict, tool_args: Dict) -> str:
        """Format tool error output"""
        if tool_name == "run_shell":
            stdout = tool_result.get("stdout", "")
            stderr = tool_result.get("stderr", "")
            exit_code = tool_result.get("exit", 0)
            error_msg = f"Command failed with exit code {exit_code}"
            if stderr:
                error_msg += f"\nstderr: {stderr}"
            return f"<BASH_RESULT>\n{stdout}\n{error_msg}\n</BASH_RESULT>"
        else:
            error_msg = tool_result.get("error", "Unknown error")
            return f"<TOOL_ERROR tool=\"{tool_name}\">\n{error_msg}\n</TOOL_ERROR>"
    
    def format_execution_results(
        self, 
        executed_results: List[tuple], 
        failed_at_index: int = -1,
        total_calls: int = 0
    ) -> List[str]:
        """Format execution results with failure handling"""
        chunks = []
        
        if failed_at_index >= 0:
            # Tool failed - show executed results and cancellation message
            for j, (tool_parsed, tool_result) in enumerate(executed_results):
                if j == failed_at_index:
                    # Show the failed tool with error formatting
                    chunks.append(self.format_tool_error(tool_parsed.function, tool_result, tool_parsed.arguments))
                else:
                    # Show successful tool normally
                    formatted_output = self.format_tool_output(tool_parsed.function, tool_result, tool_parsed.arguments)
                    chunks.append(formatted_output)
            
            # Add cancellation message  
            remaining_count = total_calls - failed_at_index - 1
            if remaining_count > 0:
                chunks.append(f"\nThe remaining {remaining_count} tool call(s) were cancelled due to the failure of the above call.")
        else:
            # All tools succeeded - show all results normally
            for tool_parsed, tool_result in executed_results:
                formatted_output = self.format_tool_output(tool_parsed.function, tool_result, tool_parsed.arguments)
                chunks.append(formatted_output)
        
        return chunks
    
    def create_synthetic_tool_calls(self, parsed_calls: List[Any]) -> List[Dict[str, Any]]:
        """Create synthetic tool_calls array for text-based parsing"""
        synthetic_tool_calls = []
        for i, p in enumerate(parsed_calls):
            synthetic_tool_calls.append({
                "id": f"text_call_{i}",
                "type": "function", 
                "function": {
                    "name": p.function,
                    "arguments": json.dumps(p.arguments) if isinstance(p.arguments, dict) else str(p.arguments)
                },
                "syntax_type": "custom-pythonic"  # Our <TOOL_CALL> format
            })
        return synthetic_tool_calls
    
    def create_enhanced_tool_calls(self, tool_calls_payload: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Create enhanced tool calls with syntax type metadata"""
        enhanced_tool_calls = []
        for tc in tool_calls_payload:
            enhanced_tc = tc.copy()
            enhanced_tc["syntax_type"] = "openai"  # Native OpenAI function calling
            enhanced_tool_calls.append(enhanced_tc)
        return enhanced_tool_calls
    
    def create_tool_result_entry(
        self, 
        tool_name: str, 
        tool_result: Dict[str, Any], 
        call_id: str = None,
        syntax_type: str = "custom-pythonic"
    ) -> Dict[str, Any]:
        """Create a tool result entry for the messages array"""
        formatted_output = self.format_tool_output(tool_name, tool_result, {})
        return {
            "role": "tool_result",
            "content": formatted_output,
            "tool_calls": [{
                "id": call_id or f"{syntax_type}_call_{tool_name}",
                "function": {"name": tool_name},
                "result": tool_result,
                "syntax_type": syntax_type
            }]
        }
