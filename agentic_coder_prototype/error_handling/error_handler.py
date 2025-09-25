"""
Error handling and recovery for agentic coding loops
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


class ErrorHandler:
    """Handles errors and provides recovery mechanisms for agentic coding loops"""
    
    def __init__(self, output_json_path: Optional[str] = None):
        self.output_json_path = output_json_path
    
    def handle_provider_error(
        self, 
        error: Exception, 
        messages: List[Dict[str, Any]], 
        transcript: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Handle provider API errors (OpenAI, etc.)"""
        import traceback

        details = getattr(error, "details", None)
        snippet = ""
        if isinstance(details, dict):
            snippet = details.get("body_snippet") or ""

        result = {
            "error": str(error),
            "error_type": error.__class__.__name__,
            "messages": messages,
            "transcript": transcript,
            "hint": "Verify provider credentials/quotas and model availability; rerun with a known-good model if needed.",
            "traceback": traceback.format_exc(),
        }

        if snippet:
            snippet_lower = snippet.lower()
            if "slow_down" in snippet_lower:
                result["hint"] = "Provider signaled rate limiting (slow_down). Allow a cool-off period before retrying."
            elif "rate limit" in snippet_lower:
                result["hint"] = "Provider indicated a rate limit or quota issue. Reduce request rate or wait before retrying."

        if details:
            result["details"] = details
        
        # Write error snapshot
        self.write_error_snapshot(result)
        return result
    
    def handle_execution_error(
        self, 
        error: Exception, 
        tool_name: str, 
        tool_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Handle tool execution errors"""
        return {
            "error": str(error), 
            "function": tool_name,
            "args": tool_args,
            "error_type": "execution_error"
        }
    
    def handle_validation_error(self, validation_result: Dict[str, Any]) -> str:
        """Handle validation errors and format for user feedback"""
        errors = validation_result.get("errors") or []
        error_message = "Tool execution validation failed:\n" + "\n".join(errors)
        return f"<VALIDATION_ERROR>\n{error_message}\n</VALIDATION_ERROR>"
    
    def handle_constraint_violation(self, error_message: str) -> str:
        """Handle constraint violations and format for user feedback"""
        return f"<CONSTRAINT_ERROR>\n{error_message}\nPlease call mark_task_complete() alone in a separate response after completing all file operations.\n</CONSTRAINT_ERROR>"
    
    def handle_empty_response(self, choice) -> Dict[str, Any]:
        """Handle empty responses from the provider"""
        return {
            "empty_response": {
                "finish_reason": getattr(choice, "finish_reason", None),
                "index": getattr(choice, "index", None),
            }
        }
    
    def write_error_snapshot(self, error_result: Dict[str, Any]):
        """Write error snapshot to JSON file"""
        if not self.output_json_path:
            return
        
        try:
            Path(self.output_json_path).write_text(json.dumps(error_result, indent=2))
        except Exception:
            pass
    
    def should_break_on_empty_response(self) -> bool:
        """Determine if the loop should break on empty responses"""
        return True  # Break to avoid looping endlessly on empty responses
    
    def format_cancellation_message(self, remaining_count: int) -> str:
        """Format cancellation message for failed tool executions"""
        if remaining_count > 0:
            return f"\nThe remaining {remaining_count} tool call(s) were cancelled due to the failure of the above call."
        return ""
