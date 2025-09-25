"""Runtime abstractions and concrete runtimes for model providers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple, Type

try:  # pragma: no cover - import guard exercised in runtime
    from openai import OpenAI
except ImportError:  # pragma: no cover - covered via error path tests
    OpenAI = None  # type: ignore[assignment]

try:  # pragma: no cover - import guard exercised in runtime
    from anthropic import Anthropic
except ImportError:  # pragma: no cover - covered via error path tests
    Anthropic = None  # type: ignore[assignment]

from .provider_routing import ProviderDescriptor


# ---------------------------------------------------------------------------
# Normalised result objects shared by runtimes
# ---------------------------------------------------------------------------


@dataclass
class ProviderToolCall:
    """Normalized representation of a provider tool call."""

    id: Optional[str]
    name: Optional[str]
    arguments: str
    type: str = "function"
    raw: Any = None


@dataclass
class ProviderMessage:
    """Normalized assistant message returned from a provider."""

    role: str
    content: Optional[str]
    tool_calls: List[ProviderToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None
    index: Optional[int] = None
    raw_message: Any = None
    raw_choice: Any = None
    reasoning: Optional[Any] = None
    annotations: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderResult:
    """Result object returned from a provider runtime invocation."""

    messages: List[ProviderMessage]
    raw_response: Any
    usage: Optional[Dict[str, Any]] = None
    encrypted_reasoning: Optional[List[Any]] = None
    reasoning_summaries: Optional[List[str]] = None
    model: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderRuntimeContext:
    """Context object passed to provider runtimes."""

    session_state: Any
    agent_config: Dict[str, Any]
    stream: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


class ProviderRuntimeError(RuntimeError):
    """Raised when a provider runtime encounters a fatal error."""

    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.details: Dict[str, Any] = details or {}


# ---------------------------------------------------------------------------
# Base runtime + registry
# ---------------------------------------------------------------------------


class ProviderRuntime:
    """Interface for provider runtimes."""

    def __init__(self, descriptor: ProviderDescriptor) -> None:
        self.descriptor = descriptor

    def create_client(
        self,
        api_key: str,
        *,
        base_url: Optional[str] = None,
        default_headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        raise NotImplementedError

    def invoke(
        self,
        *,
        client: Any,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        stream: bool,
        context: ProviderRuntimeContext,
    ) -> ProviderResult:
        raise NotImplementedError


class ProviderRuntimeRegistry:
    """Registry that maps runtime identifiers to implementation classes."""

    def __init__(self) -> None:
        self._runtime_classes: Dict[str, Type[ProviderRuntime]] = {}

    def register_runtime(self, runtime_id: str, runtime_cls: Type[ProviderRuntime]) -> None:
        if not issubclass(runtime_cls, ProviderRuntime):  # defensive guard
            raise TypeError(f"Runtime {runtime_cls!r} must inherit ProviderRuntime")
        self._runtime_classes[runtime_id] = runtime_cls

    def get_runtime_class(self, runtime_id: str) -> Optional[Type[ProviderRuntime]]:
        return self._runtime_classes.get(runtime_id)

    def create_runtime(self, descriptor: ProviderDescriptor) -> ProviderRuntime:
        runtime_cls = self.get_runtime_class(descriptor.runtime_id)
        if runtime_cls is None:
            raise ProviderRuntimeError(
                f"Unknown provider runtime '{descriptor.runtime_id}' for provider '{descriptor.provider_id}'"
            )
        return runtime_cls(descriptor)


provider_registry = ProviderRuntimeRegistry()


# ---------------------------------------------------------------------------
# Shared helper mixins
# ---------------------------------------------------------------------------


class OpenAIBaseRuntime(ProviderRuntime):
    """Utility helpers for OpenAI-compatible runtimes."""

    def _require_openai(self) -> None:
        if OpenAI is None:
            raise ProviderRuntimeError("openai package not installed")

    def _decode_snippet(self, content: Any) -> str:
        if content is None:
            return ""
        try:
            if isinstance(content, (bytes, bytearray)):
                return bytes(content).decode("utf-8", "ignore")[:400].strip()
            if hasattr(content, "decode"):
                return content.decode("utf-8", "ignore")[:400].strip()
            text = str(content)
            return text[:400].strip()
        except Exception:
            return ""

    def _call_with_raw_response(self, collection: Any, *, error_context: str, **kwargs):
        raw_callable = getattr(collection, "with_raw_response", None)
        if raw_callable is None:
            return collection.create(**kwargs)
        raw = raw_callable.create(**kwargs)
        try:
            return raw.parse()
        except json.JSONDecodeError as exc:
            snippet = self._decode_snippet(getattr(raw, "content", None))
            status_code = getattr(raw, "status_code", None)
            details = {
                "body_snippet": snippet,
                "status_code": status_code,
                "context": error_context,
            }
            raise ProviderRuntimeError(
                "Failed to decode provider response (non-JSON payload). This often indicates an HTML error page from the provider.",
                details=details,
            ) from exc

    # --- data conversion helpers -----------------------------------------
    def _convert_messages_to_chat(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        converted: List[Dict[str, Any]] = []
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content")
            if isinstance(content, list):
                converted.append({"role": role, "content": content})
            else:
                converted.append({"role": role, "content": content})
        return converted

    def _convert_tools_to_openai(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
        if not tools:
            return None
        # Tools already follow OpenAI schema in upstream config; clone defensively
        return [dict(tool) for tool in tools]

    # --- response parsing helpers ----------------------------------------
    def _get_attr(self, obj: Any, name: str, default: Any = None) -> Any:
        if hasattr(obj, name):
            return getattr(obj, name)
        if isinstance(obj, dict):
            return obj.get(name, default)
        return default

    def _message_content_to_text(self, content: Any) -> Optional[str]:
        if content is None:
            return None
        if isinstance(content, str):
            return content
        parts: List[str] = []
        try:
            for block in content:
                block_type = self._get_attr(block, "type")
                if block_type in {"output_text", "text"}:
                    text_val = self._get_attr(block, "text", "")
                    if text_val:
                        parts.append(str(text_val))
        except Exception:
            return None
        return "".join(parts) if parts else None

    def _extract_tool_calls(self, message: Any) -> List[ProviderToolCall]:
        results: List[ProviderToolCall] = []
        raw_tool_calls = self._get_attr(message, "tool_calls") or []
        for raw in raw_tool_calls:
            fn = self._get_attr(raw, "function", {}) or {}
            arguments = self._get_attr(fn, "arguments", "{}")
            if not isinstance(arguments, str):
                try:
                    arguments = json.dumps(arguments)
                except Exception:
                    arguments = "{}"
            results.append(
                ProviderToolCall(
                    id=self._get_attr(raw, "id"),
                    name=self._get_attr(fn, "name"),
                    arguments=arguments,
                    type=self._get_attr(raw, "type", "function"),
                    raw=raw,
                )
            )
        return results

    def _extract_usage(self, response: Any) -> Optional[Dict[str, Any]]:
        usage_obj = getattr(response, "usage", None)
        if usage_obj is None:
            return None
        try:
            return dict(usage_obj)
        except Exception:
            try:
                return usage_obj.model_dump()  # type: ignore[attr-defined]
            except Exception:
                return None


# ---------------------------------------------------------------------------
# OpenAI chat runtime (Chat Completions)
# ---------------------------------------------------------------------------


class OpenAIChatRuntime(OpenAIBaseRuntime):
    """Runtime for OpenAI Chat Completions API."""

    def create_client(
        self,
        api_key: str,
        *,
        base_url: Optional[str] = None,
        default_headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        self._require_openai()
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        if default_headers:
            kwargs["default_headers"] = default_headers
        return OpenAI(**kwargs)

    def _stream_chat_completion(
        self,
        client: Any,
        *,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
    ) -> Any:
        try:
            stream_ctx = client.chat.completions.stream(
                model=model,
                messages=messages,
                tools=tools,
            )
        except AttributeError as exc:
            raise ProviderRuntimeError("OpenAI SDK does not expose chat streaming helpers") from exc
        except Exception as exc:  # pragma: no cover - guarded via ProviderRuntimeError tests
            raise ProviderRuntimeError(str(exc)) from exc

        try:
            with stream_ctx as stream:
                for _ in stream:
                    pass
                return stream.get_final_response()
        except Exception as exc:  # pragma: no cover - guarded in tests via ProviderRuntimeError
            raise ProviderRuntimeError(str(exc)) from exc

    def invoke(
        self,
        *,
        client: Any,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        stream: bool,
        context: ProviderRuntimeContext,
    ) -> ProviderResult:
        request_messages = self._convert_messages_to_chat(messages)
        request_tools = self._convert_tools_to_openai(tools)

        response: Any = None
        if stream:
            response = self._stream_chat_completion(
                client,
                model=model,
                messages=request_messages,
                tools=request_tools,
            )

        if response is None:
            try:
                response = self._call_with_raw_response(
                    client.chat.completions,
                    error_context="chat.completions.create",
                    model=model,
                    messages=request_messages,
                    tools=request_tools,
                    stream=False,
                )
            except ProviderRuntimeError:
                raise
            except Exception as exc:  # pragma: no cover - exercised in integration
                raise ProviderRuntimeError(str(exc)) from exc

        normalized_messages: List[ProviderMessage] = []
        for idx, choice in enumerate(getattr(response, "choices", []) or []):
            error_obj = self._get_attr(choice, "error")
            if error_obj:
                msg = self._get_attr(error_obj, "message") or str(error_obj)
                raise ProviderRuntimeError(msg)
            message = self._get_attr(choice, "message", {})
            normalized_messages.append(
                ProviderMessage(
                    role=self._get_attr(message, "role", "assistant"),
                    content=self._message_content_to_text(self._get_attr(message, "content")),
                    tool_calls=self._extract_tool_calls(message),
                    finish_reason=self._get_attr(choice, "finish_reason"),
                    index=idx,
                    raw_message=message,
                    raw_choice=choice,
                )
            )

        return ProviderResult(
            messages=normalized_messages,
            raw_response=response,
            usage=self._extract_usage(response),
            model=getattr(response, "model", None),
            metadata={},
        )


provider_registry.register_runtime("openai_chat", OpenAIChatRuntime)
provider_registry.register_runtime("openrouter_chat", OpenAIChatRuntime)


# ---------------------------------------------------------------------------
# OpenAI Responses runtime
# ---------------------------------------------------------------------------


class OpenAIResponsesRuntime(OpenAIChatRuntime):
    """Runtime for OpenAI Responses API."""

    def _convert_messages_to_input(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        converted: List[Dict[str, Any]] = []
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content")
            if isinstance(content, list):
                converted.append({"role": role, "content": content})
            else:
                converted.append({"role": role, "content": content})
        return converted

    def _convert_tools_to_responses(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
        if not tools:
            return None
        converted: List[Dict[str, Any]] = []
        for tool in tools:
            if tool.get("type") == "function" and "function" in tool:
                fn = tool.get("function", {}) or {}
                converted.append(
                    {
                        "type": "function",
                        "name": fn.get("name"),
                        "description": fn.get("description"),
                        "parameters": fn.get("parameters"),
                    }
                )
            else:
                converted.append(tool)
        return converted

    def _stream_responses(
        self,
        client: Any,
        payload: Dict[str, Any],
    ) -> Any:
        try:
            stream_ctx = client.responses.stream(**payload)
        except Exception as exc:  # pragma: no cover - wrapped as runtime error
            raise ProviderRuntimeError(str(exc)) from exc

        try:
            with stream_ctx as stream:
                for _ in stream:
                    pass
                return stream.get_final_response()
        except Exception as exc:  # pragma: no cover - wrapped as runtime error
            raise ProviderRuntimeError(str(exc)) from exc

    def invoke(
        self,
        *,
        client: Any,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        stream: bool,
        context: ProviderRuntimeContext,
    ) -> ProviderResult:
        payload: Dict[str, Any] = {
            "model": model,
            "input": self._convert_messages_to_input(messages),
        }

        responses_tools = self._convert_tools_to_responses(tools)
        if responses_tools:
            payload["tools"] = responses_tools

        provider_cfg = (context.agent_config.get("provider_tools") or {}).get("openai", {})

        include_items: List[str] = list(provider_cfg.get("include", []))
        if provider_cfg.get("include_reasoning", True) and "reasoning.encrypted_content" not in include_items:
            include_items.append("reasoning.encrypted_content")
        if include_items:
            payload["include"] = include_items

        if "store" in provider_cfg:
            payload["store"] = bool(provider_cfg.get("store"))

        conversation_id = context.session_state.get_provider_metadata("conversation_id")
        if conversation_id:
            payload["conversation"] = conversation_id

        previous_response_id = context.session_state.get_provider_metadata("previous_response_id")
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id

        extra_payload = context.extra.get("responses_extra") if context.extra else None
        if isinstance(extra_payload, dict):
            payload.update(extra_payload)

        response: Any = None
        if stream:
            response = self._stream_responses(client, payload)

        if response is None:
            try:
                response = self._call_with_raw_response(
                    client.responses,
                    error_context="responses.create",
                    **payload,
                )
            except ProviderRuntimeError:
                raise
            except Exception as exc:  # pragma: no cover - exercised in integration
                raise ProviderRuntimeError(str(exc)) from exc

        normalized_messages: List[ProviderMessage] = []
        encrypted_reasoning: List[Any] = []
        reasoning_summaries: List[str] = []

        for idx, item in enumerate(getattr(response, "output", []) or []):
            item_type = self._get_attr(item, "type")

            if item_type == "message":
                role = self._get_attr(item, "role", "assistant")
                content = self._message_content_to_text(self._get_attr(item, "content", []))
                normalized_messages.append(
                    ProviderMessage(
                        role=role,
                        content=content,
                        finish_reason=self._get_attr(item, "finish_reason", None),
                        index=idx,
                        raw_message=item,
                        annotations={"responses_type": item_type},
                    )
                )
            elif item_type == "function_call":
                call_id = self._get_attr(item, "call_id")
                name = self._get_attr(item, "name")
                arguments = self._get_attr(item, "arguments", "{}")
                if not isinstance(arguments, str):
                    try:
                        arguments = json.dumps(arguments)
                    except Exception:
                        arguments = "{}"
                tool_call = ProviderToolCall(
                    id=call_id,
                    name=name,
                    arguments=arguments,
                    type="function",
                    raw=item,
                )
                normalized_messages.append(
                    ProviderMessage(
                        role="assistant",
                        content=None,
                        tool_calls=[tool_call],
                        finish_reason=self._get_attr(item, "finish_reason", None),
                        index=idx,
                        raw_message=item,
                        annotations={"responses_type": item_type},
                    )
                )
            elif item_type == "reasoning":
                encrypted = self._get_attr(item, "encrypted_content")
                if encrypted is not None:
                    encrypted_reasoning.append(
                        {
                            "encrypted_content": encrypted,
                            "metadata": {
                                "response_id": getattr(response, "id", None),
                                "type": item_type,
                            },
                        }
                    )
                summary_blocks = self._get_attr(item, "summary", []) or []
                summary_text = self._message_content_to_text(summary_blocks)
                if summary_text:
                    reasoning_summaries.append(summary_text)

        usage_dict = self._extract_usage(response)

        metadata: Dict[str, Any] = {}
        response_id = getattr(response, "id", None)
        if response_id:
            metadata["previous_response_id"] = response_id
        conversation_obj = getattr(response, "conversation", None)
        conversation_id_out = getattr(conversation_obj, "id", None) if conversation_obj else None
        if conversation_id_out:
            metadata["conversation_id"] = conversation_id_out

        return ProviderResult(
            messages=normalized_messages,
            raw_response=response,
            usage=usage_dict,
            encrypted_reasoning=encrypted_reasoning or None,
            reasoning_summaries=reasoning_summaries or None,
            model=getattr(response, "model", None),
            metadata=metadata,
        )


provider_registry.register_runtime("openai_responses", OpenAIResponsesRuntime)


# ---------------------------------------------------------------------------
# Anthropic Messages runtime
# ---------------------------------------------------------------------------


class AnthropicMessagesRuntime(ProviderRuntime):
    """Runtime for Anthropic Messages API."""

    def create_client(
        self,
        api_key: str,
        *,
        base_url: Optional[str] = None,
        default_headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        if Anthropic is None:
            raise ProviderRuntimeError("anthropic package not installed")

        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        if default_headers:
            kwargs["default_headers"] = default_headers
        return Anthropic(**kwargs)

    def _convert_messages(self, messages: List[Dict[str, Any]]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        system_prompt: Optional[str] = None
        converted: List[Dict[str, Any]] = []

        for message in messages:
            role = message.get("role")
            content = message.get("content")

            if role == "system" and system_prompt is None:
                system_prompt = content if isinstance(content, str) else json.dumps(content)
                continue

            if isinstance(content, list):
                blocks = content
            else:
                blocks = [{"type": "text", "text": content or ""}]

            converted.append({
                "role": role,
                "content": blocks,
            })

        return system_prompt, converted

    def _extract_usage(self, response: Any) -> Optional[Dict[str, Any]]:
        usage_obj = getattr(response, "usage", None)
        if usage_obj is None:
            return None
        try:
            return dict(usage_obj)
        except Exception:
            try:
                return usage_obj.model_dump()  # type: ignore[attr-defined]
            except Exception:
                return None

    def _get_attr(self, obj: Any, name: str, default: Any = None) -> Any:
        if hasattr(obj, name):
            return getattr(obj, name)
        if isinstance(obj, dict):
            return obj.get(name, default)
        return default

    def _normalize_response(
        self,
        response: Any,
        *,
        usage_override: Optional[Dict[str, Any]] = None,
    ) -> ProviderResult:
        text_parts: List[str] = []
        tool_calls: List[ProviderToolCall] = []
        reasoning_summaries: List[str] = []

        for block in getattr(response, "content", []) or []:
            block_type = self._get_attr(block, "type")
            if block_type == "text":
                text_val = self._get_attr(block, "text", "")
                if text_val:
                    text_parts.append(str(text_val))
            elif block_type == "tool_use":
                call_id = self._get_attr(block, "id")
                name = self._get_attr(block, "name")
                input_payload = self._get_attr(block, "input", {})
                try:
                    arguments = json.dumps(input_payload)
                except Exception:
                    arguments = "{}"
                tool_calls.append(
                    ProviderToolCall(
                        id=call_id,
                        name=name,
                        arguments=arguments,
                        type="function",
                        raw=block,
                    )
                )
            elif block_type == "thinking":
                thinking_text = self._get_attr(block, "text", "")
                if thinking_text:
                    reasoning_summaries.append(str(thinking_text))

        content_text = "".join(text_parts) if text_parts else None
        provider_message = ProviderMessage(
            role="assistant",
            content=content_text,
            tool_calls=tool_calls,
            finish_reason=getattr(response, "stop_reason", None),
            index=0,
            raw_message=response,
            annotations={"anthropic_stop_reason": getattr(response, "stop_reason", None)},
        )

        usage_dict = usage_override if usage_override is not None else self._extract_usage(response)
        metadata: Dict[str, Any] = {}
        if usage_dict:
            for key in [
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "input_tokens",
                "output_tokens",
            ]:
                if key in usage_dict:
                    metadata.setdefault("usage", {})[key] = usage_dict[key]

        return ProviderResult(
            messages=[provider_message],
            raw_response=response,
            usage=usage_dict,
            reasoning_summaries=reasoning_summaries or None,
            model=getattr(response, "model", None),
            metadata=metadata,
        )

    def invoke(
        self,
        *,
        client: Any,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        stream: bool,
        context: ProviderRuntimeContext,
    ) -> ProviderResult:
        system_prompt, converted_messages = self._convert_messages(messages)

        anthropic_cfg = (context.agent_config.get("provider_tools") or {}).get("anthropic", {})
        max_tokens = anthropic_cfg.get("max_output_tokens", 1024)
        temperature = anthropic_cfg.get("temperature")
        request: Dict[str, Any] = {
            "model": model,
            "messages": converted_messages,
            "max_tokens": int(max_tokens) if max_tokens else 1024,
        }

        if system_prompt:
            request["system"] = system_prompt

        if tools:
            request["tools"] = tools

        if temperature is not None:
            request["temperature"] = float(temperature)

        response: Any = None
        usage_override: Optional[Dict[str, Any]] = None

        if stream:
            try:
                stream_ctx = client.messages.stream(**request)
            except Exception as exc:  # pragma: no cover - wrapped by caller
                raise ProviderRuntimeError(str(exc)) from exc

            try:
                with stream_ctx as stream_obj:
                    for _ in stream_obj:
                        pass
                    response = stream_obj.get_final_message()
                    final_usage = getattr(stream_obj, "get_final_usage", None)
                    if callable(final_usage):
                        try:
                            usage_override = self._extract_usage(
                                SimpleNamespace(usage=final_usage())
                            )
                        except Exception:
                            usage_override = None
            except Exception as exc:  # pragma: no cover - wrapped by caller
                raise ProviderRuntimeError(str(exc)) from exc

        if response is None:
            try:
                response = client.messages.create(**request)
            except json.JSONDecodeError as exc:
                snippet = getattr(exc, "doc", "")
                snippet = (snippet or "")[:400].strip()
                raise ProviderRuntimeError(
                    "Failed to decode provider response (non-JSON payload).",
                    details={"body_snippet": snippet},
                ) from exc
            except Exception as exc:  # pragma: no cover - exercised in integration
                raise ProviderRuntimeError(str(exc)) from exc

        return self._normalize_response(response, usage_override=usage_override)


provider_registry.register_runtime("anthropic_messages", AnthropicMessagesRuntime)


__all__ = [
    "ProviderRuntime",
    "ProviderRuntimeContext",
    "ProviderRuntimeError",
    "ProviderRuntimeRegistry",
    "ProviderResult",
    "ProviderMessage",
    "ProviderToolCall",
    "provider_registry",
    "OpenAIChatRuntime",
    "OpenAIResponsesRuntime",
    "AnthropicMessagesRuntime",
]
