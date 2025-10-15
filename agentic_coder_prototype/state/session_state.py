"""
Session state management for agentic coding loops
"""

from typing import Any, Dict, List, Optional
from pathlib import Path
import json

from dataclasses import asdict

from ..reasoning_trace_store import ReasoningTraceStore
from ..provider_ir import (
    IRConversation,
    IRDeltaEvent,
    IRFinish,
    convert_legacy_messages,
)


class SessionState:
    """Manages session state for agentic coding loops"""
    
    def __init__(self, workspace: str, image: str, config: Optional[Dict[str, Any]] = None):
        self.workspace = workspace
        self.image = image
        self.config = config or {}
        self.messages: List[Dict[str, Any]] = []
        self.provider_messages: List[Dict[str, Any]] = []
        self.transcript: List[Dict[str, Any]] = []
        self.last_tool_prompt_mode = "unknown"
        self.completion_config = {}
        self.current_native_tools = []
        self.current_text_based_tools = []
        self.completion_summary: Dict[str, Any] = {}
        self.provider_metadata: Dict[str, Any] = {}
        self.reasoning_traces = ReasoningTraceStore()
        self.ir_events: List[IRDeltaEvent] = []
        self.ir_finish: Optional[IRFinish] = None
    
    def add_message(self, message: Dict[str, Any], to_provider: bool = True):
        """Add a message to the session state"""
        self.messages.append(message)
        if to_provider:
            self.provider_messages.append(message.copy())
    
    def add_transcript_entry(self, entry: Dict[str, Any]):
        """Add an entry to the transcript"""
        self.transcript.append(entry)

    # --- Provider metadata ----------------------------------------------------
    def set_provider_metadata(self, key: str, value: Any) -> None:
        self.provider_metadata[key] = value

    def get_provider_metadata(self, key: str, default: Any = None) -> Any:
        return self.provider_metadata.get(key, default)

    def clear_provider_metadata(self) -> None:
        self.provider_metadata.clear()

    # --- IR helpers ---------------------------------------------------------
    def add_ir_event(self, event: IRDeltaEvent) -> None:
        self.ir_events.append(event)

    def set_ir_finish(self, finish: IRFinish) -> None:
        self.ir_finish = finish

    def build_conversation_ir(self, conversation_id: str, ir_version: str = "1") -> IRConversation:
        messages_ir = convert_legacy_messages(self.messages)
        return IRConversation(
            id=conversation_id,
            ir_version=ir_version,
            messages=messages_ir,
            events=list(self.ir_events),
            finish=self.ir_finish,
        )

    def get_debug_info(self) -> Dict[str, Any]:
        """Get enhanced debugging information about tool usage and provider configuration"""
        provider_cfg = self.config.get("provider_tools", {})
        return {
            "provider_tools_config": provider_cfg,
            "tool_prompt_mode": self.last_tool_prompt_mode,
            "native_tools_enabled": bool(provider_cfg.get("use_native", False)),
            "tools_suppressed": bool(provider_cfg.get("suppress_prompts", False)),
            "yaml_tools_count": len(getattr(self, 'yaml_tools', [])),
            "enhanced_executor_enabled": bool(getattr(self, 'enhanced_executor', None)),
            "provider_metadata_keys": sorted(self.provider_metadata.keys()),
            "reasoning_trace_counts": {
                "encrypted": len(self.reasoning_traces.get_encrypted_traces()),
                "summaries": len(self.reasoning_traces.get_summaries()),
            },
        }
    
    def analyze_tool_usage(self) -> Dict[str, Any]:
        """Analyze messages for tool calling patterns"""
        return {
            "total_messages": len(self.messages),
            "assistant_messages": len([m for m in self.messages if m.get("role") == "assistant"]),
            "messages_with_native_tool_calls": len([m for m in self.messages if m.get("role") == "assistant" and m.get("tool_calls")]),
            "messages_with_text_tool_calls": len([m for m in self.messages if m.get("role") == "assistant" and m.get("content") and "<TOOL_CALL>" in str(m.get("content", ""))]),
            "tool_role_messages": len([m for m in self.messages if m.get("role") == "tool"]),
        }
    
    def create_snapshot(self, model: str, diff: Dict[str, Any] = None) -> Dict[str, Any]:
        """Create a session snapshot for debugging and persistence"""
        snapshot = {
            "workspace": self.workspace,
            "image": self.image,
            "model": model,
            "messages": self.messages,
            "transcript": self.transcript,
            "diff": diff or {"ok": False, "data": {"diff": ""}},
            "debug_info": self.get_debug_info(),
            "tool_analysis": self.analyze_tool_usage(),
            "completion_summary": self.completion_summary,
            "provider_metadata": self.provider_metadata,
            "reasoning_trace_counts": {
                "encrypted": len(self.reasoning_traces.get_encrypted_traces()),
                "summaries": len(self.reasoning_traces.get_summaries()),
            },
            "ir_version": "1",
            "conversation_ir": asdict(self.build_conversation_ir(conversation_id="snapshot")),
        }
        return snapshot
    
    def write_snapshot(self, output_path: Optional[str], model: str, diff: Dict[str, Any] = None):
        """Write session snapshot to JSON file"""
        if not output_path:
            return
        
        try:
            snapshot = self.create_snapshot(model, diff)
            outp = Path(output_path)
            outp.parent.mkdir(parents=True, exist_ok=True)
            outp.write_text(json.dumps(snapshot, indent=2))
        except Exception:
            pass
