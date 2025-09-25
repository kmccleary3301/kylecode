"""
State-of-the-art completion detection for agentic coding loops
"""

from typing import Any, Dict, List, Optional


class CompletionDetector:
    """Advanced completion detection using multiple methods"""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None, completion_sentinel: Optional[str] = None):
        # Initialize SoTA completion detection system
        default_config = {
            "primary_method": "hybrid",  # tool_based | text_based | provider_based | hybrid
            "enable_text_sentinels": True,
            "enable_provider_signals": True,
            "enable_natural_finish": True,
            "confidence_threshold": 0.6,
            "text_sentinels": [
                completion_sentinel,
                "TASK COMPLETE",
                "ALL TESTS PASSED", 
                "IMPLEMENTATION COMPLETE",
                "DONE"
            ] if completion_sentinel else [
                "TASK COMPLETE",
                "ALL TESTS PASSED",
                "IMPLEMENTATION COMPLETE", 
                "DONE"
            ],
            "success_keywords": [
                "success", "complete", "finished", "done", "passed", "working", "ready"
            ],
            "failure_keywords": [
                "failed", "error", "broken", "timeout", "cancelled"
            ]
        }
        
        # Merge with provided config
        yaml_config = config or {}
        if yaml_config.get("text_sentinels"):
            # Merge text sentinels from YAML with defaults
            yaml_sentinels = yaml_config["text_sentinels"]
            default_sentinels = default_config["text_sentinels"]
            yaml_config["text_sentinels"] = list(set(yaml_sentinels + default_sentinels))
        
        self.config = {**default_config, **yaml_config}
    
    def is_mark_task_complete_available(self, agent_config: Dict[str, Any]) -> bool:
        """Check if mark_task_complete tool is available"""
        if not agent_config:
            return True
        tools_config = agent_config.get("tools", {})
        return tools_config.get("mark_task_complete", True)
    
    def detect_completion(
        self, 
        msg_content: str, 
        choice_finish_reason: str, 
        tool_results: List[Dict[str, Any]] = None,
        agent_config: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        State-of-the-art completion detection using multiple methods.
        Returns: {"completed": bool, "method": str, "confidence": float, "reason": str}
        """
        completion_result = {"completed": False, "method": "none", "confidence": 0.0, "reason": ""}
        
        # Check if mark_task_complete tool is available
        mark_task_complete_available = self.is_mark_task_complete_available(agent_config or {})
        
        # 1. TOOL-BASED COMPLETION (Primary - Highest Confidence)
        if tool_results:
            for result in tool_results:
                if isinstance(result, dict) and result.get("action") == "complete":
                    return {
                        "completed": True, 
                        "method": "tool_based", 
                        "confidence": 1.0, 
                        "reason": "mark_task_complete() called"
                    }
        
        # 2. TEXT SENTINEL DETECTION (High Confidence - Enhanced when no tool completion)
        if self.config.get("enable_text_sentinels", True) and msg_content:
            for sentinel in self.config.get("text_sentinels", []):
                if sentinel and sentinel in msg_content:
                    # Boost confidence when tool completion is not available
                    confidence = 0.95 if not mark_task_complete_available else 0.9
                    return {
                        "completed": True,
                        "method": "text_sentinel", 
                        "confidence": confidence,
                        "reason": f"Sentinel detected: '{sentinel}'" + (
                            " (boosted confidence - no tool completion)" if not mark_task_complete_available else ""
                        )
                    }
        
        # 3. PROVIDER FINISH REASON ANALYSIS (Medium Confidence)
        if self.config.get("enable_provider_signals", True) and choice_finish_reason:
            if choice_finish_reason in ["stop", "end_turn", "length"]:
                # Analyze content for completion indicators
                success_score = 0
                failure_score = 0
                
                if msg_content:
                    content_lower = msg_content.lower()
                    for keyword in self.config.get("success_keywords", []):
                        if keyword in content_lower:
                            success_score += 1
                    for keyword in self.config.get("failure_keywords", []):
                        if keyword in content_lower:
                            failure_score += 1
                
                # Natural completion if strong success indicators (enhanced scoring when no tool completion)
                required_score = 1 if not mark_task_complete_available else 2
                if success_score >= required_score and failure_score == 0:
                    confidence = 0.8 if not mark_task_complete_available else 0.7
                    return {
                        "completed": True,
                        "method": "provider_natural",
                        "confidence": confidence,
                        "reason": f"Provider finish + success indicators (score: {success_score})" + (
                            " (enhanced for no-tool mode)" if not mark_task_complete_available else ""
                        )
                    }
        
        # 4. NATURAL FINISH DETECTION (Lower Confidence)
        if self.config.get("enable_natural_finish", True) and msg_content:
            # Look for natural completion patterns
            content_lower = msg_content.lower()
            natural_patterns = [
                "task is complete", "implementation is done", "everything is working",
                "all requirements met", "successfully implemented", "ready for use"
            ]
            
            for pattern in natural_patterns:
                if pattern in content_lower:
                    # Enhanced confidence when no tool completion available
                    confidence = 0.75 if not mark_task_complete_available else 0.6
                    return {
                        "completed": True,
                        "method": "natural_language",
                        "confidence": confidence,
                        "reason": f"Natural completion pattern: '{pattern}'" + (
                            " (enhanced for no-tool mode)" if not mark_task_complete_available else ""
                        )
                    }
        
        return completion_result
    
    def meets_threshold(self, completion_analysis: Dict[str, Any]) -> bool:
        """Check if completion analysis meets confidence threshold"""
        confidence_threshold = self.config.get("confidence_threshold", 0.6)
        return (completion_analysis["method"] == "tool_based" or 
               completion_analysis["confidence"] >= confidence_threshold)