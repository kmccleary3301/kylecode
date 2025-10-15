from __future__ import annotations

import copy
import json
import os
import random
import shutil
import time
import uuid
import traceback
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
from types import SimpleNamespace
from dataclasses import asdict

import ray

from kylecode.sandbox_v2 import DevSandboxV2
from kylecode.sandbox_virtualized import SandboxFactory, DeploymentMode
from .core.core import ToolDefinition, ToolParameter
from .dialects.pythonic02 import Pythonic02Dialect
from .dialects.pythonic_inline import PythonicInlineDialect
from .dialects.aider_diff import AiderDiffDialect
from .dialects.unified_diff import UnifiedDiffDialect
from .dialects.opencode_patch import OpenCodePatchDialect
from .dialects.yaml_command import YAMLCommandDialect
from .execution.composite import CompositeToolCaller
from .dialects.bash_block import BashBlockDialect
from .execution.dialect_manager import DialectManager
from .execution.enhanced_executor import EnhancedToolExecutor
from .execution.agent_executor import AgentToolExecutor
from .compilation.tool_yaml_loader import load_yaml_tools
from .compilation.system_prompt_compiler import get_compiler
from .provider_ir import IRFinish, IRDeltaEvent
from .compilation.enhanced_config_validator import EnhancedConfigValidator
from .provider_routing import provider_router
from .provider_adapters import provider_adapter_manager
from .provider_runtime import (
    provider_registry,
    ProviderRuntimeContext,
    ProviderResult,
    ProviderMessage,
    ProviderRuntimeError,
)
from .provider_capability_probe import ProviderCapabilityProbeRunner
from .state.session_state import SessionState
from .state.completion_detector import CompletionDetector
from .messaging.message_formatter import MessageFormatter
from .messaging.markdown_logger import MarkdownLogger
from .error_handling.error_handler import ErrorHandler
from .monitoring.telemetry import TelemetryLogger
from .logging_v2 import LoggerV2Manager
from .logging_v2.api_recorder import APIRequestRecorder
from .logging_v2.prompt_logger import PromptArtifactLogger
from .logging_v2.markdown_transcript import MarkdownTranscriptWriter
from .logging_v2.provider_native_logger import ProviderNativeLogger
from .logging_v2.request_recorder import StructuredRequestRecorder
from .utils.local_ray import LocalActorProxy, identity_get
from .provider_capability_probe import ProviderCapabilityProbeRunner
from .provider_health import RouteHealthManager
from .provider_normalizer import normalize_provider_result
from .provider_metrics import ProviderMetricsCollector


def compute_tool_prompt_mode(tool_prompt_mode: str, will_use_native_tools: bool, config: Dict[str, Any]) -> str:
    """Pure helper for testing prompt mode adjustment (module-level)."""
    if will_use_native_tools:
        suppress_prompts = bool((config or {}).get("provider_tools", {}).get("suppress_prompts", False))
        return "none" if suppress_prompts else "per_turn_append"
    return tool_prompt_mode

def _tools_schema() -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write a text file to the workspace (overwrites).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a text file from the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "List files in a directory in the workspace. Optional depth parameter for tree structure (1-5, default 1).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "depth": {"type": "integer", "description": "Tree depth (1-5, default 1)", "default": 1}
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_shell",
                "description": "Run a shell command in the workspace and return stdout/stderr/exit.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout": {"type": "integer"},
                    },
                    "required": ["command"],
                },
            },
        },
    ]


