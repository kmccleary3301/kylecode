"""
Enhanced base dialect class implementing design decisions from tool syntax analysis.

Provides foundation for modular tool calling with performance monitoring,
format selection, and extensible architecture.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Union
from enum import Enum
import time
import uuid


class ToolCallFormat(Enum):
    """Supported tool calling formats."""
    NATIVE_FUNCTION_CALLING = "native_function_calling"
    UNIFIED_DIFF = "unified_diff"
    ANTHROPIC_XML = "anthropic_xml"
    JSON_BLOCK = "json_block"
    YAML_COMMAND = "yaml_command"
    XML_PYTHON_HYBRID = "xml_python_hybrid"  # Legacy format
    AIDER_SEARCH_REPLACE = "aider_search_replace"


class TaskType(Enum):
    """Types of tasks for format optimization."""
    CODE_EDITING = "code_editing"
    FILE_MODIFICATION = "file_modification"
    API_OPERATIONS = "api_operations"
    SHELL_COMMANDS = "shell_commands"
    DATA_PROCESSING = "data_processing"
    CONFIGURATION = "configuration"
    GENERAL = "general"


@dataclass
class ToolCallExecutionMetrics:
    """Metrics for tool call execution."""
    format: str
    model_id: str
    task_type: str
    success: bool
    error_type: Optional[str] = None
    execution_time: float = 0.0
    token_count: int = 0
    timestamp: float = field(default_factory=time.time)
    call_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class ParsedToolCall:
    """Enhanced parsed tool call with metadata."""
    function: str
    arguments: Dict[str, Any]
    raw_content: Optional[str] = None
    call_id: Optional[str] = None
    format: Optional[str] = None
    confidence: float = 1.0  # Parsing confidence (0.0 to 1.0)
    
    # Compatibility with existing ToolCallParsed
    @property
    def ToolCallParsed(self):
        """Backward compatibility property."""
        from ..core.core import ToolCallParsed
        return ToolCallParsed(
            function=self.function,
            arguments=self.arguments
        )


@dataclass 
class EnhancedToolParameter:
    """Enhanced tool parameter with validation and metadata."""
    name: str
    type: str | None = None
    description: str | None = None
    default: Any | None = None
    required: bool = False
    validation_rules: Dict[str, Any] = field(default_factory=dict)
    examples: List[str] = field(default_factory=list)


@dataclass
class EnhancedToolDefinition:
    """Enhanced tool definition with format support and metadata."""
    name: str
    description: str
    parameters: List[EnhancedToolParameter] = field(default_factory=list)
    type_id: str = "python"
    blocking: bool = False
    
    # New enhanced fields
    supported_formats: Set[str] = field(default_factory=set)
    preferred_formats: List[str] = field(default_factory=list)
    use_cases: List[str] = field(default_factory=list)
    performance_data: Dict[str, float] = field(default_factory=dict)
    max_per_turn: Optional[int] = None
    dependencies: List[str] = field(default_factory=list)
    
    # Provider-specific settings
    provider_settings: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    
    def supports_format(self, format: Union[str, ToolCallFormat]) -> bool:
        """Check if tool supports a specific format."""
        format_str = format.value if isinstance(format, ToolCallFormat) else format
        return format_str in self.supported_formats or len(self.supported_formats) == 0
    
    def get_preferred_format(self, available_formats: List[str]) -> Optional[str]:
        """Get the most preferred format from available options."""
        for preferred in self.preferred_formats:
            if preferred in available_formats:
                return preferred
        return None


class EnhancedBaseDialect(ABC):
    """Enhanced base class for tool dialects with performance tracking."""
    
    def __init__(self):
        self.type_id: str = "unknown"
        self.format: ToolCallFormat = ToolCallFormat.XML_PYTHON_HYBRID
        self.provider_support: List[str] = ["*"]  # Which providers support this format
        self.performance_baseline: float = 0.0
        self.use_cases: List[TaskType] = [TaskType.GENERAL]
        
        # Performance tracking
        self._execution_history: List[ToolCallExecutionMetrics] = []
        self._success_rate_cache: Optional[float] = None
        self._cache_timestamp: float = 0
        
    @abstractmethod
    def get_system_prompt_section(self) -> str:
        """Get the system prompt section for this dialect."""
        pass
    
    @abstractmethod
    def parse_tool_calls(self, content: str) -> List[ParsedToolCall]:
        """Parse tool calls from content."""
        pass
    
    @abstractmethod
    def format_tools_for_prompt(self, tools: List[EnhancedToolDefinition]) -> str:
        """Format tools for inclusion in prompts."""
        pass
    
    def supports_provider(self, provider: str) -> bool:
        """Check if this dialect supports a specific provider."""
        return "*" in self.provider_support or provider in self.provider_support
    
    def supports_task_type(self, task_type: Union[str, TaskType]) -> bool:
        """Check if this dialect is suitable for a task type."""
        if isinstance(task_type, str):
            try:
                task_type = TaskType(task_type)
            except ValueError:
                return False
        return task_type in self.use_cases or TaskType.GENERAL in self.use_cases
    
    def get_success_rate(self, time_window_hours: float = 24.0) -> float:
        """Get recent success rate for this dialect."""
        current_time = time.time()
        
        # Use cache if recent
        if (current_time - self._cache_timestamp) < 300:  # 5 minutes
            return self._success_rate_cache or 0.0
        
        # Calculate from recent history
        cutoff_time = current_time - (time_window_hours * 3600)
        recent_executions = [
            m for m in self._execution_history 
            if m.timestamp >= cutoff_time
        ]
        
        if not recent_executions:
            success_rate = self.performance_baseline
        else:
            successes = sum(1 for m in recent_executions if m.success)
            success_rate = successes / len(recent_executions)
        
        # Update cache
        self._success_rate_cache = success_rate
        self._cache_timestamp = current_time
        
        return success_rate
    
    def record_execution(self, metrics: ToolCallExecutionMetrics) -> None:
        """Record execution metrics for performance tracking."""
        self._execution_history.append(metrics)
        
        # Keep only recent history (last 1000 executions)
        if len(self._execution_history) > 1000:
            self._execution_history = self._execution_history[-1000:]
        
        # Invalidate cache
        self._success_rate_cache = None
    
    def get_error_patterns(self) -> Dict[str, int]:
        """Get common error patterns for this dialect."""
        error_counts = {}
        for metric in self._execution_history:
            if not metric.success and metric.error_type:
                error_counts[metric.error_type] = error_counts.get(metric.error_type, 0) + 1
        return error_counts
    
    def estimate_token_overhead(self, num_tools: int) -> int:
        """Estimate token overhead for this dialect."""
        # Base implementation - subclasses should override with format-specific logic
        base_overhead = 50  # Base prompt overhead
        per_tool_overhead = 30  # Per tool description overhead
        return base_overhead + (num_tools * per_tool_overhead)
    
    def requires_user_message_injection(self) -> bool:
        """Check if this dialect requires tool calls in user messages."""
        # Most dialects need user message injection, native function calling doesn't
        return True
    
    def format_for_provider(self, provider: str, tools: List[EnhancedToolDefinition]) -> Dict[str, Any]:
        """Format tools for specific provider API."""
        # Default implementation returns prompt-based format
        return {
            "system_prompt_addition": self.format_tools_for_prompt(tools),
            "requires_user_injection": self.requires_user_message_injection()
        }
    
    # Backward compatibility methods
    def prompt_for_tools(self, tools: List) -> str:
        """Backward compatibility with old ToolDefinition format."""
        # Convert old format to new format
        enhanced_tools = []
        for tool in tools:
            enhanced_tool = EnhancedToolDefinition(
                name=tool.name,
                description=tool.description,
                type_id=getattr(tool, 'type_id', 'python'),
                blocking=getattr(tool, 'blocking', False)
            )
            enhanced_tools.append(enhanced_tool)
        
        return self.format_tools_for_prompt(enhanced_tools)
    
    def parse_calls(self, text: str, tools: List) -> List:
        """Backward compatibility with old parsing interface."""
        from ..core.core import ToolCallParsed
        
        parsed_calls = self.parse_tool_calls(text)
        
        # Convert to old format
        result = []
        for call in parsed_calls:
            result.append(ToolCallParsed(
                function=call.function,
                arguments=call.arguments
            ))
        
        return result


class ProviderDialectAdapter:
    """Adapter for provider-specific dialect modifications."""
    
    def __init__(self, base_dialect: EnhancedBaseDialect, provider: str):
        self.base_dialect = base_dialect
        self.provider = provider
        
    def adapt_for_provider(self, tools: List[EnhancedToolDefinition]) -> Dict[str, Any]:
        """Adapt dialect for specific provider."""
        
        if self.provider == "openai":
            return self._adapt_for_openai(tools)
        elif self.provider == "anthropic":
            return self._adapt_for_anthropic(tools)
        elif self.provider == "google":
            return self._adapt_for_google(tools)
        else:
            return self.base_dialect.format_for_provider(self.provider, tools)
    
    def _adapt_for_openai(self, tools: List[EnhancedToolDefinition]) -> Dict[str, Any]:
        """OpenAI-specific adaptations."""
        # Convert optional parameters to nullable for OpenAI compatibility
        adapted_format = self.base_dialect.format_for_provider("openai", tools)
        
        # Add OpenAI-specific metadata
        adapted_format["provider_metadata"] = {
            "supports_parallel_calls": True,
            "supports_structured_outputs": True,
            "max_tools_per_call": 128
        }
        
        return adapted_format
    
    def _adapt_for_anthropic(self, tools: List[EnhancedToolDefinition]) -> Dict[str, Any]:
        """Anthropic-specific adaptations."""
        adapted_format = self.base_dialect.format_for_provider("anthropic", tools)
        
        # Add Anthropic-specific metadata
        adapted_format["provider_metadata"] = {
            "supports_xml_format": True,
            "supports_prefill": True,
            "max_tools_per_call": 64
        }
        
        return adapted_format
    
    def _adapt_for_google(self, tools: List[EnhancedToolDefinition]) -> Dict[str, Any]:
        """Google/Gemini-specific adaptations."""
        adapted_format = self.base_dialect.format_for_provider("google", tools)
        
        # Add Google-specific metadata
        adapted_format["provider_metadata"] = {
            "requires_sanitized_schema": True,
            "supports_function_calling": True,
            "max_tools_per_call": 32
        }
        
        return adapted_format


# Utility functions for format detection and selection
def detect_task_type(content: str, context: Dict[str, Any] = None) -> TaskType:
    """Detect task type from content and context."""
    content_lower = content.lower()
    
    # Code editing indicators
    if any(keyword in content_lower for keyword in [
        "edit", "modify", "change", "update", "fix", "refactor", "code"
    ]):
        return TaskType.CODE_EDITING
    
    # Configuration indicators (check before generic file terms)
    if any(keyword in content_lower for keyword in [
        "config", "configuration", "settings", "configure", "setup", "install"
    ]):
        return TaskType.CONFIGURATION

    # File operation indicators  
    if any(keyword in content_lower for keyword in [
        "file", "create", "write", "read", "delete", "copy", "move"
    ]):
        return TaskType.FILE_MODIFICATION
    
    # Shell command indicators
    if any(keyword in content_lower for keyword in [
        "run", "execute", "command", "shell", "bash", "terminal"
    ]):
        return TaskType.SHELL_COMMANDS
    
    # API operation indicators
    if any(keyword in content_lower for keyword in [
        "api", "request", "fetch", "get", "post", "call", "endpoint"
    ]):
        return TaskType.API_OPERATIONS
    
    # Secondary configuration check for edge cases
    if any(keyword in content_lower for keyword in [
        "config", "configuration", "settings"
    ]):
        return TaskType.CONFIGURATION
    
    return TaskType.GENERAL


def calculate_format_preference_score(
    dialect: EnhancedBaseDialect,
    model_id: str,
    task_type: TaskType,
    context: Dict[str, Any] = None
) -> float:
    """Calculate preference score for a dialect based on multiple factors."""
    score = 0.0
    
    # Base success rate (0-1)
    success_rate = dialect.get_success_rate()
    score += success_rate * 0.5
    
    # Task type compatibility (0-0.3)
    if dialect.supports_task_type(task_type):
        score += 0.3
    
    # Provider compatibility (0-0.2)  
    provider = context.get("provider", "unknown") if context else "unknown"
    if dialect.supports_provider(provider):
        score += 0.2
    
    return min(score, 1.0)