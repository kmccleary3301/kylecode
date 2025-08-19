"""
Provider-specific adapters for handling tool calling formats, ID matching, and result processing.

Handles the translation between our internal tool calling system and provider-specific formats.
"""
from typing import Dict, Any, List, Optional, Tuple, Union
from abc import ABC, abstractmethod
import json


class ProviderAdapter(ABC):
    """Base class for provider-specific tool calling adapters"""
    
    @abstractmethod
    def should_use_native_tool(self, tool_def: Dict[str, Any]) -> bool:
        """Determine if a tool should use native provider schema vs text-based"""
        pass
    
    @abstractmethod
    def translate_tool_to_native_schema(self, tool_def: Dict[str, Any]) -> Dict[str, Any]:
        """Convert internal tool definition to provider-native schema"""
        pass
    
    @abstractmethod
    def extract_tool_calls_from_response(self, response_message: Any) -> List[Dict[str, Any]]:
        """Extract tool calls from provider response, with call IDs"""
        pass
    
    @abstractmethod
    def create_tool_result_message(self, call_id: str, tool_name: str, result: Any) -> Dict[str, Any]:
        """Create provider-expected tool result message"""
        pass
    
    @abstractmethod
    def get_provider_id(self) -> str:
        """Return provider identifier"""
        pass


class OpenAIAdapter(ProviderAdapter):
    """OpenAI-specific adapter for tool calling"""
    
    def should_use_native_tool(self, tool_def: Dict[str, Any]) -> bool:
        """Check if tool should use OpenAI native function calling"""
        provider_routing = tool_def.get("provider_routing", {})
        openai_config = provider_routing.get("openai", {})
        return openai_config.get("native_primary", False)
    
    def translate_tool_to_native_schema(self, tool_def: Dict[str, Any]) -> Dict[str, Any]:
        """Convert to OpenAI function calling format"""
        parameters = {}
        required = []
        
        for param in tool_def.get("parameters", []):
            param_name = param.get("name")
            param_type = param.get("type", "string")
            param_desc = param.get("description", "")
            param_required = param.get("required", False)
            
            # Proper OpenAI schema format
            param_schema = {"type": param_type}
            if param_desc:
                param_schema["description"] = param_desc
            
            # Handle array types properly
            if param_type == "array":
                param_schema["items"] = {"type": "string"}  # Default to string items
            elif param_type == "object":
                param_schema["properties"] = {}
                param_schema["additionalProperties"] = True
            
            # Add default if present
            if param.get("default") is not None:
                param_schema["default"] = param["default"]
            
            parameters[param_name] = param_schema
            
            if param_required:
                required.append(param_name)
        
        return {
            "type": "function",
            "function": {
                "name": tool_def["name"],
                "description": tool_def.get("description", ""),
                "parameters": {
                    "type": "object",
                    "properties": parameters,
                    "required": required
                }
            }
        }
    
    def extract_tool_calls_from_response(self, response_message: Any) -> List[Dict[str, Any]]:
        """Extract OpenAI tool calls with IDs"""
        tool_calls = []
        
        if hasattr(response_message, 'tool_calls') and response_message.tool_calls:
            for tc in response_message.tool_calls:
                tool_calls.append({
                    "id": getattr(tc, "id", None),
                    "name": getattr(getattr(tc, "function", None), "name", None),
                    "arguments": getattr(getattr(tc, "function", None), "arguments", "{}"),
                    "type": "function"
                })
        
        return tool_calls
    
    def create_tool_result_message(self, call_id: str, tool_name: str, result: Any) -> Dict[str, Any]:
        """Create OpenAI-expected tool result message"""
        # Format result as string for OpenAI
        if isinstance(result, dict):
            content = json.dumps(result, indent=2)
        else:
            content = str(result)
        
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "name": tool_name,
            "content": content
        }
    
    def get_provider_id(self) -> str:
        return "openai"


class AnthropicAdapter(ProviderAdapter):
    """Anthropic-specific adapter for tool calling"""
    
    def should_use_native_tool(self, tool_def: Dict[str, Any]) -> bool:
        """Check if tool should use Anthropic native tool calling"""
        provider_routing = tool_def.get("provider_routing", {})
        anthropic_config = provider_routing.get("anthropic", {})
        return anthropic_config.get("native_primary", False)
    
    def translate_tool_to_native_schema(self, tool_def: Dict[str, Any]) -> Dict[str, Any]:
        """Convert to Anthropic tool format"""
        properties = {}
        required = []
        
        for param in tool_def.get("parameters", []):
            param_name = param.get("name")
            param_type = param.get("type", "string")
            param_desc = param.get("description", "")
            param_required = param.get("required", False)
            
            properties[param_name] = {
                "type": param_type,
                "description": param_desc
            }
            
            if param_required:
                required.append(param_name)
        
        return {
            "name": tool_def["name"],
            "description": tool_def.get("description", ""),
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required
            }
        }
    
    def extract_tool_calls_from_response(self, response_message: Any) -> List[Dict[str, Any]]:
        """Extract Anthropic tool calls"""
        # Anthropic uses different format - this would need to be implemented
        # based on their actual response format
        return []
    
    def create_tool_result_message(self, call_id: str, tool_name: str, result: Any) -> Dict[str, Any]:
        """Create Anthropic-expected tool result message"""
        # Anthropic uses different format for tool results
        if isinstance(result, dict):
            content = json.dumps(result, indent=2)
        else:
            content = str(result)
        
        return {
            "role": "user",  # Anthropic typically expects user role for tool results
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": content
                }
            ]
        }
    
    def get_provider_id(self) -> str:
        return "anthropic"


class ProviderAdapterManager:
    """Manages provider-specific adapters"""
    
    def __init__(self):
        self.adapters = {
            "openai": OpenAIAdapter(),
            "anthropic": AnthropicAdapter(),
            "openrouter": OpenAIAdapter(),  # OpenRouter uses OpenAI format
        }
    
    def get_adapter(self, provider_id: str) -> ProviderAdapter:
        """Get adapter for provider"""
        return self.adapters.get(provider_id, self.adapters["openai"])
    
    def filter_tools_for_provider(self, tools: List[Dict[str, Any]], provider_id: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Filter tools into provider-native vs text-based categories.
        
        Returns: (native_tools, text_based_tools)
        """
        adapter = self.get_adapter(provider_id)
        native_tools = []
        text_based_tools = []
        
        for tool in tools:
            if adapter.should_use_native_tool(tool):
                native_tools.append(tool)
            else:
                text_based_tools.append(tool)
        
        return native_tools, text_based_tools
    
    def translate_tools_to_native_schema(self, tools: List[Dict[str, Any]], provider_id: str) -> List[Dict[str, Any]]:
        """Convert tools to provider-native schema format"""
        adapter = self.get_adapter(provider_id)
        return [adapter.translate_tool_to_native_schema(tool) for tool in tools]


# Global instance
provider_adapter_manager = ProviderAdapterManager()