def _dump_tool_defs(tool_defs: List[ToolDefinition]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for t in tool_defs:
        result.append(
            {
                "type_id": t.type_id,
                "name": t.name,
                "description": t.description,
                "parameters": [
                    {
                        "name": p.name,
                        "type": p.type,
                        "description": p.description,
                        "default": p.default,
                    }
                    for p in t.parameters
                ],
            }
        )
    return result


@ray.remote
class OpenAIConductor:
    def __init__(self, workspace: str, image: str = "python-dev:latest", config: Optional[Dict[str, Any]] = None, *, local_mode: bool = False) -> None:
        # Resolve workspace from v2 config if provided
        cfg_ws = None
        if config:
            try:
                cfg_ws = (config.get("workspace", {}) or {}).get("root")
            except Exception:
                cfg_ws = None
        effective_ws = cfg_ws or workspace
        # Ensure we start from a clean workspace so each run works from a fresh clone
        effective_ws = str(self._prepare_workspace(effective_ws))
        # Choose virtualization mode based on workspace.mirror.mode
        mirror_cfg = ((config or {}).get("workspace", {}) or {}).get("mirror", {})
        mirror_mode = str(mirror_cfg.get("mode", "development")).lower()
        use_virtualized = mirror_cfg.get("enabled", True)
        
        
        print("WORKSPACE", effective_ws)
        print("MIRROR CFG", mirror_cfg)
        
        # Local-host fallback unless explicitly using docker by env
        self.local_mode = bool(local_mode)
        self._ray_get = ray.get if not self.local_mode else identity_get

        self.using_virtualized = bool(use_virtualized) and not self.local_mode
        if self.using_virtualized:
            try:
                mode = DeploymentMode(mirror_mode) if mirror_mode in (m.value for m in DeploymentMode) else DeploymentMode.DEVELOPMENT
            except Exception:
                mode = DeploymentMode.DEVELOPMENT
            factory = SandboxFactory()
            vsb, session_id = factory.create_sandbox(mode, {"runtime": {"image": image}, "workspace": effective_ws})
            self.sandbox = vsb
        elif self.local_mode:
            dev_cls = DevSandboxV2.__ray_metadata__.modified_class
            dev_impl = dev_cls(image=image, session_id=f"local-{uuid.uuid4()}", workspace=effective_ws, lsp_actor=None)
            self.sandbox = LocalActorProxy(dev_impl)
        else:
            self.sandbox = DevSandboxV2.options(name=f"oa-sb-{uuid.uuid4()}").remote(image=image, workspace=effective_ws)

        self.workspace = effective_ws
        self.image = image
        self.config = config or {}
        
        # Initialize components
        self._initialize_dialect_manager()
        self._initialize_yaml_tools()
        self._initialize_config_validator()
        self._initialize_enhanced_executor()

        # Initialize Logging v2 (safe no-op if disabled/missing config)
        self.logger_v2 = LoggerV2Manager(self.config)
        try:
            # Use only workspace basename for logging session id to avoid path tokens
            run_dir = self.logger_v2.start_run(session_id=os.path.basename(os.path.normpath(self.workspace)))
        except Exception:
            run_dir = ""
        self.api_recorder = APIRequestRecorder(self.logger_v2)
        self.structured_request_recorder = StructuredRequestRecorder(self.logger_v2)
        self.prompt_logger = PromptArtifactLogger(self.logger_v2)
        self.md_writer = MarkdownTranscriptWriter()
        self.provider_logger = ProviderNativeLogger(self.logger_v2)
        self.capability_probe_runner = ProviderCapabilityProbeRunner(
            provider_router,
            provider_registry,
            self.logger_v2,
            None,
        )
        self._capability_probes_ran = False
        self.route_health = RouteHealthManager()
        self._current_route_id: Optional[str] = None
        self.provider_metrics = ProviderMetricsCollector()
        
        # Initialize extracted modules
        self.message_formatter = MessageFormatter(self.workspace)
        self.agent_executor = AgentToolExecutor(self.config, self.workspace)
        self.agent_executor.set_enhanced_executor(self.enhanced_executor)
        self.agent_executor.set_config_validator(self.config_validator)

    def _prepare_workspace(self, workspace: str) -> Path:
        """Ensure the workspace directory exists and is empty before use."""
        try:
            path = Path(workspace)
        except Exception:
            path = Path(str(workspace))
        try:
            path = path if path.is_absolute() else (Path.cwd() / path)
            path = path.resolve()
        except Exception:
            # Fallback: use absolute() best-effort without strict resolution
            try:
                path = path.absolute()
            except Exception:
                pass
        try:
            if path.exists():
                shutil.rmtree(path)
        except Exception:
            # If cleanup fails we still attempt to proceed with a fresh directory
            pass
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _initialize_dialect_manager(self):
        """Initialize enhanced tools and configuration validator"""
        self.dialect_manager = DialectManager(self.config)

    def _get_model_routing_preferences(self, route_id: Optional[str]) -> Dict[str, Any]:
        """Return routing preferences for a given configured route."""
        defaults = {
            "disable_stream_on_probe_failure": True,
            "disable_native_tools_on_probe_failure": True,
            "fallback_models": [],
        }
        providers_cfg = (self.config.get("providers") or {})
        if not route_id:
            provider_defaults = providers_cfg.get("routing") or providers_cfg.get("routing_preferences") or {}
            prefs = dict(defaults)
            for key, value in (provider_defaults.items() if isinstance(provider_defaults, dict) else []):
                if value is not None:
                    prefs[key] = value
            return prefs

        models_cfg = providers_cfg.get("models") or []
        for entry in models_cfg:
            if entry.get("id") == route_id:
                routing_cfg = entry.get("routing") or entry.get("routing_preferences") or {}
                prefs = dict(defaults)
                for key, value in (routing_cfg.items() if isinstance(routing_cfg, dict) else []):
                    if value is not None:
                        prefs[key] = value
                return prefs

        provider_defaults = providers_cfg.get("routing") or providers_cfg.get("routing_preferences") or {}
        prefs = dict(defaults)
        for key, value in (provider_defaults.items() if isinstance(provider_defaults, dict) else []):
            if value is not None:
                prefs[key] = value
        return prefs

    def _get_capability_probe_result(
        self,
        session_state: SessionState,
        route_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Fetch previously recorded capability probe result for a route."""
        if not route_id:
            return None
        try:
            probes = session_state.get_provider_metadata("capability_probes", [])
        except Exception:
            probes = []
        if not probes:
            return None
        for entry in probes:
            if entry.get("model_id") == route_id:
                return entry
        return None

    def _log_routing_event(
        self,
        session_state: SessionState,
        markdown_logger: MarkdownLogger,
        *,
        turn_index: Optional[Any],
        tag: str,
        message: str,
        payload: Dict[str, Any],
    ) -> None:
        """Emit routing-related diagnostic across transcript, markdown, and IR."""
        try:
            session_state.add_transcript_entry({tag: payload})
        except Exception:
            pass
        try:
            markdown_logger.log_system_message(message)
        except Exception:
            pass
        try:
            if getattr(self.logger_v2, "run_dir", None):
                self.logger_v2.append_text(
                    "conversation/conversation.md",
                    self.md_writer.system(message),
                )
        except Exception:
            pass
        cursor: str
        if isinstance(turn_index, int) and turn_index >= 0:
            cursor = f"turn_{turn_index}:{tag}"
        elif isinstance(turn_index, str):
            cursor = f"{turn_index}:{tag}"
        else:
            cursor = tag
        try:
            session_state.add_ir_event(
                IRDeltaEvent(
                    cursor=cursor,
                    type="reasoning_meta",
                    payload=dict(payload, message=message),
                )
            )
        except Exception:
            pass

    def _record_stream_policy_metadata(self, session_state: SessionState, policy: Dict[str, Any]) -> None:
        """Store stream policy decisions in session metadata."""
        try:
            history = session_state.get_provider_metadata("stream_policy_history", [])
        except Exception:
            history = []
        try:
            if isinstance(history, list):
                updated_history = list(history)
            else:
                updated_history = [history] if history else []
            updated_history.append(policy)
            session_state.set_provider_metadata("stream_policy_history", updated_history)
            session_state.set_provider_metadata("last_stream_policy", policy)
        except Exception:
            pass

    def _apply_capability_tool_overrides(
        self,
        provider_tools_cfg: Dict[str, Any],
        session_state: SessionState,
        markdown_logger: MarkdownLogger,
    ) -> Dict[str, Any]:
        """Adjust native tool usage based on capability probe outputs."""
        route_id = getattr(self, "_current_route_id", None)
        routing_prefs = self._get_model_routing_preferences(route_id)
        if not routing_prefs.get("disable_native_tools_on_probe_failure", True):
            return provider_tools_cfg
        capability = self._get_capability_probe_result(session_state, route_id)
        if not capability or not capability.get("attempted"):
            return provider_tools_cfg
        if capability.get("tool_stream_success") is not False:
            return provider_tools_cfg
        # Respect explicit overrides favoring native tools when set to False already
        current_override = provider_tools_cfg.get("use_native")
        if current_override is False:
            return provider_tools_cfg
        updated_cfg = dict(provider_tools_cfg)
        updated_cfg["use_native"] = False
        payload = {
            "route": route_id,
            "capabilities": {
                "stream_success": capability.get("stream_success"),
                "tool_stream_success": capability.get("tool_stream_success"),
                "json_mode_success": capability.get("json_mode_success"),
            },
            "reason": "capability_probe_tool_failure",
        }
        message = (
            f"[tool-policy] Disabled native tool usage for route '{route_id}' after capability probe failure."
        )
        self.provider_metrics.add_tool_override(
            route=route_id,
            reason="capability_probe_tool_failure",
        )
        self._log_routing_event(
            session_state,
            markdown_logger,
            turn_index="setup",
            tag="tool_policy",
            message=message,
            payload=payload,
        )
        try:
            history = session_state.get_provider_metadata("tool_policy_history", [])
            if isinstance(history, list):
                updated_history = list(history)
            else:
                updated_history = [history] if history else []
            updated_history.append(
                {
                    "route": route_id,
                    "reason": "capability_probe_tool_failure",
                    "ts": time.time(),
                }
            )
            session_state.set_provider_metadata("tool_policy_history", updated_history)
        except Exception:
            pass
        return updated_cfg

    def _select_fallback_route(
        self,
        primary_route: Optional[str],
        provider_id: Optional[str],
        primary_model: str,
        explicit_candidates: List[str],
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """Choose a fallback route honoring routing prefs and route health."""
        ordered: List[str] = []
        skipped: List[Dict[str, Any]] = []
        seen = set()

        def add_candidate(candidate: Optional[str]) -> None:
            if not candidate:
                return
            if candidate in seen:
                return
            seen.add(candidate)
            ordered.append(candidate)

        for item in explicit_candidates or []:
            add_candidate(str(item))

        if provider_id == "openrouter":
            try:
                _, base_model, _ = provider_router.get_provider_config(primary_route or primary_model)
            except Exception:
                base_model = None
            if base_model and base_model.startswith("openai/"):
                add_candidate(base_model)
            add_candidate("openrouter/openai/gpt-4o-mini")
            add_candidate("gpt-4o-mini")
        elif provider_id == "openai":
            add_candidate("gpt-4o-mini")

        for candidate in ordered:
            try:
                descriptor, resolved = provider_router.get_runtime_descriptor(candidate)
            except Exception:
                skipped.append({"route": candidate, "reason": "descriptor_lookup_failed"})
                continue
            if self.route_health.is_circuit_open(candidate) or self.route_health.is_circuit_open(resolved):
                skipped.append({"route": candidate, "reason": "circuit_open"})
                continue
            return candidate, {"candidates": ordered, "skipped": skipped, "selected": candidate}

        return None, {"candidates": ordered, "skipped": skipped, "selected": None}
    
    # ===== Agent Schema v2 helpers =====
    def _resolve_active_mode(self) -> Optional[str]:
        try:
            seq = (self.config.get("loop", {}) or {}).get("sequence") or []
            features = self.config.get("features", {}) or {}
            for step in seq:
                if not isinstance(step, dict):
                    continue
                if "if" in step and "then" in step:
                    cond = str(step.get("if"))
                    then = step.get("then") or {}
                    ok = False
                    if cond.startswith("features."):
                        key = cond.split("features.", 1)[1]
                        ok = bool(features.get(key))
                    if ok and isinstance(then, dict) and then.get("mode"):
                        return str(then.get("mode"))
                if step.get("mode"):
                    return str(step.get("mode"))
        except Exception:
            pass
        try:
            modes = self.config.get("modes", []) or []
            if modes and isinstance(modes, list):
                name = modes[0].get("name")
                if name:
                    return str(name)
        except Exception:
            pass
        return None

    def _get_mode_config(self, mode_name: Optional[str]) -> Dict[str, Any]:
        if not mode_name:
            return {}
        try:
            for m in self.config.get("modes", []) or []:
                if m.get("name") == mode_name:
                    return m
        except Exception:
            pass
        return {}

    def _filter_tools_by_mode(self, tool_defs: List[ToolDefinition], mode_cfg: Dict[str, Any]) -> List[ToolDefinition]:
        try:
            enabled = mode_cfg.get("tools_enabled")
            disabled = mode_cfg.get("tools_disabled") or []
            if not enabled and not disabled:
                return tool_defs
            enabled_set = set(enabled or [])
            disabled_set = set(disabled)
            out: List[ToolDefinition] = []
            for t in tool_defs:
                name = t.name
                if name in disabled_set:
                    continue
                if enabled and "*" not in enabled_set and name not in enabled_set:
                    continue
                out.append(t)
            return out or tool_defs
        except Exception:
            return tool_defs

    def _apply_turn_strategy_from_loop(self) -> None:
        try:
            loop_ts = (self.config.get("loop", {}) or {}).get("turn_strategy") or {}
            if loop_ts:
                self.config.setdefault("turn_strategy", {})
                self.config["turn_strategy"].update(loop_ts)
        except Exception:
            pass

    def _prepare_concurrency_policy(self) -> Dict[str, Any]:
        """Prepare a simple concurrency policy map from config for testing/logging."""
        policy = self.config.get("concurrency", {}) or {}
        groups = policy.get("groups", []) or []
        tool_to_group: Dict[str, Dict[str, Any]] = {}
        barrier_functions: set[str] = set()
        try:
            for g in groups:
                match_tools = g.get("match_tools", []) or []
                for tn in match_tools:
                    tool_to_group[str(tn)] = {
                        "name": g.get("name"),
                        "max_parallel": int(g.get("max_parallel", 1) or 1),
                        "barrier_after": g.get("barrier_after"),
                    }
                if g.get("barrier_after"):
                    barrier_functions.add(str(g.get("barrier_after")))
        except Exception:
            pass
        return {"tool_to_group": tool_to_group, "barrier_functions": barrier_functions}
    
    def _initialize_yaml_tools(self):
        """Load YAML-defined tools and manipulations map for routing/validation"""
        try:
            # Allow overriding the YAML tool defs directory via config
            tools_cfg = (self.config.get("tools", {}) or {})
            defs_dir = tools_cfg.get("defs_dir") or "implementations/tools/defs"
            overlays = (tools_cfg.get("overlays") or [])
            aliases = (tools_cfg.get("aliases") or {})
            loaded = load_yaml_tools(defs_dir, overlays=overlays, aliases=aliases)
            self.yaml_tools = loaded.tools
            self.yaml_tool_manipulations = loaded.manipulations_by_id
        except Exception:
            self.yaml_tools = []
            self.yaml_tool_manipulations = {}
        # Expose manipulations map to downstream components via config
        try:
            self.config.setdefault("yaml_tool_manipulations", self.yaml_tool_manipulations)
        except Exception:
            pass
    
    def _initialize_config_validator(self):
        """Initialize enhanced configuration validator"""
        enhanced_tools_config = self.config.get("tools", {}).get("registry")
        if enhanced_tools_config and os.path.exists("implementations/tools/enhanced_tools.yaml"):
            try:
                self.config_validator = EnhancedConfigValidator("implementations/tools/enhanced_tools.yaml")
            except Exception:
                self.config_validator = None
        else:
            self.config_validator = None
        
        # Initialize sequential executor for design decision implementation
        self.sequential_executor = None
    
    def _initialize_enhanced_executor(self):
        """Initialize enhanced executor if requested"""
        enhanced_config = self.config.get("enhanced_tools", {})
        # If LSP integration is requested, wrap sandbox even if enhanced executor not enabled
        if enhanced_config.get("lsp_integration", {}).get("enabled", False):
            try:
                from kylecode.sandbox_lsp_integration import LSPEnhancedSandbox
                self.sandbox = LSPEnhancedSandbox.remote(self.sandbox, self.workspace)
            except ImportError:
                pass
        if enhanced_config.get("enabled", False) or enhanced_config.get("lsp_integration", {}).get("enabled", False):
            # Wrap sandbox with LSP capabilities if requested
            sandbox_for_executor = self.sandbox
            if enhanced_config.get("lsp_integration", {}).get("enabled", False):
                try:
                    from kylecode.sandbox_lsp_integration import LSPEnhancedSandbox
                    sandbox_for_executor = LSPEnhancedSandbox.remote(self.sandbox, self.workspace)
                except ImportError:
                    pass  # LSP integration not available, use regular sandbox
            
            self.enhanced_executor = EnhancedToolExecutor(
                sandbox=sandbox_for_executor,
                config=self.config
            )
        else:
            self.enhanced_executor = None

    def _tool_defs_from_yaml(self) -> Optional[List[ToolDefinition]]:
        """Convert YAML EnhancedToolDefinitions to legacy ToolDefinition list for prompts."""
        try:
            if not getattr(self, "yaml_tools", None):
                return None

            # Prefer Schema V2 registry.include if present; else fall back to legacy tools.enabled
            tools_cfg = (self.config.get("tools", {}) or {})
            registry_cfg = (tools_cfg.get("registry", {}) or {})
            include_list = list(registry_cfg.get("include") or [])
            legacy_enabled = (tools_cfg.get("enabled", {}) or {})

            def _is_included(name: str) -> bool:
                if include_list:
                    return name in include_list
                if legacy_enabled:
                    return bool(legacy_enabled.get(name, False))
                # If neither specified, include everything
                return True

            converted: List[ToolDefinition] = []
            for t in self.yaml_tools:
                name = getattr(t, "name", None)
                if not name or not _is_included(name):
                    continue
                params = []
                for p in getattr(t, "parameters", []) or []:
                    params.append(ToolParameter(name=p.name, type=p.type, description=p.description, default=p.default))
                converted.append(
                    ToolDefinition(
                        type_id=getattr(t, "type_id", "python"),
                        name=name,
                        description=getattr(t, "description", ""),
                        parameters=params,
                        blocking=bool(getattr(t, "blocking", False)),
                    )
                )
            return converted if converted else None
        except Exception:
            return None

    def create_file(self, path: str) -> Dict[str, Any]:
        return self._ray_get(self.sandbox.write_text.remote(path, ""))

    def read_file(self, path: str) -> Dict[str, Any]:
        return self._ray_get(self.sandbox.read_text.remote(path))

    def list_dir(self, path: str, depth: int = 1) -> Dict[str, Any]:
        # Always pass virtual paths when using VirtualizedSandbox; else pass normalized absolute
        if getattr(self, 'using_virtualized', False):
            return self._ray_get(self.sandbox.ls.remote(path, depth))
        target = self._normalize_workspace_path(str(path))
        return self._ray_get(self.sandbox.ls.remote(target, depth))

    def run_shell(self, command: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        stream = self._ray_get(self.sandbox.run.remote(command, timeout=timeout or 30, stream=True))
        # Decode adaptive-encoded list materialized; last element is {"exit": code}
        exit_obj = stream[-1] if stream else {"exit": None}
        # Collect only string lines, drop adaptive markers (>>>>> ...)
        lines: list[str] = []
        for x in stream[:-1]:
            if not isinstance(x, str):
                continue
            if x.startswith(">>>>>"):
                continue
            lines.append(x)
        stdout = "\n".join(lines)
        return {"stdout": stdout, "exit": exit_obj.get("exit")}

    def vcs(self, request: Dict[str, Any]) -> Dict[str, Any]:
        return self._ray_get(self.sandbox.vcs.remote(request))

    def _normalize_workspace_path(self, path_in: str) -> str:
        """Normalize a tool-supplied path so it stays within the workspace root."""
        ws_path = Path(self.workspace).resolve()
        if not path_in:
            return str(ws_path)

        raw = str(path_in).strip()
        if not raw:
            return str(ws_path)

        # Absolute paths: prefer workspace-relative when under the workspace
        try:
            abs_candidate = Path(raw)
            if abs_candidate.is_absolute():
                try:
                    rel = abs_candidate.resolve(strict=False).relative_to(ws_path)
                    return str((ws_path / rel).resolve(strict=False))
                except Exception:
                    return str(abs_candidate.resolve(strict=False))
        except Exception:
            pass

        # Work with normalized string form for relative paths
        normalized = raw.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized in ("", "."):
            return str(ws_path)

        ws_name = ws_path.name
        segments = [seg for seg in normalized.split("/") if seg and seg != "."]

        # Strip duplicated workspace prefixes (agent_ws/.../agent_ws/...) lazily
        while segments and segments[0] == ws_name:
            segments.pop(0)

        # Remove any embedded absolute workspace prefix fragments
        ws_str_norm = str(ws_path).replace("\\", "/")
        if ws_str_norm in normalized:
            tail = normalized.split(ws_str_norm, 1)[1].lstrip("/\\")
            segments = [seg for seg in tail.split("/") if seg and seg != "."]

        # Collapse .. components so paths cannot escape the workspace
        cleaned: List[str] = []
        for seg in segments:
            if seg == "..":
                if cleaned:
                    cleaned.pop()
                continue
            cleaned.append(seg)

        candidate = ws_path.joinpath(*cleaned) if cleaned else ws_path
        candidate = candidate.resolve(strict=False)
        try:
            candidate.relative_to(ws_path)
        except ValueError:
            # If resolution escaped the workspace, fall back to workspace root
            return str(ws_path)
        return str(candidate)
    
    def _exec_raw(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        """Raw tool execution without enhanced features (for compatibility)"""
        name = tool_call["function"]
        args = tool_call["arguments"]
        
        if name == "create_file":
            target = self._normalize_workspace_path(str(args.get("path", "")))
            return self.create_file(target)
        if name == "read_file":
            target = self._normalize_workspace_path(str(args.get("path", "")))
            return self.read_file(target)
        if name == "list_dir":
            target = self._normalize_workspace_path(str(args.get("path", "")))
            depth = int(args.get("depth", 1))
            return self.list_dir(target, depth)
        if name == "run_shell":
            return self.run_shell(args["command"], args.get("timeout"))
        if name == "apply_search_replace":
            target = self._normalize_workspace_path(str(args.get("file_name", "")))
            search_text = str(args.get("search", ""))
            replace_text = str(args.get("replace", ""))
            try:
                exists = self._ray_get(self.sandbox.exists.remote(target))
            except Exception:
                exists = False
            if not exists or search_text.strip() == "":
                return self._ray_get(self.sandbox.write_text.remote(target, replace_text))
            return self._ray_get(self.sandbox.edit_replace.remote(target, search_text, replace_text, 1))
        if name == "apply_unified_patch":
            patch_text = str(args.get("patch", ""))
            return self.vcs({
                "action": "apply_patch",
                "params": {
                    "patch": patch_text,
                    "three_way": True,
                    "index": True,
                    "whitespace": "fix",
                    "keep_rejects": True,
                },
            })
        if name == "create_file_from_block":
            path = self._normalize_workspace_path(str(args.get("file_name", "")))
            content = str(args.get("content", ""))
            return self._ray_get(self.sandbox.write_text.remote(path, content))
        if name == "mark_task_complete":
            return {"action": "complete"}
        return {"error": f"unknown tool {name}"}

    def run_agentic_loop(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_steps: int = 12,
        output_json_path: Optional[str] = None,
        stream_responses: bool = False,
        output_md_path: Optional[str] = None,
        tool_prompt_mode: str = "system_once",  # system_once | per_turn_append | system_and_per_turn | none
        completion_sentinel: Optional[str] = ">>>>>> END RESPONSE",
        completion_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # Initialize components
        session_state = SessionState(self.workspace, self.image, self.config)
        completion_detector = CompletionDetector(
            config=completion_config or self.config.get("completion", {}),
            completion_sentinel=completion_sentinel
        )
        markdown_logger = MarkdownLogger(output_md_path)
        # Also seed conversation.md under logging v2 if enabled
        try:
            if self.logger_v2.run_dir:
                self.logger_v2.write_text("conversation/conversation.md", "# Conversation Transcript\n")
        except Exception:
            pass
        telemetry_path = os.environ.get("RAYCODE_TELEMETRY_PATH")
        if not telemetry_path and getattr(self.logger_v2, "run_dir", None):
            telemetry_path = str(Path(self.logger_v2.run_dir) / "meta" / "telemetry.jsonl")
        telemetry = TelemetryLogger(telemetry_path)
        self._active_telemetry_logger = telemetry
        error_handler = ErrorHandler(output_json_path)

        self._ensure_capability_probes(session_state, markdown_logger)
        self.provider_metrics.reset()
        
        # Clear existing log files
        for del_path in [output_md_path, output_json_path]:
            if del_path and os.path.exists(del_path):
                os.remove(del_path)
        
        # Initialize provider and client
        requested_route_id = model
        provider_config, resolved_model, supports_native_tools_for_model = provider_router.get_provider_config(requested_route_id)
        runtime_descriptor, runtime_model = provider_router.get_runtime_descriptor(requested_route_id)
        client_config = provider_router.create_client_config(requested_route_id)

        provider_tools_cfg = dict((self.config.get("provider_tools") or {}))
        api_variant_override = provider_tools_cfg.get("api_variant")
        if api_variant_override and runtime_descriptor.provider_id == "openai":
            variant = str(api_variant_override).lower()
            if variant == "responses":
                runtime_descriptor.runtime_id = "openai_responses"
                runtime_descriptor.default_api_variant = "responses"
            elif variant == "chat":
                runtime_descriptor.runtime_id = "openai_chat"
                runtime_descriptor.default_api_variant = "chat"

        if not client_config["api_key"]:
            raise RuntimeError(f"{provider_config.api_key_env} missing in environment")

        runtime = provider_registry.create_runtime(runtime_descriptor)

        # Create client with provider-specific configuration
        client = runtime.create_client(
            client_config["api_key"],
            base_url=client_config.get("base_url"),
            default_headers=client_config.get("default_headers"),
        )

        self._current_route_id = requested_route_id
        try:
            session_state.set_provider_metadata("route_id", requested_route_id)
        except Exception:
            pass
        model = runtime_model  # Update model to resolved version for API calls
        session_state.set_provider_metadata("provider_id", runtime_descriptor.provider_id)
        session_state.set_provider_metadata("runtime_id", runtime_descriptor.runtime_id)
        session_state.set_provider_metadata("api_variant", runtime_descriptor.default_api_variant)
        session_state.set_provider_metadata("resolved_model", runtime_model)
        try:
            capabilities = provider_router.get_capabilities(model)
            session_state.set_provider_metadata("capabilities", asdict(capabilities))
        except Exception:
            pass
        
        # Defer initial message creation until after tool/dialect resolution so we can compile prompts correctly
        per_turn_prompt = ""
        
        # Setup tool definitions and dialects
        tool_defs_yaml = self._tool_defs_from_yaml()
        tool_defs = tool_defs_yaml or self._get_default_tool_definitions()
        
        # Create dialect mapping and filter based on model configuration
        dialect_mapping = self._create_dialect_mapping()
        active_dialect_names = self.dialect_manager.get_dialects_for_model(
            model, list(dialect_mapping.keys())
        )
        
        # Initialize caller for all modes (needed for parsing)
        # Apply v2 selection ordering (by_model/by_tool_kind) if v2 config detected
        try:
            from .compilation.v2_loader import is_v2_config
            if is_v2_config(self.config):
                active_dialect_names = self._apply_v2_dialect_selection(active_dialect_names, model, tool_defs)
        except Exception:
            pass

        filtered_dialects = [dialect_mapping[name] for name in active_dialect_names if name in dialect_mapping]
        caller = CompositeToolCaller(filtered_dialects)

        # Initialize session state with initial messages (now that we have tools and dialects)
        try:
            if int(self.config.get("version", 0)) == 2 and self.config.get("prompts"):
                mode_name = self._resolve_active_mode()
                comp = get_compiler()
                v2 = comp.compile_v2_prompts(self.config, mode_name, tool_defs, active_dialect_names)
                system_prompt = v2.get("system") or system_prompt
                per_turn_prompt = v2.get("per_turn") or ""
                # Persist compiled system prompt via logging v2
                try:
                    if system_prompt and self.logger_v2.run_dir:
                        self.prompt_logger.save_compiled_system(system_prompt)
                except Exception:
                    pass
                # Persist TPSL catalogs if available
                try:
                    if self.logger_v2.run_dir and isinstance(v2.get("tpsl"), dict):
                        tpsl_meta = v2["tpsl"]
                        # Choose a stable catalog id from template ids (hash part)
                        def _catalog_id(meta: dict) -> str:
                            tid = str(meta.get("template_id", "fallback"))
                            return tid.split("::")[-1].replace("/", "_")
                        files = []
                        # System catalog
                        if isinstance(tpsl_meta.get("system"), dict):
                            cid = _catalog_id(tpsl_meta["system"]) or "tpsl"
                            path = f"prompts/catalogs/{cid}/system_full.md"
                            self.logger_v2.write_text(path, str(tpsl_meta["system"].get("text", "")))
                            files.append({
                                "path": path,
                                "dialect": tpsl_meta["system"].get("dialect"),
                                "detail": tpsl_meta["system"].get("detail"),
                                "template_id": tpsl_meta["system"].get("template_id"),
                            })
                        # Per-turn catalog
                        if isinstance(tpsl_meta.get("per_turn"), dict):
                            cid = _catalog_id(tpsl_meta["per_turn"]) or "tpsl"
                            path = f"prompts/catalogs/{cid}/per_turn_short.md"
                            self.logger_v2.write_text(path, str(tpsl_meta["per_turn"].get("text", "")))
                            files.append({
                                "path": path,
                                "dialect": tpsl_meta["per_turn"].get("dialect"),
                                "detail": tpsl_meta["per_turn"].get("detail"),
                                "template_id": tpsl_meta["per_turn"].get("template_id"),
                            })
                        if files:
                            # Use first catalog id as manifest name
                            name = (files[0]["template_id"] or "tpsl").split("::")[-1]
                            self.prompt_logger.save_catalog(name, files)
                except Exception:
                    pass
        except Exception:
            per_turn_prompt = per_turn_prompt or ""

        enhanced_system_msg = {"role": "system", "content": system_prompt}
        initial_user_content = user_prompt if not per_turn_prompt else (user_prompt + "\n\n" + per_turn_prompt)
        enhanced_user_msg = {"role": "user", "content": initial_user_content}
        session_state.add_message(enhanced_system_msg)
        session_state.add_message(enhanced_user_msg)
        
        # Configure native tools and tool prompt mode
        native_pref_hint = getattr(self, "_native_preference_hint", None)
        provider_tools_cfg = dict(provider_tools_cfg)
        if provider_tools_cfg.get("use_native") is None and native_pref_hint is not None:
            provider_tools_cfg["use_native"] = native_pref_hint
        provider_tools_cfg = self._apply_capability_tool_overrides(
            provider_tools_cfg,
            session_state,
            markdown_logger,
        )
        effective_config = dict(self.config)
        effective_config["provider_tools"] = provider_tools_cfg
        self._provider_tools_effective = provider_tools_cfg
        use_native_tools = provider_router.should_use_native_tools(model, effective_config)
        will_use_native_tools = self._setup_native_tools(model, use_native_tools)
        tool_prompt_mode = self._adjust_tool_prompt_mode(tool_prompt_mode, will_use_native_tools)
        session_state.last_tool_prompt_mode = tool_prompt_mode
        
        # Setup tool prompts and system messages
        local_tools_prompt = self._setup_tool_prompts(
            tool_prompt_mode, tool_defs, active_dialect_names, 
            session_state, markdown_logger, caller
        )
        
        # Add enhanced descriptive fields to initial messages after tool setup
        self._add_enhanced_message_fields(
            tool_prompt_mode, tool_defs, active_dialect_names, 
            session_state, will_use_native_tools, local_tools_prompt, user_prompt
        )
        
        # Initialize markdown log and snapshot
        markdown_logger.log_system_message(system_prompt)
        try:
            if self.logger_v2.run_dir:
                self.logger_v2.append_text("conversation/conversation.md", self.md_writer.system(system_prompt))
        except Exception:
            pass
        # Use enhanced user content for markdown log
        initial_user_content = session_state.messages[1].get("content", user_prompt)
        markdown_logger.log_user_message(initial_user_content)
        try:
            if self.logger_v2.run_dir:
                self.logger_v2.append_text("conversation/conversation.md", self.md_writer.user(initial_user_content))
        except Exception:
            pass
        session_state.write_snapshot(output_json_path, model)
        
        # Main agentic loop - significantly simplified
        run_result = None
        run_loop_error: Optional[Dict[str, Any]] = None
        try:
            run_result = self._run_main_loop(
                runtime,
                client,
                model,
                max_steps,
                output_json_path,
                tool_prompt_mode,
                tool_defs,
                active_dialect_names,
                caller,
                session_state,
                completion_detector,
                markdown_logger,
                error_handler,
                stream_responses,
                local_tools_prompt,
            )
        except Exception as exc:
            run_loop_error = {
                "type": exc.__class__.__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
            session_state.add_transcript_entry({"run_loop_exception": run_loop_error})
            try:
                session_state.set_provider_metadata("run_loop_exception", run_loop_error)
            except Exception:
                pass
            try:
                if self.logger_v2.run_dir:
                    self.logger_v2.write_json("errors/run_loop_exception.json", run_loop_error)
            except Exception:
                pass
        finally:
            self._persist_final_workspace()
            try:
                active_logger = getattr(self, "_active_telemetry_logger", None)
                if active_logger:
                    active_logger.close()
            except Exception:
                pass
            self._active_telemetry_logger = None
        # Defensive: always return a dict result
        if not isinstance(run_result, dict):
            completion_summary = session_state.completion_summary or {}
            completion_summary.setdefault("completed", False)
            if run_loop_error is not None:
                completion_summary["reason"] = "run_loop_exception"
                completion_summary["error"] = run_loop_error
            else:
                completion_summary.setdefault("reason", "no_result")
            session_state.completion_summary = completion_summary
            run_result = {
                "messages": session_state.messages,
                "transcript": session_state.transcript,
                "completion_summary": completion_summary,
                "completion_reason": completion_summary.get("reason", "no_result"),
                "completed": bool(completion_summary.get("completed", False)),
            }

        # Populate finish metadata for IR and persist conversation snapshot
        usage_payload = session_state.get_provider_metadata("usage")
        if not isinstance(usage_payload, dict):
            usage_payload = {}
        finish_reason = "stop" if run_result.get("completed") else "error"
        finish_meta = session_state.get_provider_metadata("raw_finish_meta")
        agent_summary = copy.deepcopy(session_state.completion_summary or {})
        session_state.set_ir_finish(
            IRFinish(
                reason=finish_reason,
                usage=usage_payload,
                provider_meta=finish_meta,
                agent_summary=agent_summary,
            )
        )

        try:
            if self.logger_v2.run_dir:
                conv_id = os.path.basename(self.logger_v2.run_dir)
                conversation_ir = session_state.build_conversation_ir(conversation_id=conv_id)
                self.logger_v2.write_json("meta/conversation_ir.json", asdict(conversation_ir))
        except Exception:
            pass

        return run_result
    
    def _persist_final_workspace(self) -> None:
        """Copy the final workspace into the run's log directory if available."""
        run_dir = getattr(self.logger_v2, "run_dir", None)
        if not run_dir:
            return
        try:
            workspace_path = Path(self.workspace)
        except Exception:
            return
        if not workspace_path.exists() or not workspace_path.is_dir():
            return
        dest = Path(run_dir) / "final_container_dir"
        try:
            workspace_resolved = workspace_path.resolve()
            dest_resolved = dest.resolve(strict=False)
            if str(dest_resolved).startswith(str(workspace_resolved)):
                return
        except Exception:
            pass
        try:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(workspace_path, dest)
        except Exception:
            pass

    def _get_default_tool_definitions(self) -> List[ToolDefinition]:
        """Get default tool definitions"""
        return [
            ToolDefinition(
                type_id="python",
                name="run_shell",
                description="Run a shell command in the workspace and return stdout/exit.",
                parameters=[
                    ToolParameter(name="command", type="string", description="The shell command to execute"),
                    ToolParameter(name="timeout", type="integer", description="Timeout seconds", default=30),
                ],
                blocking=True,
            ),
            ToolDefinition(
                type_id="python",
                name="create_file",
                description=(
                    "Create an empty file (akin to 'touch'). For contents, use diff blocks: "
                    "SEARCH/REPLACE for edits to existing files; unified diff (```patch/```diff) or OpenCode Add File for new files."
                ),
                parameters=[
                    ToolParameter(name="path", type="string"),
                ],
            ),
            ToolDefinition(
                type_id="python",
                name="mark_task_complete",
                description=(
                    "Signal that the task is fully complete. When called, the agent will stop the run."
                ),
                parameters=[],
                blocking=True,
            ),
            ToolDefinition(
                type_id="python",
                name="read_file",
                description="Read a text file from the workspace.",
                parameters=[ToolParameter(name="path", type="string")],
            ),
            ToolDefinition(
                type_id="python",
                name="list_dir",
                description="List files in a directory in the workspace. Optional depth parameter for tree structure (1-5, default 1).",
                parameters=[
                    ToolParameter(name="path", type="string"),
                    ToolParameter(name="depth", type="integer", description="Tree depth (1-5, default 1)", default=1)
                ],
            ),
            ToolDefinition(
                type_id="diff",
                name="apply_search_replace",
                description="Edit code via SEARCH/REPLACE block (Aider-style)",
                parameters=[
                    ToolParameter(name="file_name", type="string"),
                    ToolParameter(name="search", type="string"),
                    ToolParameter(name="replace", type="string"),
                ],
            ),
            ToolDefinition(
                type_id="diff",
                name="apply_unified_patch",
                description="Apply a unified-diff patch (may include new files, edits, deletes)",
                parameters=[
                    ToolParameter(name="patch", type="string", description="Unified diff text; label blocks as ```patch or ```diff"),
                ],
                blocking=True,
            ),
            ToolDefinition(
                type_id="diff",
                name="create_file_from_block",
                description=(
                    "Create a new file from an OpenCode-style Add File block's parsed content. "
                    "Use when not emitting a unified diff."
                ),
                parameters=[
                    ToolParameter(name="file_name", type="string"),
                    ToolParameter(name="content", type="string"),
                ],
            ),
        ]
    
    def _create_dialect_mapping(self) -> Dict[str, Any]:
        """Create mapping from config names to dialect instances"""
        return {
            "pythonic02": Pythonic02Dialect(),
            "pythonic_inline": PythonicInlineDialect(), 
            "bash_block": BashBlockDialect(),
            "aider_diff": AiderDiffDialect(),
            "unified_diff": UnifiedDiffDialect(),
            "opencode_patch": OpenCodePatchDialect(),
            "yaml_command": YAMLCommandDialect(),
        }

    def _apply_v2_dialect_selection(self, current: List[str], model_id: str, tool_defs: List[ToolDefinition]) -> List[str]:
        """Apply v2 dialect selection rules (preference + legacy selection).

        - Reorders the input list according to config.tools.dialects.selection
        - Honors config.tools.dialects.preference for native vs text fallbacks
        - Preserves only dialects present in the current list
        """
        try:
            tools_cfg = (self.config.get("tools", {}) or {})
            dialects_cfg = (tools_cfg.get("dialects", {}) or {})
            selection_cfg = (dialects_cfg.get("selection", {}) or {})
            base_order = self._apply_selection_legacy(current, model_id, tool_defs, selection_cfg)

            preference_cfg = (dialects_cfg.get("preference", {}) or {})
            if preference_cfg:
                ordered, native_hint = self._apply_preference_order(base_order, model_id, tool_defs, preference_cfg)
                self._native_preference_hint = native_hint
                return ordered

            self._native_preference_hint = None
            return base_order
        except Exception:
            self._native_preference_hint = None
            return current

    def _apply_selection_legacy(
        self,
        current: List[str],
        model_id: str,
        tool_defs: List[ToolDefinition],
        selection_cfg: Dict[str, Any],
    ) -> List[str]:
        """Legacy selection logic retained for backward compatibility."""
        by_model: Dict[str, List[str]] = selection_cfg.get("by_model", {}) or {}
        by_tool_kind: Dict[str, List[str]] = selection_cfg.get("by_tool_kind", {}) or {}

        def ordered_intersection(prefer: List[str], available: List[str]) -> List[str]:
            seen = set()
            out: List[str] = []
            for name in prefer:
                if name in available and name not in seen:
                    out.append(name)
                    seen.add(name)
            return out

        import fnmatch as _fnmatch

        preferred: List[str] = []
        if model_id:
            for pattern, prefer_list in by_model.items():
                try:
                    if _fnmatch.fnmatch(model_id, pattern):
                        preferred.extend(ordered_intersection([str(x) for x in (prefer_list or [])], current))
                except Exception:
                    continue

        present_types = {t.type_id for t in (tool_defs or []) if getattr(t, "type_id", None)}
        diff_pref_list = [str(x) for x in (by_tool_kind.get("diff", []) or [])]
        bash_pref_list = [str(x) for x in (by_tool_kind.get("bash", []) or [])]

        known_diff_names = set(diff_pref_list) | {"aider_diff", "unified_diff", "opencode_patch"}
        diff_present = ("diff" in present_types) or any(name in current for name in known_diff_names)
        bash_present = any(name in current for name in (bash_pref_list or ["bash_block"]))

        if diff_present and diff_pref_list:
            preferred.extend(ordered_intersection(diff_pref_list, current))
        if bash_present and bash_pref_list:
            preferred.extend(ordered_intersection(bash_pref_list, current))

        seen = set()
        ordered_pref = [d for d in preferred if (d not in seen and not seen.add(d))]
        remaining = [d for d in current if d not in ordered_pref]
        return ordered_pref + remaining

    def _apply_preference_order(
        self,
        base_order: List[str],
        model_id: str,
        tool_defs: List[ToolDefinition],
        preference_cfg: Dict[str, Any],
    ) -> Tuple[List[str], Optional[bool]]:
        """Apply declarative preference ordering and return (order, native_hint)."""
        import fnmatch as _fnmatch

        available = list(base_order)
        available_set = set(available)
        preferred: List[str] = []
        seen = set()
        native_hint: Optional[bool] = None

        def normalize_entry(entry: Any) -> Tuple[List[str], Optional[bool]]:
            native_override: Optional[bool] = None
            order_values: List[Any]

            if isinstance(entry, dict):
                order_values = entry.get("order")  # type: ignore[assignment]
                if order_values is None and "list" in entry:
                    order_values = entry.get("list")
                native_val = entry.get("native")
                if native_val is not None:
                    native_override = bool(native_val)
            else:
                order_values = entry

            if isinstance(order_values, str):
                raw_items = [order_values]
            elif isinstance(order_values, (list, tuple)):
                raw_items = list(order_values)
            elif order_values is None:
                raw_items = []
            else:
                raw_items = [str(order_values)]

            cleaned: List[str] = []
            for item in raw_items:
                item_str = str(item).strip()
                if not item_str:
                    continue
                if item_str == "provider_native":
                    if native_override is None:
                        native_override = True
                    continue
                cleaned.append(item_str)

            return cleaned, native_override

        def extend(entry: Any) -> None:
            nonlocal native_hint
            order_list, native_override = normalize_entry(entry)
            for name in order_list:
                if name not in available_set:
                    continue
                if name in seen:
                    try:
                        preferred.remove(name)
                    except ValueError:
                        pass
                else:
                    seen.add(name)
                preferred.append(name)
            if native_override is not None and native_hint is None:
                native_hint = native_override

        if model_id:
            for pattern, entry in (preference_cfg.get("by_model", {}) or {}).items():
                try:
                    if _fnmatch.fnmatch(model_id, pattern):
                        extend(entry)
                except Exception:
                    continue

        present_types = {t.type_id for t in (tool_defs or []) if getattr(t, "type_id", None)}
        available_names = set(available)

        for kind, entry in (preference_cfg.get("by_tool_kind", {}) or {}).items():
            if kind == "diff":
                diff_names = normalize_entry(entry)[0]
                diff_present = ("diff" in present_types) or any(name in available_names for name in diff_names)
                if not diff_present:
                    continue
            elif kind == "bash":
                bash_names = normalize_entry(entry)[0]
                bash_present = any(name in available_names for name in (bash_names or ["bash_block"]))
                if not bash_present:
                    continue
            extend(entry)

        extend(preference_cfg.get("default"))

        remaining = [d for d in available if d not in seen]
        ordered = preferred + remaining
        return ordered, native_hint

    def _get_native_preference_hint(self) -> Optional[bool]:
        """Testing helper: expose resolved native preference hint."""
        return getattr(self, "_native_preference_hint", None)

    def _setup_native_tools(self, model: str, use_native_tools: bool) -> bool:
        """Setup native tools configuration"""
        will_use_native_tools = False
        
        if use_native_tools and getattr(self, "yaml_tools", None):
            try:
                # Use provider adapter to filter tools properly
                provider_id = provider_router.parse_model_id(model)[0]
                native_tools, text_based_tools = provider_adapter_manager.filter_tools_for_provider(self.yaml_tools, provider_id)
                will_use_native_tools = bool(native_tools)
                self.current_native_tools = native_tools
                self.current_text_based_tools = text_based_tools
            except Exception:
                will_use_native_tools = False
        
        return will_use_native_tools
    
    def _adjust_tool_prompt_mode(self, tool_prompt_mode: str, will_use_native_tools: bool) -> str:
        """Adjust tool prompt mode based on native tools usage"""
        if will_use_native_tools:
            provider_cfg = getattr(self, "_provider_tools_effective", None) or (self.config.get("provider_tools") or {})
            suppress_prompts = bool(provider_cfg.get("suppress_prompts", False))
            if suppress_prompts:
                return "none"  # Suppress prompts entirely for native tools
            else:
                return "per_turn_append"  # Minimal prompts for native tools
        return tool_prompt_mode
    
    def _setup_tool_prompts(
        self, 
        tool_prompt_mode: str, 
        tool_defs: List[ToolDefinition], 
        active_dialect_names: List[str],
        session_state: SessionState,
        markdown_logger: MarkdownLogger,
        caller
    ) -> str:
        """Setup tool prompts based on mode and return local_tools_prompt"""
        # Use only text-based tools for prompt compilation (exclude provider-native tools)
        prompt_tool_defs = getattr(self, 'current_text_based_tools', None) or tool_defs
        # Apply v2 mode gating and loop turn strategy
        if int(self.config.get("version", 0)) == 2:
            active_mode = self._resolve_active_mode()
            loop_cfg = (self.config.get("loop", {}) or {})
            plan_limit = 0
            plan_turns: Optional[int] = None
            try:
                plan_limit = int(loop_cfg.get("plan_turn_limit") or 0)
            except Exception:
                plan_limit = 0
            if active_mode == "plan" and plan_limit:
                existing = session_state.get_provider_metadata("plan_turns", 0)
                plan_turns = int(existing) + 1
                session_state.set_provider_metadata("plan_turns", plan_turns)
                session_state.set_provider_metadata("plan_turn_limit", plan_limit)
            mode_cfg = self._get_mode_config(active_mode)
            if mode_cfg:
                prompt_tool_defs = self._filter_tools_by_mode(prompt_tool_defs, mode_cfg)
            if active_mode == "plan" and plan_limit and plan_turns is not None and plan_turns >= plan_limit:
                features_cfg = self.config.setdefault("features", {})
                if features_cfg.get("plan", True):
                    features_cfg["plan"] = False
                    session_state.set_provider_metadata("plan_mode_disabled", True)
                    session_state.add_transcript_entry({
                        "mode_transition": {
                            "from": "plan",
                            "to": "build",
                            "reason": "plan_turn_limit",
                            "plan_turns": plan_turns,
                        }
                    })
            self._apply_turn_strategy_from_loop()

        if tool_prompt_mode == "system_compiled_and_persistent_per_turn":
            # NEW MODE: Use comprehensive cached system prompt + small per-turn availability
            compiler = get_compiler()
            
            # Get comprehensive system prompt (cached) - includes primary prompt first
            primary_prompt = session_state.messages[0].get("content", "")
            comprehensive_prompt, tools_hash = compiler.get_or_create_system_prompt(prompt_tool_defs, active_dialect_names, primary_prompt)
            
            # Replace system prompt with comprehensive version (primary + tools)
            session_state.messages[0]["content"] = comprehensive_prompt
            session_state.provider_messages[0]["content"] = comprehensive_prompt
            
            local_tools_prompt = "(using cached comprehensive system prompt with research-based preferences)"
        else:
            # LEGACY MODES: Use old composite caller approach
            local_tools_prompt = caller.build_prompt(prompt_tool_defs)
            
            # Add workspace context if enhanced tools are enabled
            if self.enhanced_executor:
                context = self.enhanced_executor.get_workspace_context()
                if context.get("files_created_this_session"):
                    local_tools_prompt += f"\n\nWORKSPACE CONTEXT:\nFiles created this session: {context['files_created_this_session']}"
                    local_tools_prompt += "\nIMPORTANT: Use edit tools for existing files, not create tools.\n"
            
            # Build a reusable directive text
            tool_directive_text = (
                "\n\nSYSTEM MESSAGE - AVAILABLE TOOLS\n"
                f"<FUNCTIONS>\n{local_tools_prompt}\n</FUNCTIONS>\n"
                "MANDATORY: Respond ONLY with one or more tool calls using <TOOL_CALL> ..., with <BASH>...</BASH> for shell, or with diff blocks (SEARCH/REPLACE for edits; unified diff or OpenCode Add File for new files).\n"
                "You may call multiple tools in one reply; non-blocking tools may run concurrently.\n"
                "Some tools are blocking and must run alone in sequence.\n"
                "Do NOT include any extra prose beyond tool calls or diff blocks.\n"
                "When you deem the task fully complete, call mark_task_complete().\n"
                "NEVER use bash to write large file contents (heredocs, echo >>). For files: call create_file() then apply a diff block for contents.\n"
                "Do NOT include extra prose.\nEND SYSTEM MESSAGE\n"
            )
            
            # Optionally embed the tools directive once into the initial system prompt
            if tool_prompt_mode in ("system_once", "system_and_per_turn"):
                session_state.messages[0]["content"] = (session_state.messages[0].get("content") or "") + tool_directive_text
                session_state.provider_messages[0]["content"] = session_state.messages[0]["content"]
                # Mirror into markdown log
                markdown_logger.log_tool_availability([t.name for t in tool_defs])
        
        return local_tools_prompt
    
    def _add_enhanced_message_fields(
        self,
        tool_prompt_mode: str,
        tool_defs: List[ToolDefinition],
        active_dialect_names: List[str],
        session_state: SessionState,
        will_use_native_tools: bool,
        local_tools_prompt: str,
        user_prompt: str
    ):
        """Add enhanced descriptive fields to initial messages"""
        # System message enhancement
        if tool_prompt_mode in ("system_once", "system_and_per_turn", "system_compiled_and_persistent_per_turn"):
            session_state.messages[0]["compiled_tools_available"] = [
                {
                    "name": t.name,
                    "type_id": t.type_id,
                    "description": t.description,
                    "parameters": [{"name": p.name, "type": p.type, "description": p.description, "default": p.default} for p in t.parameters] if t.parameters else []
                }
                for t in tool_defs
            ]
        
        # User message enhancement
        tools_prompt_content = None
        native_tools_spec = None
        
        if tool_prompt_mode == "system_compiled_and_persistent_per_turn":
            enabled_tools = [t.name for t in tool_defs]
            tools_prompt_content = get_compiler().format_per_turn_availability(enabled_tools, active_dialect_names)
        elif tool_prompt_mode in ("per_turn_append", "system_and_per_turn"):
            tools_prompt_content = local_tools_prompt
        
        # Check if native tools will be used
        if will_use_native_tools:
            try:
                native_tools = getattr(self, 'current_native_tools', [])
                if native_tools:
                    provider_id = provider_router.parse_model_id(self.config.get("model", "gpt-4"))[0]
                    native_tools_spec = provider_adapter_manager.translate_tools_to_native_schema(native_tools, provider_id)
            except Exception:
                pass
        
        session_state.messages[1]["tools_available_prompt"] = tools_prompt_content
        if native_tools_spec:
            session_state.messages[1]["tools"] = native_tools_spec
        
        # For the new mode, append tools to initial user message too
        if tool_prompt_mode == "system_compiled_and_persistent_per_turn":
            enabled_tools = [t.name for t in tool_defs]
            per_turn_availability = get_compiler().format_per_turn_availability(enabled_tools, active_dialect_names)
            initial_user_content = user_prompt + "\n\n" + per_turn_availability
            # Also update the actual message arrays
            session_state.messages[1]["content"] = initial_user_content
            session_state.provider_messages[1]["content"] = initial_user_content
    
    def _run_main_loop(
        self,
        runtime,
        client,
        model: str,
        max_steps: int,
        output_json_path: str,
        tool_prompt_mode: str,
        tool_defs: List[ToolDefinition],
        active_dialect_names: List[str],
        caller,
        session_state: SessionState,
        completion_detector: CompletionDetector,
        markdown_logger: MarkdownLogger,
        error_handler: ErrorHandler,
        stream_responses: bool,
        local_tools_prompt: str
    ) -> Dict[str, Any]:
        """Main agentic loop - significantly simplified and readable"""
        
        completed = False
        step_index = -1

        def finalize_run(
            exit_kind_value: str,
            default_reason: str,
            extra_payload: Optional[Dict[str, Any]] = None,
        ) -> Dict[str, Any]:
            """Finalize loop execution by recording summary, snapshot, and return payload."""

            steps_taken = (step_index + 1) if step_index >= 0 else 0
            summary: Dict[str, Any] = dict(session_state.completion_summary or {})
            summary.setdefault("completed", completed)
            if default_reason and "reason" not in summary:
                summary["reason"] = default_reason
            if "method" not in summary:
                summary["method"] = "provider_error" if exit_kind_value == "provider_error" else "loop_exit"
            summary["exit_kind"] = exit_kind_value
            summary["steps_taken"] = steps_taken
            summary["max_steps"] = max_steps
            session_state.completion_summary = summary
            session_state.set_provider_metadata("final_state", summary)
            session_state.set_provider_metadata("exit_kind", exit_kind_value)
            session_state.set_provider_metadata("steps_taken", steps_taken)
            session_state.set_provider_metadata("max_steps", max_steps)

            metrics_snapshot = self.provider_metrics.snapshot()
            try:
                session_state.set_provider_metadata("provider_metrics", metrics_snapshot)
            except Exception:
                pass
            if getattr(self.logger_v2, "run_dir", None):
                try:
                    self.logger_v2.write_json("meta/provider_metrics.json", metrics_snapshot)
                except Exception:
                    pass
            telemetry_logger = getattr(self, "_active_telemetry_logger", None)
            if telemetry_logger:
                try:
                    telemetry_logger.log(
                        {
                            "event": "provider_metrics",
                            "summary": metrics_snapshot.get("summary", {}),
                            "routes": metrics_snapshot.get("routes", {}),
                            "fallbacks": metrics_snapshot.get("fallbacks", []),
                            "stream_overrides": metrics_snapshot.get("stream_overrides", []),
                            "tool_overrides": metrics_snapshot.get("tool_overrides", []),
                        }
                    )
                except Exception:
                    pass

            try:
                diff = self.vcs({"action": "diff", "params": {"staged": False, "unified": 3}})
            except Exception:
                diff = {"ok": False, "data": {"diff": ""}}

            session_state.write_snapshot(output_json_path, model, diff)
            if stream_responses:
                reason_label = summary.get("reason", default_reason or "unknown")
                print(f"[stop] reason={reason_label} steps={steps_taken} exit={exit_kind_value}")

            result_payload: Dict[str, Any] = {
                "messages": session_state.messages,
                "transcript": session_state.transcript,
                "completion_summary": summary,
                "completion_reason": summary.get("reason", default_reason or "unknown"),
                "completed": summary.get("completed", False),
            }
            if extra_payload:
                result_payload.update(extra_payload)
            return result_payload

        for step_index in range(max_steps):
            try:
                # Build request and get response
                provider_result = self._get_model_response(
                    runtime,
                    client,
                    model,
                    tool_prompt_mode,
                    tool_defs,
                    active_dialect_names,
                    session_state,
                    markdown_logger,
                    stream_responses,
                    local_tools_prompt,
                )
                if session_state.get_provider_metadata("streaming_disabled"):
                    stream_responses = False
                if provider_result.usage:
                    session_state.set_provider_metadata("usage", provider_result.usage)

                if provider_result.encrypted_reasoning:
                    for payload in provider_result.encrypted_reasoning:
                        session_state.reasoning_traces.record_encrypted_trace(payload)

                if provider_result.reasoning_summaries:
                    for summary_text in provider_result.reasoning_summaries:
                        session_state.reasoning_traces.record_summary(summary_text)

                if provider_result.metadata:
                    for meta_key, meta_value in provider_result.metadata.items():
                        session_state.set_provider_metadata(meta_key, meta_value)

                if not provider_result.messages:
                    session_state.add_transcript_entry({"provider_response": "empty"})
                    empty_choice = SimpleNamespace(finish_reason=None, index=None)
                    session_state.add_transcript_entry(
                        error_handler.handle_empty_response(empty_choice)
                    )
                    if not getattr(session_state, "completion_summary", None):
                        session_state.completion_summary = {
                            "completed": False,
                            "reason": "empty_response",
                            "method": "provider_empty",
                        }
                    if stream_responses:
                        print("[stop] reason=empty-response")
                    break

                for provider_message in provider_result.messages:
                    # Log model response
                    self._log_provider_message(
                        provider_message, session_state, markdown_logger, stream_responses
                    )

                    # Handle empty responses per message
                    if not provider_message.tool_calls and not (provider_message.content or ""):
                        empty_choice = SimpleNamespace(
                            finish_reason=provider_message.finish_reason,
                            index=provider_message.index,
                        )
                        session_state.add_transcript_entry(
                            error_handler.handle_empty_response(empty_choice)
                        )
                        if not getattr(session_state, "completion_summary", None):
                            session_state.completion_summary = {
                                "completed": False,
                                "reason": "empty_response",
                                "method": "provider_empty",
                            }
                        if stream_responses:
                            print("[stop] reason=empty-response")
                        completed = False
                        break

                    # Process tool calls or content
                    completion_detected = self._process_model_output(
                        provider_message,
                        caller,
                        tool_defs,
                        session_state,
                        completion_detector,
                        markdown_logger,
                        error_handler,
                        stream_responses,
                        model,
                    )

                    if completion_detected:
                        completed = True
                        if not getattr(session_state, "completion_summary", None):
                            session_state.completion_summary = {
                                "completed": True,
                                "reason": "completion_detected",
                                "method": "completion_detector",
                            }
                        else:
                            session_state.completion_summary.setdefault("completed", True)
                        break
                if completed:
                    break

            except Exception as e:
                # Handle provider errors and surface immediately to logs/stdout
                result = error_handler.handle_provider_error(
                    e, session_state.messages, session_state.transcript
                )
                warning_text = (
                    f"[provider-error] {result.get('error_type', type(e).__name__)}: "
                    f"{result.get('error', str(e))}"
                )
                try:
                    markdown_logger.log_system_message(warning_text)
                except Exception:
                    pass
                try:
                    session_state.add_transcript_entry({
                        "provider_error": {
                            "type": result.get("error_type"),
                            "message": result.get("error"),
                            "hint": result.get("hint"),
                        }
                    })
                except Exception:
                    pass
                try:
                    if getattr(self.logger_v2, "run_dir", None):
                        turn_idx = session_state.get_provider_metadata(
                            "current_turn_index",
                            len(session_state.transcript) + 1,
                        )
                        self._persist_error_artifacts(turn_idx, result)
                except Exception:
                    pass
                try:
                    print(warning_text)
                    if result.get("hint"):
                        print(f"Hint: {result['hint']}")
                    if result.get("traceback"):
                        print(result["traceback"])
                    details = result.get("details")
                    snippet = None
                    if isinstance(details, dict):
                        snippet = details.get("body_snippet")
                    if snippet:
                        print(f"Provider response snippet: {snippet}")
                except Exception:
                    pass
                summary = dict(session_state.completion_summary or {})
                summary.setdefault("completed", False)
                if result.get("error_type"):
                    summary.setdefault("reason", result.get("error_type"))
                    summary.setdefault("error_type", result.get("error_type"))
                else:
                    summary.setdefault("reason", "provider_error")
                if result.get("hint"):
                    summary.setdefault("hint", result.get("hint"))
                summary.setdefault("method", "provider_error")
                session_state.completion_summary = summary
                extra_payload = {
                    "provider_error": result,
                    "error": result.get("error"),
                    "error_type": result.get("error_type"),
                    "hint": result.get("hint"),
                    "details": result.get("details"),
                }
                completed = False
                return finalize_run("provider_error", summary.get("reason", "provider_error"), extra_payload)

        # Post-loop finalization for successful or exhausted runs
        steps_taken = (step_index + 1) if step_index >= 0 else 0
        if not session_state.completion_summary:
            if completed:
                reason = "completion_detected"
            elif max_steps and steps_taken >= max_steps:
                reason = "max_steps_exhausted"
            else:
                reason = "loop_terminated"
            session_state.completion_summary = {
                "completed": completed,
                "reason": reason,
                "method": "loop_exit",
            }

        default_reason = session_state.completion_summary.get("reason", "loop_terminated")
        return finalize_run("loop_exit", default_reason)

    def _apply_streaming_policy_for_turn(
        self,
        runtime,
        model: str,
        tools_schema: Optional[List[Dict[str, Any]]],
        stream_requested: bool,
        session_state: SessionState,
        markdown_logger: MarkdownLogger,
        turn_index: int,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Apply provider-specific streaming policy for the current turn."""

        effective_stream = stream_requested
        policy: Optional[Dict[str, Any]] = None
        descriptor = getattr(runtime, "descriptor", None)
        provider_id = getattr(descriptor, "provider_id", None)
        runtime_id = getattr(descriptor, "runtime_id", None)

        if stream_requested and tools_schema and provider_id == "openrouter":
            effective_stream = False
            policy = {
                "turn_index": turn_index,
                "model": model,
                "provider": provider_id,
                "runtime": runtime_id,
                "reason": "openrouter_tool_turn_policy",
                "stream_requested": True,
                "stream_effective": False,
            }
            self.provider_metrics.add_stream_override(
                route=getattr(self, "_current_route_id", None),
                reason="openrouter_tool_turn_policy",
            )
            try:
                session_state.add_transcript_entry({"stream_policy": policy})
            except Exception:
                pass
            note = (
                "[stream-policy] Forcing stream=false for OpenRouter tool turn "
                f"(turn={turn_index}, model={model})."
            )
            try:
                markdown_logger.log_system_message(note)
            except Exception:
                pass
            try:
                if getattr(self.logger_v2, "run_dir", None):
                    self.logger_v2.append_text(
                        "conversation/conversation.md",
                        self.md_writer.system(note),
                    )
            except Exception:
                pass
        route_id = getattr(self, "_current_route_id", None)
        capability = self._get_capability_probe_result(session_state, route_id)
        routing_prefs = self._get_model_routing_preferences(route_id)
        if (
            capability
            and capability.get("attempted")
            and capability.get("stream_success") is False
            and effective_stream
            and routing_prefs.get("disable_stream_on_probe_failure", True)
        ):
            effective_stream = False
            override_payload = {
                "turn_index": turn_index,
                "model": model,
                "provider": provider_id,
                "runtime": runtime_id,
                "reason": "capability_probe_stream_failure",
                "stream_requested": stream_requested,
                "stream_effective": False,
                "route_id": route_id,
            }
            if policy is None:
                override_payload["reasons"] = ["capability_probe_stream_failure"]
                policy = override_payload
            else:
                merged = dict(policy)
                reasons = merged.get("reasons")
                if not isinstance(reasons, list):
                    reasons_list: List[str] = []
                    if merged.get("reason"):
                        reasons_list.append(merged["reason"])
                else:
                    reasons_list = list(reasons)
                reasons_list.append("capability_probe_stream_failure")
                merged["reasons"] = reasons_list
                merged["capability_override"] = True
                merged["stream_effective"] = False
                merged["stream_requested"] = stream_requested
                merged["route_id"] = route_id
                policy = merged
            payload = {
                "route": route_id,
                "capabilities": {
                    "stream_success": capability.get("stream_success"),
                    "tool_stream_success": capability.get("tool_stream_success"),
                    "json_mode_success": capability.get("json_mode_success"),
                },
                "policy": policy,
            }
            message = (
                f"[stream-policy] Disabled streaming for route '{route_id}' based on capability probe results."
            )
            self.provider_metrics.add_stream_override(
                route=route_id,
                reason="capability_probe_stream_failure",
            )
            self._log_routing_event(
                session_state,
                markdown_logger,
                turn_index=turn_index,
                tag="stream_policy",
                message=message,
                payload=payload,
            )
            try:
                session_state.set_provider_metadata("capability_stream_override", {
                    "route": route_id,
                    "turn_index": turn_index,
                    "reason": "capability_probe_stream_failure",
                })
            except Exception:
                pass
        if policy is not None:
            self._record_stream_policy_metadata(session_state, policy)
        return effective_stream, policy

    def _persist_error_artifacts(self, turn_index: int, payload: Dict[str, Any]) -> None:
        """Persist error payload and associated diagnostics artifacts."""

        run_dir = getattr(self.logger_v2, "run_dir", None)
        if not run_dir:
            return

        try:
            self.logger_v2.write_json(f"errors/turn_{turn_index}.json", payload)
            details = payload.get("details")
            if not isinstance(details, dict):
                return
            headers = details.get("response_headers")
            if headers:
                self.logger_v2.write_json(
                    f"raw/responses/turn_{turn_index}.headers.json",
                    headers,
                )
            raw_b64 = details.get("raw_body_b64")
            if raw_b64:
                self.logger_v2.write_text(
                    f"raw/responses/turn_{turn_index}.body.b64",
                    raw_b64,
                )
            raw_excerpt = details.get("raw_excerpt")
            if raw_excerpt:
                self.logger_v2.write_text(
                    f"raw/responses/turn_{turn_index}.raw_excerpt.txt",
                    raw_excerpt,
                )
            html_excerpt = details.get("html_excerpt")
            if html_excerpt:
                self.logger_v2.write_text(
                    f"raw/responses/turn_{turn_index}.html_excerpt.txt",
                    html_excerpt,
                )
        except Exception:
            pass

    def _retry_with_fallback(
        self,
        runtime,
        client,
        model: str,
        messages: List[Dict[str, Any]],
        tools_schema: Optional[List[Dict[str, Any]]],
        runtime_context: ProviderRuntimeContext,
        *,
        stream_responses: bool,
        session_state: SessionState,
        markdown_logger: MarkdownLogger,
        attempted: List[Tuple[str, bool, Optional[str]]],
        last_error: Optional[ProviderRuntimeError],
    ) -> Optional[ProviderResult]:
        """Attempt a retry and fallback routing when the primary call fails."""

        descriptor = getattr(runtime, "descriptor", None)
        provider_id = getattr(descriptor, "provider_id", None)
        runtime_id = getattr(descriptor, "runtime_id", None)

        def _record_degraded(route_model: str, reason: str) -> None:
            try:
                degraded = session_state.get_provider_metadata("degraded_routes", {})
                if not isinstance(degraded, dict):
                    degraded = {}
                info = degraded.get(route_model, {})
                history = info.get("history", [])
                if not isinstance(history, list):
                    history = [history] if history else []
                history.append(
                    {
                        "reason": reason,
                        "provider": provider_id,
                        "runtime": runtime_id,
                        "turn": session_state.get_provider_metadata("current_turn_index"),
                    }
                )
                info.update({
                    "reason": reason,
                    "provider": provider_id,
                    "runtime": runtime_id,
                    "history": history,
                })
                degraded[route_model] = info
                session_state.set_provider_metadata("degraded_routes", degraded)
            except Exception:
                pass

        def _sleep_with_jitter(base: float) -> None:
            jitter = base * 0.25
            wait_time = base + random.uniform(-jitter, jitter)
            if wait_time < 0:
                wait_time = base
            try:
                time.sleep(wait_time)
            except Exception:
                pass

        def _simplify_result(result: ProviderResult) -> ProviderResult:
            return result

        def _invoke(target_model: str) -> ProviderResult:
            return runtime.invoke(
                client=client,
                model=target_model,
                messages=messages,
                tools=tools_schema,
                stream=stream_responses,
                context=runtime_context,
            )

        def _log_retry(route_model: str, reason: str, attempt: str) -> None:
            message = (
                f"[provider-retry] route={route_model} attempt={attempt} reason={reason}"
            )
            try:
                markdown_logger.log_system_message(message)
            except Exception:
                pass
            try:
                if getattr(self.logger_v2, "run_dir", None):
                    self.logger_v2.append_text(
                        "conversation/conversation.md",
                        self.md_writer.system(message),
                    )
            except Exception:
                pass
            try:
                session_state.add_transcript_entry({
                    "provider_retry": {
                        "route": route_model,
                        "attempt": attempt,
                        "reason": reason,
                    }
                })
            except Exception:
                pass

        if last_error and not attempted:
            self.route_health.record_failure(model, str(last_error))
            self._update_health_metadata(session_state)

        same_route_reason = str(last_error) if last_error else "retry"
        backoff_seconds = 0.6
        _log_retry(model, same_route_reason, "retry")
        _sleep_with_jitter(backoff_seconds)

        try:
            result = _invoke(model)
            attempted.append((model, stream_responses, None))
            self.route_health.record_success(model)
            self._update_health_metadata(session_state)
            return _simplify_result(result)
        except ProviderRuntimeError as retry_error:
            attempted.append((model, stream_responses, str(retry_error) or retry_error.__class__.__name__))
            last_error = retry_error
            self.route_health.record_failure(model, str(retry_error) or retry_error.__class__.__name__)
            self._update_health_metadata(session_state)

        route_id = None
        if runtime_context and isinstance(runtime_context.extra, dict):
            route_id = runtime_context.extra.get("route_id")
        routing_prefs = self._get_model_routing_preferences(route_id)
        explicit_fallbacks = routing_prefs.get("fallback_models") or []
        fallback_model, fallback_diag = self._select_fallback_route(
            route_id,
            provider_id,
            model,
            explicit_fallbacks,
        )

        if not fallback_model:
            if last_error:
                raise last_error
            return None

        fallback_reason = str(last_error) if last_error else "fallback"
        _record_degraded(model, fallback_reason)
        _log_retry(fallback_model, fallback_reason, "fallback")
        self.provider_metrics.add_fallback(primary=model, fallback=fallback_model, reason=fallback_reason)
        turn_hint = None
        if runtime_context and isinstance(runtime_context.extra, dict):
            turn_hint = runtime_context.extra.get("turn_index")
        self._log_routing_event(
            session_state,
            markdown_logger,
            turn_index=turn_hint,
            tag="fallback_route",
            message=(
                f"[routing] Selected fallback route '{fallback_model}' after '{fallback_reason}'."
            ),
            payload={
                "from": route_id or model,
                "reason": fallback_reason,
                "diagnostics": fallback_diag,
            },
        )

        try:
            fallback_runtime_descriptor, fallback_model_resolved = provider_router.get_runtime_descriptor(fallback_model)
            fallback_runtime = provider_registry.create_runtime(fallback_runtime_descriptor)
            fallback_client_config = provider_router.create_client_config(fallback_model)
            fallback_runtime_context = ProviderRuntimeContext(
                session_state=runtime_context.session_state,
                agent_config=runtime_context.agent_config,
                stream=False,
                extra=dict(runtime_context.extra or {}, fallback_of=model, route_id=fallback_model),
            )
            fallback_client = fallback_runtime.create_client(
                fallback_client_config["api_key"],
                base_url=fallback_client_config.get("base_url"),
                default_headers=fallback_client_config.get("default_headers"),
            )
            try:
                if getattr(self.logger_v2, "include_structured_requests", True):
                    turn_idx = runtime_context.extra.get("turn_index") if runtime_context.extra else None
                    if turn_idx is not None:
                        try:
                            turn_for_record = int(turn_idx)
                        except Exception:
                            turn_for_record = None
                    else:
                        turn_for_record = None
                    if turn_for_record is not None:
                        headers_snapshot = dict(fallback_client_config.get("default_headers") or {})
                        if getattr(fallback_runtime_descriptor, "provider_id", None) == "openrouter":
                            headers_snapshot.setdefault("Accept", "application/json; charset=utf-8")
                            headers_snapshot.setdefault("Accept-Encoding", "identity")
                        self.structured_request_recorder.record_request(
                            turn_for_record,
                            provider_id=fallback_runtime_descriptor.provider_id,
                            runtime_id=fallback_runtime_descriptor.runtime_id,
                            model=fallback_model_resolved,
                            request_headers=headers_snapshot,
                            request_body={
                                "model": fallback_model_resolved,
                                "messages": messages,
                                "tools": tools_schema,
                                "stream": False,
                            },
                            stream=False,
                            tool_count=len(tools_schema or []),
                            endpoint=fallback_client_config.get("base_url"),
                            attempt=len(attempted),
                            extra={"fallback_of": model},
                        )
            except Exception:
                pass
            start_ts = time.time()
            result = fallback_runtime.invoke(
                client=fallback_client,
                model=fallback_model_resolved,
                messages=messages,
                tools=tools_schema,
                stream=False,
                context=fallback_runtime_context,
            )
            elapsed = time.time() - start_ts
            try:
                self.provider_metrics.add_call(
                    fallback_model_resolved,
                    stream=False,
                    elapsed=elapsed,
                    outcome="success",
                )
            except Exception:
                pass
            session_state.set_provider_metadata("fallback_route", {
                "from": model,
                "to": fallback_model,
                "provider": fallback_runtime_descriptor.provider_id,
            })
            self.route_health.record_success(fallback_model)
            self._update_health_metadata(session_state)
            return result
        except Exception as exc:
            elapsed = time.time() - start_ts if 'start_ts' in locals() else 0.0
            try:
                self.provider_metrics.add_call(
                    fallback_model,
                    stream=False,
                    elapsed=elapsed,
                    outcome="error",
                    error_reason=str(exc),
                )
            except Exception:
                pass
            attempted.append((fallback_model, False, str(exc) or exc.__class__.__name__))
            if last_error:
                raise last_error
            raise ProviderRuntimeError(str(exc)) from exc

    def _ensure_capability_probes(self, session_state: SessionState, markdown_logger: MarkdownLogger) -> None:
        if self._capability_probes_ran:
            return
        try:
            self.capability_probe_runner.markdown_logger = markdown_logger
            self.capability_probe_runner.run(self.config, session_state)
        except Exception:
            pass
        finally:
            self._capability_probes_ran = True

    def _update_health_metadata(self, session_state: SessionState) -> None:
        try:
            session_state.set_provider_metadata("route_health", self.route_health.snapshot())
        except Exception:
            pass

    def _invoke_runtime_with_streaming(
        self,
        runtime,
        client,
        model: str,
        send_messages: List[Dict[str, Any]],
        tools_schema: Optional[List[Dict[str, Any]]],
        stream_responses: bool,
        runtime_context: ProviderRuntimeContext,
        session_state: SessionState,
        markdown_logger: MarkdownLogger,
    ) -> Tuple[ProviderResult, bool]:
        """Invoke provider runtime, falling back to non-streaming on failure."""

        fallback_stream_reason: Optional[str] = None
        result: Optional[ProviderResult] = None
        last_error: Optional[ProviderRuntimeError] = None
        success_recorded = False

        if self.route_health.is_circuit_open(model):
            notice = (
                f"[circuit-open] Skipping direct call for route {model}; attempting fallback."
            )
            self.provider_metrics.add_circuit_skip(model)
            try:
                markdown_logger.log_system_message(notice)
            except Exception:
                pass
            try:
                if getattr(self.logger_v2, "run_dir", None):
                    self.logger_v2.append_text(
                        "conversation/conversation.md",
                        self.md_writer.system(notice),
                    )
            except Exception:
                pass
            try:
                session_state.add_transcript_entry({"circuit_open": {"model": model, "notice": notice}})
            except Exception:
                pass
            circuit_error = ProviderRuntimeError("route_circuit_open")
            fallback_result = self._retry_with_fallback(
                runtime,
                client,
                model,
                send_messages,
                tools_schema,
                runtime_context,
                stream_responses=False,
                session_state=session_state,
                markdown_logger=markdown_logger,
                attempted=[],
                last_error=circuit_error,
            )
            self._update_health_metadata(session_state)
            if fallback_result is not None:
                return fallback_result, False
            raise circuit_error

        def _is_tool_turn() -> bool:
            if not tools_schema:
                return False
            for msg in reversed(session_state.messages):
                role = msg.get("role")
                if role == "assistant":
                    tool_calls = msg.get("tool_calls") or []
                    return bool(tool_calls)
                if role == "user":
                    break
            return False

        attempted_models = []

        def _call_runtime(target_model: str, use_stream: bool) -> ProviderResult:
            start_time = time.time()
            try:
                call_result = runtime.invoke(
                    client=client,
                    model=target_model,
                    messages=send_messages,
                    tools=tools_schema,
                    stream=use_stream,
                    context=runtime_context,
                )
                elapsed = time.time() - start_time
                self.provider_metrics.add_call(
                    target_model,
                    stream=use_stream,
                    elapsed=elapsed,
                    outcome="success",
                )
                return call_result
            except ProviderRuntimeError as exc:
                elapsed = time.time() - start_time
                details = getattr(exc, "details", None)
                html_detected = False
                if isinstance(details, dict):
                    html_detected = bool(details.get("html_detected"))
                self.provider_metrics.add_call(
                    target_model,
                    stream=use_stream,
                    elapsed=elapsed,
                    outcome="error",
                    error_reason=str(exc),
                    html_detected=html_detected,
                    details=details if isinstance(details, dict) else None,
                )
                raise

        def _maybe_disable_stream(reason: str) -> None:
            try:
                session_state.set_provider_metadata("streaming_disabled", True)
            except Exception:
                pass
            warning_payload = {
                "provider": runtime.descriptor.provider_id,
                "runtime": runtime.descriptor.runtime_id,
                "reason": reason,
            }
            try:
                session_state.add_transcript_entry({"streaming_disabled": warning_payload})
            except Exception:
                pass
            warning_text = (
                "[streaming-disabled] "
                f"Provider {runtime.descriptor.provider_id} ({runtime.descriptor.runtime_id}) "
                f"rejected streaming: {reason}. Falling back to non-streaming."
            )
            try:
                markdown_logger.log_system_message(warning_text)
            except Exception:
                pass
            try:
                if getattr(self.logger_v2, "run_dir", None):
                    self.logger_v2.append_text(
                        "conversation/conversation.md",
                        self.md_writer.system(warning_text),
                    )
            except Exception:
                pass
            try:
                print(warning_text)
            except Exception:
                pass

        if stream_responses:
            try:
                result = _call_runtime(model, True)
                attempted_models.append((model, True, None))
                self.route_health.record_success(model)
                success_recorded = True
                self._update_health_metadata(session_state)
            except ProviderRuntimeError as exc:
                fallback_stream_reason = str(exc) or exc.__class__.__name__
                last_error = exc
                attempted_models.append((model, True, fallback_stream_reason))
                self.route_health.record_failure(model, fallback_stream_reason)
                self._update_health_metadata(session_state)

        used_streaming = stream_responses and result is not None

        if result is None:
            if fallback_stream_reason and stream_responses:
                warning_payload = {
                    "provider": runtime.descriptor.provider_id,
                    "runtime": runtime.descriptor.runtime_id,
                    "reason": fallback_stream_reason,
                }
                self.provider_metrics.add_stream_override(
                    route=getattr(self, "_current_route_id", None),
                    reason=fallback_stream_reason,
                )
                session_state.add_transcript_entry({"streaming_disabled": warning_payload})
                warning_text = (
                    "[streaming-disabled] "
                    f"Provider {runtime.descriptor.provider_id} ({runtime.descriptor.runtime_id}) "
                    f"rejected streaming: {fallback_stream_reason}. Falling back to non-streaming."
                )
                try:
                    markdown_logger.log_system_message(warning_text)
                except Exception:
                    pass
                try:
                    if getattr(self.logger_v2, "run_dir", None):
                        self.logger_v2.append_text(
                            "conversation/conversation.md",
                            self.md_writer.system(warning_text),
                        )
                except Exception:
                    pass
                try:
                    session_state.set_provider_metadata("streaming_disabled", True)
                except Exception:
                    pass
                try:
                    print(warning_text)
                except Exception:
                    pass
            try:
                result = _call_runtime(model, False)
                attempted_models.append((model, False, None))
                used_streaming = False
                self.route_health.record_success(model)
                success_recorded = True
                self._update_health_metadata(session_state)
            except ProviderRuntimeError as exc:
                last_error = exc
                attempted_models.append((model, False, str(exc) or exc.__class__.__name__))
                result = None
                self.route_health.record_failure(model, str(exc) or exc.__class__.__name__)
                self._update_health_metadata(session_state)

        if result is None and _is_tool_turn():
            history = session_state.get_provider_metadata("streaming_disabled")
            if not history:
                reason_text = str(last_error) if last_error else "tool_turn_retry"
                _maybe_disable_stream(reason_text)

        if result is None:
            fallback_result = self._retry_with_fallback(
                runtime,
                client,
                model,
                send_messages,
                tools_schema,
                runtime_context,
                stream_responses=False,
                session_state=session_state,
                markdown_logger=markdown_logger,
                attempted=attempted_models,
                last_error=last_error,
            )
            if fallback_result is not None:
                result = fallback_result
                used_streaming = False
                success_recorded = True
            elif last_error:
                raise last_error


        try:
            if result is not None:
                session_state.set_provider_metadata("raw_finish_meta", result.metadata)
        except Exception:
            pass

        if result is not None and not success_recorded:
            self.route_health.record_success(model)
            self._update_health_metadata(session_state)
        else:
            self._update_health_metadata(session_state)

        return result, used_streaming

        steps_taken = (step_index + 1) if step_index >= 0 else 0

        if not getattr(session_state, "completion_summary", None):
            reason = "max_steps_exhausted" if max_steps and steps_taken >= max_steps and not completed else "loop_terminated"
            session_state.completion_summary = {
                "completed": completed,
                "reason": reason,
                "method": "loop_exit",
            }

        session_state.completion_summary.setdefault("completed", completed)
        session_state.completion_summary.setdefault("steps_taken", steps_taken)
        session_state.completion_summary.setdefault("max_steps", max_steps)

        # Final snapshot and return
        try:
            diff = self.vcs({"action": "diff", "params": {"staged": False, "unified": 3}})
        except Exception:
            diff = {"ok": False, "data": {"diff": ""}}

        session_state.write_snapshot(output_json_path, model, diff)
        if stream_responses:
            print(f"[stop] reason=end-of-loop steps={steps_taken}")

        completion_summary = session_state.completion_summary or {}
        completion_reason = completion_summary.get("reason", "unknown")
        result = {
            "messages": session_state.messages,
            "transcript": session_state.transcript,
            "completion_summary": completion_summary,
            "completion_reason": completion_reason,
            "completed": completion_summary.get("completed", False),
        }
        return result
    
    def _get_model_response(
        self,
        runtime,
        client,
        model: str,
        tool_prompt_mode: str,
        tool_defs: List[ToolDefinition],
        active_dialect_names: List[str],
        session_state: SessionState,
        markdown_logger: MarkdownLogger,
        stream_responses: bool,
        local_tools_prompt: str
    ) -> ProviderResult:
        """Get response from the model with proper tool configuration."""
        # Build per-turn message list
        send_messages = [dict(m) for m in session_state.provider_messages]
        
        # Ensure last message is user; if not, append a user wrapper for tools prompt
        if not send_messages or send_messages[-1].get("role") != "user":
            send_messages.append({"role": "user", "content": ""})
        
        # Add tool prompts based on mode
        turn_index = len(session_state.transcript) + 1
        try:
            session_state.set_provider_metadata("current_turn_index", turn_index)
        except Exception:
            pass
        per_turn_written_text = None
        if tool_prompt_mode in ("per_turn_append", "system_and_per_turn"):
            # Build full tool directive text (restored from original logic)
            tool_directive_text = (
                "\n\nSYSTEM MESSAGE - AVAILABLE TOOLS\n"
                f"<FUNCTIONS>\n{local_tools_prompt}\n</FUNCTIONS>\n"
                "MANDATORY: Respond ONLY with one or more tool calls using <TOOL_CALL> ..., with <BASH>...</BASH> for shell, or with diff blocks (SEARCH/REPLACE for edits; unified diff or OpenCode Add File for new files).\n"
                "You may call multiple tools in one reply; non-blocking tools may run concurrently.\n"
                "Some tools are blocking and must run alone in sequence.\n"
                "Do NOT include any extra prose beyond tool calls or diff blocks.\n"
                "When you deem the task fully complete, call mark_task_complete().\n"
                "NEVER use bash to write large file contents (heredocs, echo >>). For files: call create_file() then apply a diff block for contents.\n"
                "Do NOT include extra prose.\nEND SYSTEM MESSAGE\n"
            )
            send_messages[-1]["content"] = (send_messages[-1].get("content") or "") + tool_directive_text
            markdown_logger.log_tool_availability([t.name for t in tool_defs])
            per_turn_written_text = tool_directive_text
        elif tool_prompt_mode == "system_compiled_and_persistent_per_turn":
            # Add per-turn availability
            compiler = get_compiler()
            enabled_tools = [t.name for t in tool_defs]
            per_turn_availability = compiler.format_per_turn_availability(enabled_tools, active_dialect_names)
            
            # Permanently append to the actual message history
            send_messages[-1]["content"] = (send_messages[-1].get("content") or "") + "\n\n" + per_turn_availability
            session_state.provider_messages[-1]["content"] = (session_state.provider_messages[-1].get("content") or "") + "\n\n" + per_turn_availability
            per_turn_written_text = per_turn_availability

        # Persist per-turn availability content and add transcript section
        try:
            if per_turn_written_text and self.logger_v2.run_dir:
                rel = self.prompt_logger.save_per_turn(turn_index, per_turn_written_text)
                self.logger_v2.append_text("conversation/conversation.md", self.md_writer.tools_available_temp("Per-turn tools prompt appended.", rel))
        except Exception:
            pass
        
        # Add context to transcript  
        session_state.add_transcript_entry({
            "tools_context": {
                "available_tools": _dump_tool_defs(tool_defs),
                "compiled_tools_prompt": local_tools_prompt,
            }
        })
        
        # Determine if native tools should be used
        provider_tools_cfg = getattr(self, "_provider_tools_effective", None) or dict((self.config.get("provider_tools") or {}))
        effective_config = dict(self.config)
        effective_config["provider_tools"] = provider_tools_cfg
        self._provider_tools_effective = provider_tools_cfg
        use_native_tools = provider_router.should_use_native_tools(model, effective_config)
        tools_schema = None
        if use_native_tools and getattr(self, "current_native_tools", None):
            try:
                provider_id = provider_router.parse_model_id(model)[0]
                native_tools = getattr(self, 'current_native_tools', [])
                if native_tools:
                    tools_schema = provider_adapter_manager.translate_tools_to_native_schema(native_tools, provider_id)
                    # Persist provider tools provided
                    try:
                        if self.logger_v2.run_dir and tools_schema:
                            self.provider_logger.save_tools_provided(turn_index, tools_schema)
                            # Also append transcript section listing tool IDs if available
                            ids = []
                            try:
                                # OpenAI style
                                for it in tools_schema:
                                    fn = (it.get("function") or {}).get("name")
                                    if fn:
                                        ids.append(str(fn))
                            except Exception:
                                pass
                            self.logger_v2.append_text("conversation/conversation.md", self.md_writer.provider_tools_provided(ids or ["(see JSON)"], f"provider_native/tools_provided/turn_{turn_index}.json"))
                    except Exception:
                        pass
            except Exception:
                tools_schema = None
        
        effective_stream_responses, stream_policy = self._apply_streaming_policy_for_turn(
            runtime,
            model,
            tools_schema,
            stream_responses,
            session_state,
            markdown_logger,
            turn_index,
        )
        try:
            session_state.set_provider_metadata("current_stream_requested", stream_responses)
            session_state.set_provider_metadata("current_stream_effective", effective_stream_responses)
        except Exception:
            pass

        # Persist raw request (pre-call)
        try:
            if self.logger_v2.include_raw:
                self.api_recorder.save_request(turn_index, {
                    "model": model,
                    "messages": send_messages,
                    "tools": tools_schema,
                    "stream": effective_stream_responses,
                })
        except Exception:
            pass

        # Make API call
        runtime_extra = {
            "turn_index": turn_index,
            "model": model,
            "stream": effective_stream_responses,
            "route_id": self._current_route_id,
        }
        if stream_policy is not None:
            runtime_extra["stream_policy"] = stream_policy

        runtime_context = ProviderRuntimeContext(
            session_state=session_state,
            agent_config=self.config,
            stream=effective_stream_responses,
            extra=runtime_extra,
        )

        # Persist structured request snapshot for incident response triage
        try:
            request_headers = dict(client_config.get("default_headers") or {})
            if getattr(runtime, "descriptor", None) and getattr(runtime.descriptor, "provider_id", None) == "openrouter":
                request_headers.setdefault("Accept", "application/json; charset=utf-8")
                request_headers.setdefault("Accept-Encoding", "identity")
        except Exception:
            request_headers = {}

        try:
            if getattr(self.logger_v2, "include_structured_requests", True):
                extra_meta: Dict[str, Any] = {
                    "message_count": len(send_messages or []),
                    "has_tools": bool(tools_schema),
                }
                if stream_policy:
                    extra_meta["stream_policy"] = {
                        "reason": stream_policy.get("reason"),
                        "stream_effective": stream_policy.get("stream_effective"),
                    }
                self.structured_request_recorder.record_request(
                    turn_index,
                    provider_id=getattr(runtime.descriptor, "provider_id", "unknown"),
                    runtime_id=getattr(runtime.descriptor, "runtime_id", "unknown"),
                    model=model,
                    request_headers=request_headers,
                    request_body={
                        "model": model,
                        "messages": send_messages,
                        "tools": tools_schema,
                        "stream": effective_stream_responses,
                    },
                    stream=effective_stream_responses,
                    tool_count=len(tools_schema or []),
                    endpoint=client_config.get("base_url"),
                    attempt=0,
                    extra=extra_meta,
                )
        except Exception:
            pass

        result, _ = self._invoke_runtime_with_streaming(
            runtime,
            client,
            model,
            send_messages,
            tools_schema,
            effective_stream_responses,
            runtime_context,
            session_state,
            markdown_logger,
        )

        if (self.config.get("features", {}) or {}).get("response_normalizer"):
            try:
                normalized_events = normalize_provider_result(result)
                result.metadata.setdefault("normalized_events", normalized_events)
                session_state.set_provider_metadata("normalized_events", normalized_events)
                if self.logger_v2.run_dir:
                    self.logger_v2.write_json(
                        f"meta/turn_{turn_index}_normalized_events.json",
                        normalized_events,
                    )
            except Exception:
                pass

        usage_raw = result.usage or {}
        normalized_usage: Dict[str, Any] = {}
        if isinstance(usage_raw, dict):
            prompt_tokens = usage_raw.get("prompt_tokens") or usage_raw.get("input_tokens")
            completion_tokens = usage_raw.get("completion_tokens") or usage_raw.get("output_tokens")
            if prompt_tokens is not None:
                normalized_usage["prompt_tokens"] = prompt_tokens
            if completion_tokens is not None:
                normalized_usage["completion_tokens"] = completion_tokens
            if usage_raw.get("cache_read_tokens") is not None:
                normalized_usage["cache_read_tokens"] = usage_raw.get("cache_read_tokens")
            if usage_raw.get("cache_write_tokens") is not None:
                normalized_usage["cache_write_tokens"] = usage_raw.get("cache_write_tokens")
            normalized_usage["model"] = model
        try:
            session_state.set_provider_metadata("usage_normalized", normalized_usage)
        except Exception:
            pass

        cursor_prefix = f"turn_{turn_index}"
        try:
            for msg_idx, prov_msg in enumerate(result.messages or []):
                if prov_msg.content:
                    session_state.add_ir_event(
                        IRDeltaEvent(
                            cursor=f"{cursor_prefix}:text:{msg_idx}",
                            type="text",
                            payload={"role": prov_msg.role, "content": prov_msg.content},
                        )
                    )
                if prov_msg.tool_calls:
                    for tc_idx, tc in enumerate(prov_msg.tool_calls):
                        session_state.add_ir_event(
                            IRDeltaEvent(
                                cursor=f"{cursor_prefix}:tool_call:{msg_idx}:{tc_idx}",
                                type="tool_call",
                                payload={
                                    "id": tc.id,
                                    "name": tc.name,
                                    "arguments": tc.arguments,
                                    "tool_type": tc.type,
                                },
                            )
                        )
        except Exception:
            pass

        try:
            finish_reason = None
            if result.messages:
                finish_reason = result.messages[-1].finish_reason
            session_state.add_ir_event(
                IRDeltaEvent(
                    cursor=f"{cursor_prefix}:finish",
                    type="finish",
                    payload={
                        "finish_reason": finish_reason,
                        "usage": normalized_usage,
                        "metadata": result.metadata,
                    },
                )
            )
        except Exception:
            pass

        # Persist raw response (post-call)
        try:
            if self.logger_v2.include_raw:
                raw_payload = result.raw_response
                if hasattr(raw_payload, "model_dump"):
                    serialized = raw_payload.model_dump()  # type: ignore[attr-defined]
                elif isinstance(raw_payload, dict):
                    serialized = raw_payload
                else:
                    try:
                        serialized = dict(raw_payload)
                    except Exception:
                        serialized = None
                if serialized is not None:
                    self.api_recorder.save_response(turn_index, serialized)
        except Exception:
            pass

        return result
    
    def _legacy_message_view(self, provider_message: ProviderMessage) -> SimpleNamespace:
        """Create a SimpleNamespace compatible with legacy helper functions."""

        tool_calls_ns: List[SimpleNamespace] = []
        for call in provider_message.tool_calls:
            function_ns = SimpleNamespace(
                name=call.name,
                arguments=call.arguments,
            )
            tool_calls_ns.append(
                SimpleNamespace(
                    id=call.id,
                    type=call.type,
                    function=function_ns,
                )
            )
        return SimpleNamespace(
            role=provider_message.role,
            content=provider_message.content,
            tool_calls=tool_calls_ns,
            raw_message=provider_message.raw_message,
            finish_reason=provider_message.finish_reason,
            index=provider_message.index,
        )

    def _log_provider_message(
        self,
        provider_message: ProviderMessage,
        session_state: SessionState,
        markdown_logger: MarkdownLogger,
        stream_responses: bool,
    ) -> None:
        """Log the provider message and capture debug metadata."""

        legacy_msg = self._legacy_message_view(provider_message)

        try:
            debug_tc = None
            if getattr(legacy_msg, "tool_calls", None):
                debug_tc = [
                    {
                        "id": getattr(tc, "id", None),
                        "name": getattr(getattr(tc, "function", None), "name", None),
                    }
                    for tc in legacy_msg.tool_calls
                ]
            session_state.add_transcript_entry(
                {
                    "choice_debug": {
                        "finish_reason": provider_message.finish_reason,
                        "has_content": bool(getattr(legacy_msg, "content", None)),
                        "tool_calls_len": len(legacy_msg.tool_calls)
                        if getattr(legacy_msg, "tool_calls", None)
                        else 0,
                    }
                }
            )
        except Exception:
            pass

        if getattr(legacy_msg, "content", None):
            markdown_logger.log_assistant_message(str(legacy_msg.content))
            try:
                if self.logger_v2.run_dir:
                    self.logger_v2.append_text(
                        "conversation/conversation.md",
                        self.md_writer.assistant(str(legacy_msg.content)),
                    )
            except Exception:
                pass
        
        if stream_responses and getattr(legacy_msg, "content", None):
            try:
                print(str(legacy_msg.content))
            except Exception:
                pass
    
    def _process_model_output(
        self,
        provider_message: ProviderMessage,
        caller,
        tool_defs: List[ToolDefinition],
        session_state: SessionState,
        completion_detector: CompletionDetector,
        markdown_logger: MarkdownLogger,
        error_handler: ErrorHandler,
        stream_responses: bool,
        model: str,
    ) -> bool:
        """Process model output (tool calls or content) and return True if completion detected"""
        msg = self._legacy_message_view(provider_message)

        # Handle text-based tool calls
        if not getattr(msg, "tool_calls", None) and (msg.content or ""):
            return self._handle_text_tool_calls(
                msg, caller, tool_defs, session_state, markdown_logger, 
                error_handler, stream_responses
            )
        
        # Handle native tool calls
        if msg.tool_calls:
            return self._handle_native_tool_calls(
                msg, session_state, markdown_logger, error_handler, 
                stream_responses, model
            )
        
        # Handle regular assistant content (no tool calls)
        if msg.content:
            session_state.add_message({"role": "assistant", "content": msg.content})
            session_state.add_transcript_entry({"assistant": msg.content})
            
            # Check for completion
            completion_analysis = completion_detector.detect_completion(
                msg_content=msg.content or "",
                choice_finish_reason=provider_message.finish_reason,
                tool_results=[],
                agent_config=self.config
            )
            
            session_state.add_transcript_entry({"completion_analysis": completion_analysis})
            
            if completion_analysis["completed"] and completion_detector.meets_threshold(completion_analysis):
                if stream_responses:
                    print(f"[stop] reason={completion_analysis['method']} confidence={completion_analysis['confidence']:.2f} - {completion_analysis['reason']}")
                session_state.add_transcript_entry({
                    "completion_detected": {
                        "method": completion_analysis["method"],
                        "confidence": completion_analysis["confidence"],
                        "reason": completion_analysis["reason"],
                        "content_analyzed": bool(msg.content),
                        "threshold_met": completion_detector.meets_threshold(completion_analysis)
                    }
                })
                if not getattr(session_state, "completion_summary", None):
                    session_state.completion_summary = {
                        "completed": True,
                        "method": completion_analysis["method"],
                        "reason": completion_analysis["reason"],
                        "confidence": completion_analysis["confidence"],
                        "source": "assistant_content",
                        "analysis": completion_analysis,
                    }
                else:
                    session_state.completion_summary.setdefault("completed", True)
                    session_state.completion_summary.setdefault("method", completion_analysis["method"])
                    session_state.completion_summary.setdefault("reason", completion_analysis["reason"])
                    session_state.completion_summary.setdefault("confidence", completion_analysis["confidence"])
                return True
        
        return False
    
    def _handle_text_tool_calls(
        self,
        msg,
        caller,
        tool_defs: List[ToolDefinition],
        session_state: SessionState,
        markdown_logger: MarkdownLogger,
        error_handler: ErrorHandler,
        stream_responses: bool
    ) -> bool:
        """Handle text-based tool calls (simplified version)"""
        parsed = caller.parse_all(msg.content, tool_defs)
        if not parsed:
            return False
        
        # Add assistant message with synthetic tool calls to main messages (for debugging) only
        # Do NOT add synthetic tool calls to provider messages - that would confuse the provider
        synthetic_tool_calls = self.message_formatter.create_synthetic_tool_calls(parsed)
        assistant_entry = {
            "role": "assistant", 
            "content": msg.content,
            "tool_calls": synthetic_tool_calls
        }
        session_state.add_message(assistant_entry, to_provider=False)  # Only to main messages, NOT provider
        
        # Add a simple content-only assistant message to provider messages
        session_state.add_message({"role": "assistant", "content": msg.content}, to_provider=True)
        
        session_state.add_transcript_entry({
            "assistant_with_text_tool_calls": {
                "content": msg.content,
                "parsed_tools_count": len(parsed),
                "parsed_tools": [p.function for p in parsed]
            }
        })
        
        # Execute tool calls
        executed_results, failed_at_index, execution_error = self.agent_executor.execute_parsed_calls(
            parsed, 
            self._exec_raw,
            transcript_callback=session_state.add_transcript_entry
        )
        
        # Handle execution errors
        if execution_error:
            if execution_error.get("validation_failed"):
                error_msg = error_handler.handle_validation_error(execution_error)
            elif execution_error.get("constraint_violation"):
                error_msg = error_handler.handle_constraint_violation(execution_error["error"])
            else:
                error_msg = f"<EXECUTION_ERROR>\n{execution_error['error']}\n</EXECUTION_ERROR>"
            
            session_state.add_message({"role": "user", "content": error_msg}, to_provider=True)
            markdown_logger.log_user_message(error_msg)
            
            if stream_responses:
                print(f"[error] {execution_error.get('error', 'Unknown error')}")
            return False
        
        # Check for completion via tool results
        for tool_parsed, tool_result in executed_results:
            if isinstance(tool_result, dict) and tool_result.get("action") == "complete":
                # Format and show all executed results including the completion
                chunks = self.message_formatter.format_execution_results(executed_results, failed_at_index, len(parsed))
                provider_tool_msg = "\n\n".join(chunks)
                session_state.add_message({"role": "user", "content": provider_tool_msg}, to_provider=True)
                markdown_logger.log_user_message(provider_tool_msg)
                
                if not getattr(session_state, "completion_summary", None):
                    session_state.completion_summary = {
                        "completed": True,
                        "method": "tool_mark_task_complete",
                        "reason": "mark_task_complete",
                        "confidence": 1.0,
                        "tool": tool_parsed.function,
                        "tool_result": tool_result,
                        "source": "tool_call",
                    }
                else:
                    session_state.completion_summary.setdefault("completed", True)
                    session_state.completion_summary.setdefault("reason", "mark_task_complete")
                    session_state.completion_summary.setdefault("method", "tool_mark_task_complete")

                if stream_responses:
                    print(f"[stop] reason=tool_based confidence=1.0 - mark_task_complete() called")
                return True
        
        # Persist per-tool results as artifacts and collect links
        artifact_links: list[str] = []
        try:
            if self.logger_v2.run_dir:
                for idx, (tool_parsed, tool_result) in enumerate(executed_results):
                    rel = self.message_formatter.write_tool_result_file(self.logger_v2.run_dir, len(session_state.transcript) + 1, idx, tool_parsed.function, tool_result)
                    if rel:
                        artifact_links.append(rel)
        except Exception:
            pass

        # Format and relay results
        chunks = self.message_formatter.format_execution_results(executed_results, failed_at_index, len(parsed))
        
        # Add tool result messages to main messages array
        for tool_parsed, tool_result in executed_results:
            tool_result_entry = self.message_formatter.create_tool_result_entry(
                tool_parsed.function, tool_result, syntax_type="custom-pythonic"
            )
            session_state.add_message(tool_result_entry, to_provider=False)
        
        # Get turn strategy flow configuration
        turn_cfg = self.config.get("turn_strategy", {})
        flow_strategy = turn_cfg.get("flow", "assistant_continuation").lower()
        
        if flow_strategy == "assistant_continuation":
            # ASSISTANT CONTINUATION PATTERN: Create assistant message with tool results
            provider_tool_msg = "\n\n".join(chunks)
            assistant_continuation = {
                "role": "assistant",
                "content": f"\n\nTool execution results:\n{provider_tool_msg}"
            }
            session_state.add_message(assistant_continuation)
            markdown_logger.log_assistant_message(assistant_continuation["content"])
            try:
                if self.logger_v2.run_dir:
                    # First, log the assistant continuation text to conversation
                    self.logger_v2.append_text("conversation/conversation.md", self.md_writer.assistant(assistant_continuation["content"]))
                    # Then, add a compact artifact links section once per turn
                    if artifact_links:
                        self.logger_v2.append_text("conversation/conversation.md", self.md_writer.text_tool_results("Tool execution results appended.", artifact_links))
            except Exception:
                pass
        else:
            # USER INTERLEAVED PATTERN: Traditional user message relay
            provider_tool_msg = "\n\n".join(chunks)
            session_state.add_message({"role": "user", "content": provider_tool_msg}, to_provider=True)
            markdown_logger.log_user_message(provider_tool_msg)
            try:
                if self.logger_v2.run_dir:
                    # In user-interleaved, still present a single compact artifact links section
                    if artifact_links:
                        self.logger_v2.append_text("conversation/conversation.md", self.md_writer.text_tool_results("Tool execution results appended.", artifact_links))
            except Exception:
                pass
        
        return False
    
    def _handle_native_tool_calls(
        self,
        msg,
        session_state: SessionState,
        markdown_logger: MarkdownLogger,
        error_handler: ErrorHandler,
        stream_responses: bool,
        model: str
    ) -> bool:
        """Handle native tool calls with proper provider tool result formatting"""
        # Execute provider-native tool calls and relay results using provider-supported 'tool' role
        turn_cfg = self.config.get("turn_strategy", {})
        relay_strategy = (turn_cfg.get("relay") or "tool_role").lower()
        
        tool_messages_to_relay: List[Dict[str, Any]] = []
        
        try:
            # 1) Append the assistant message that contains tool_calls to maintain linkage
            try:
                tool_calls_payload = []
                for tc in msg.tool_calls:
                    fn_name = getattr(getattr(tc, "function", None), "name", None)
                    arg_str = getattr(getattr(tc, "function", None), "arguments", "{}")
                    tool_calls_payload.append({
                        "id": getattr(tc, "id", None),
                        "type": "function",
                        "function": {"name": fn_name, "arguments": arg_str if isinstance(arg_str, str) else json.dumps(arg_str or {})},
                    })
                # Persist provider-native tool calls and add transcript section
                try:
                    if self.logger_v2.run_dir and tool_calls_payload:
                        turn_index = len(session_state.transcript) + 1
                        self.provider_logger.save_tool_calls(turn_index, tool_calls_payload)
                        short = "\n".join([f"- {c['function']['name']} (id={c.get('id')})" for c in tool_calls_payload])
                        self.logger_v2.append_text("conversation/conversation.md", self.md_writer.provider_tool_calls(short, f"provider_native/tool_calls/turn_{turn_index}.json"))
                except Exception:
                    pass
                
                # CRITICAL: Add assistant message with tool calls to BOTH arrays
                # Enhanced assistant entry with syntax type metadata
                enhanced_tool_calls = self.message_formatter.create_enhanced_tool_calls(tool_calls_payload)
                
                assistant_entry = {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": enhanced_tool_calls,
                }
                session_state.add_message(assistant_entry, to_provider=False)  # For complete session history in JSON output
                session_state.add_message({"role": "assistant", "content": msg.content, "tool_calls": tool_calls_payload}, to_provider=True)  # For provider (no enhanced fields)
                
                # Add to transcript for debugging
                session_state.add_transcript_entry({
                    "assistant_with_tool_calls": {
                        "content": msg.content,
                        "tool_calls_count": len(msg.tool_calls),
                        "tool_calls": [tc["function"]["name"] for tc in tool_calls_payload]
                    }
                })
            except Exception:
                pass
            
            # 2) Execute calls via shared agent executor to honor concurrency + alias policies
            parsed_calls: List[Any] = []
            for tc in msg.tool_calls:
                fn = getattr(getattr(tc, "function", None), "name", None)
                call_id = getattr(tc, "id", None)
                arg_str = getattr(getattr(tc, "function", None), "arguments", "{}")
                try:
                    args = json.loads(arg_str) if isinstance(arg_str, str) else (arg_str or {})
                except Exception:
                    args = {}
                if not fn:
                    continue
                canonical_fn = self.agent_executor.canonical_tool_name(fn)
                call_obj = SimpleNamespace(function=canonical_fn, arguments=args, provider_name=fn, call_id=call_id)
                parsed_calls.append(call_obj)

            executed_results: List[tuple] = []
            failed_at_index = -1
            execution_error: Optional[Dict[str, Any]] = None

            if parsed_calls:
                executed_results, failed_at_index, execution_error = self.agent_executor.execute_parsed_calls(
                    parsed_calls,
                    self._exec_raw,
                    transcript_callback=session_state.add_transcript_entry,
                )

            if execution_error:
                if execution_error.get("validation_failed"):
                    error_msg = error_handler.handle_validation_error(execution_error)
                elif execution_error.get("constraint_violation"):
                    error_msg = error_handler.handle_constraint_violation(execution_error["error"])
                else:
                    error_msg = f"<EXECUTION_ERROR>\n{execution_error['error']}\n</EXECUTION_ERROR>"

                session_state.add_message({"role": "user", "content": error_msg}, to_provider=True)
                markdown_logger.log_user_message(error_msg)
                if stream_responses:
                    print(f"[error] {execution_error.get('error', 'Unknown error')}")
                return False

            results: List[Dict[str, Any]] = []
            for parsed, tool_result in executed_results:
                call_id = getattr(parsed, "call_id", None)
                provider_name = getattr(parsed, "provider_name", parsed.function)
                results.append({
                    "fn": parsed.function,
                    "provider_fn": provider_name,
                    "out": tool_result,
                    "args": parsed.arguments,
                    "call_id": call_id,
                    "failed": False,
                })
            
            # Get turn strategy flow configuration
            flow_strategy = turn_cfg.get("flow", "assistant_continuation").lower()
            
            # Persist provider-native tool results before relay
            try:
                if self.logger_v2.run_dir and results:
                    turn_index = len(session_state.transcript) + 1
                    persistable = []
                    for r in results:
                        persistable.append({
                            "fn": r["fn"],
                            "provider_fn": r.get("provider_fn", r["fn"]),
                            "call_id": r.get("call_id"),
                            "args": r.get("args"),
                            "out": r.get("out"),
                        })
                    self.provider_logger.save_tool_results(turn_index, persistable)
                    short = "\n".join([f"- {r.get('provider_fn', r['fn'])} (id={r.get('call_id')})" for r in results])
                    self.logger_v2.append_text("conversation/conversation.md", self.md_writer.provider_tool_results(short, f"provider_native/tool_results/turn_{turn_index}.json"))
            except Exception:
                pass

            # Relay results based on flow strategy
            if flow_strategy == "assistant_continuation":
                # ASSISTANT CONTINUATION PATTERN: Append tool results to assistant messages
                all_results_text = []
                for r in results:
                    formatted_output = self.message_formatter.format_tool_output(r["fn"], r["out"], r["args"])
                    call_id = r.get("call_id")
                    
                    # Always add tool result to main messages array for debugging
                    tool_result_entry = self.message_formatter.create_tool_result_entry(
                        r["fn"], r["out"], call_id or f"native_call_{r['fn']}", "openai"
                    )
                    session_state.add_message(tool_result_entry, to_provider=False)
                    all_results_text.append(formatted_output)
                
                # Create assistant continuation message with tool results
                continuation_content = f"\n\nTool execution results:\n" + "\n\n".join(all_results_text)
                assistant_continuation = {
                    "role": "assistant", 
                    "content": continuation_content
                }
                session_state.add_message(assistant_continuation)
                markdown_logger.log_assistant_message(continuation_content)
            else:
                # USER INTERLEAVED PATTERN: Traditional relay via user/tool messages
                for r in results:
                    formatted_output = self.message_formatter.format_tool_output(r["fn"], r["out"], r["args"])
                    call_id = r.get("call_id")
                    
                    # Always add tool result to main messages array for debugging
                    tool_result_entry = self.message_formatter.create_tool_result_entry(
                        r["fn"], r["out"], call_id or f"native_call_{r['fn']}", "openai"
                    )
                    session_state.add_message(tool_result_entry, to_provider=False)
                    
                    if relay_strategy == "tool_role" and call_id:
                        # Use provider adapter to create proper tool result message
                        provider_id = provider_router.parse_model_id(model)[0]
                        adapter = provider_adapter_manager.get_adapter(provider_id)
                        tool_result_msg = adapter.create_tool_result_message(call_id, r.get("provider_fn", r["fn"]), r["out"])
                        tool_messages_to_relay.append(tool_result_msg)
                    else:
                        session_state.add_message({"role": "user", "content": formatted_output}, to_provider=True)
                
                # If using tool-role relay, append all tool messages and continue loop
                if tool_messages_to_relay:
                    session_state.provider_messages.extend(tool_messages_to_relay)
                    # Do not add any synthetic user messages; let the model continue the assistant turn
            
            return False  # Continue the loop
            
        except Exception:
            # On relay error, fall back to legacy behavior (append as user)
            try:
                if tool_messages_to_relay:
                    fallback_blob = "\n\n".join([m.get("content", "") for m in tool_messages_to_relay])
                    session_state.add_message({"role": "user", "content": fallback_blob}, to_provider=True)
            except Exception:
                pass
            return False
