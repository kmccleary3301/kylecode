"""
Enhanced System Prompt Compiler for Tool Calling
Generates and caches comprehensive system prompts implementing design decisions from 08-13-25 report.

Key features:
- Format preference based on research (Aider > OpenCode > Unified)
- Sequential execution patterns
- Assistant message continuation guidance
- Tool blocking constraints
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import List, Dict, Any, Set, Optional

from ..core.core import ToolDefinition
from ..dialects.pythonic02 import Pythonic02Dialect
from ..dialects.pythonic_inline import PythonicInlineDialect
from ..dialects.bash_block import BashBlockDialect
from ..dialects.aider_diff import AiderDiffDialect
from ..dialects.unified_diff import UnifiedDiffDialect
from ..dialects.opencode_patch import OpenCodePatchDialect
from .tool_prompt_synth import ToolPromptSynthesisEngine


class SystemPromptCompiler:
    """Compiles and caches comprehensive system prompts for tool calling"""
    
    def __init__(self, cache_dir: str = "implementations/tooling_sys_prompts_cached"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # All available dialects
        self.all_dialects = [
            Pythonic02Dialect(),
            PythonicInlineDialect(),
            BashBlockDialect(),
            AiderDiffDialect(),
            UnifiedDiffDialect(),
            OpenCodePatchDialect(),
        ]
        
        # Tool format categories for the per-turn availability list
        # Ordered by research-based preference (Aider > OpenCode > Unified)
        self.format_categories = {
            "python": "python inside <TOOL_CALL> XML",
            "bash": "bash command inside of <BASH> XML", 
            "aider": "Aider SEARCH/REPLACE Format (PREFERRED - 2.3x success rate)",
            "opencode": "OpenCode Add File format (Good structured alternative)",
            "unified_diff": "Unified Patch Git-like format (Use only if Aider unavailable)"
        }
        
        # Research-based format preferences
        self.format_preferences = [
            "aider_diff",      # Highest success rate (59% vs 26%)
            "opencode_patch",  # Structured but more complex
            "unified_diff"     # Lowest success rate for smaller models
        ]
        
        self.tpsl = ToolPromptSynthesisEngine()

    def _compute_tools_hash(self, tools: List[ToolDefinition], dialects: List[str], primary_prompt: str = "", tool_prompt_mode: str = "system_once") -> str:
        """Compute hash of tools and dialects for caching"""
        # Create normalized representation
        tools_data = []
        for tool in sorted(tools, key=lambda t: t.name):
            tool_data = {
                "name": tool.name,
                "description": tool.description,
                "parameters": [
                    {
                        "name": p.name,
                        "type": p.type,
                        "description": p.description,
                        "default": p.default
                    }
                    for p in sorted(tool.parameters or [], key=lambda p: p.name)
                ],
                "blocking": getattr(tool, 'blocking', False)
            }
            tools_data.append(tool_data)
        
        hash_input = {
            "tools": tools_data,
            "dialects": sorted(dialects),
            "primary_prompt": primary_prompt.strip(),
            "tool_prompt_mode": tool_prompt_mode
        }
        
        hash_str = json.dumps(hash_input, sort_keys=True)
        return hashlib.sha256(hash_str.encode()).hexdigest()[:12]
    
    def _get_next_id(self) -> int:
        """Get next sequential ID for cached prompts"""
        existing_files = list(self.cache_dir.glob("sys_prompt_*.md"))
        if not existing_files:
            return 1
        
        max_id = 0
        for file_path in existing_files:
            try:
                # Extract ID from filename like "sys_prompt_003_a8bd45c7f0.md"
                parts = file_path.stem.split('_')
                if len(parts) >= 3:
                    id_part = parts[2]
                    max_id = max(max_id, int(id_part))
            except (ValueError, IndexError):
                continue
        
        return max_id + 1
    
    def _find_cached_prompt(self, tools_hash: str) -> Optional[Path]:
        """Find existing cached prompt with matching hash"""
        pattern = f"sys_prompt_*_{tools_hash}.md"
        matches = list(self.cache_dir.glob(pattern))
        return matches[0] if matches else None
    
    def _generate_minimal_system_prompt(self, primary_prompt: str, tool_prompt_mode: str) -> str:
        """
        Generate minimal system prompt for per_turn_append mode.
        Based on OpenCode/Crush insights: focus on behavior, not tool definitions.
        """
        prompt_parts = []
        
        # Primary system prompt first (if provided)
        if primary_prompt.strip():
            prompt_parts.append(primary_prompt.strip())
            prompt_parts.append("")
        
        # Based on Crush's approach: concise behavior-focused instructions
        prompt_parts.extend([
            "# ENHANCED TOOL USAGE SYSTEM",
            "",
            "You have access to tools that will be specified in each user message. Focus on:",
            "",
            "## CORE PRINCIPLES (From Production Research)",
            "- **Conciseness**: Answer in fewer than 4 lines unless detail requested",
            "- **Direct execution**: Use tools immediately without excessive explanation", 
            "- **Format preference**: Use Aider SEARCH/REPLACE when available (2.3x success rate)",
            "- **Sequential execution**: Only ONE bash command per turn",
            "- **Proactive completion**: Mark tasks complete when finished",
            "",
            "## EXECUTION PATTERN",
            "1. **Brief plan**: State what you'll do in 1-2 lines",
            "2. **Execute tools**: Use the most appropriate format available", 
            "3. **Concise summary**: Briefly confirm completion",
            "",
            "## TOOL SELECTION PRIORITY (Research-based)",
            "1. **Aider SEARCH/REPLACE** - Highest success rate, use for file modifications",
            "2. **OpenCode formats** - Good for structured operations",
            "3. **Unified diff** - Last resort, higher error rate",
            "",
            "The specific tools and formats available will be listed in each user message.",
        ])
        
        return "\n".join(prompt_parts)
    
    def _generate_comprehensive_prompt(self, tools: List[ToolDefinition], dialects: List[str], primary_prompt: str = "", tool_prompt_mode: str = "system_once") -> str:
        """Generate comprehensive system prompt with all tool formats"""
        
        # Filter dialects to only those requested
        active_dialects = [d for d in self.all_dialects if d.type_id in dialects]
        
        prompt_parts = []
        
        # Primary system prompt first (if provided)
        if primary_prompt.strip():
            prompt_parts.append(primary_prompt.strip())
            prompt_parts.append("")
            prompt_parts.append("")
        
        # Header
        prompt_parts.append("# TOOL CALLING SYSTEM")
        prompt_parts.append("")
        prompt_parts.append("You have access to multiple tool calling formats. The specific tools available for each turn will be indicated in the user message.")
        prompt_parts.append("")
        
        # Comprehensive tool descriptions
        prompt_parts.append("## AVAILABLE TOOL FORMATS")
        prompt_parts.append("")
        
        # Add each dialect's prompt
        for dialect in active_dialects:
            dialect_prompt = dialect.prompt_for_tools(tools)
            if dialect_prompt.strip():
                prompt_parts.append(dialect_prompt)
                prompt_parts.append("")
        
        # Tool-specific documentation
        prompt_parts.append("## TOOL FUNCTIONS")
        prompt_parts.append("")
        prompt_parts.append("The following functions may be available (availability specified per turn):")
        prompt_parts.append("")
        
        for tool in tools:
            prompt_parts.append(f"**{tool.name}**")
            prompt_parts.append(f"- Description: {tool.description}")
            if tool.parameters:
                prompt_parts.append("- Parameters:")
                for param in tool.parameters:
                    default_str = f" (default: {param.default})" if param.default is not None else ""
                    desc_str = f" - {param.description}" if param.description else ""
                    prompt_parts.append(f"  - {param.name} ({param.type}){default_str}{desc_str}")
            if getattr(tool, 'blocking', False):
                prompt_parts.append("- **Blocking**: This tool must execute alone and blocks other tools")
            prompt_parts.append("")
        
        # Enhanced usage guidelines based on research findings
        prompt_parts.append("## ENHANCED USAGE GUIDELINES")
        prompt_parts.append("*Based on 2024-2025 research findings*")
        prompt_parts.append("")
        
        # Format preferences from research
        prompt_parts.append("### FORMAT PREFERENCES (Research-Based)")
        prompt_parts.append("1. **PREFERRED: Aider SEARCH/REPLACE** - 2.3x higher success rate (59% vs 26%)")
        prompt_parts.append("   - Use for all file modifications when possible")
        prompt_parts.append("   - Exact text matching reduces errors")
        prompt_parts.append("   - Simple syntax, high reliability")
        prompt_parts.append("")
        prompt_parts.append("2. **GOOD: OpenCode Patch Format** - Structured alternative")
        prompt_parts.append("   - Use for complex multi-file operations")
        prompt_parts.append("   - Good for adding new files")
        prompt_parts.append("")
        prompt_parts.append("3. **LAST RESORT: Unified Diff** - Lowest success rate for small models")
        prompt_parts.append("   - Use only when other formats unavailable")
        prompt_parts.append("   - Higher complexity, more error-prone")
        prompt_parts.append("")
        
        # Sequential execution guidance
        prompt_parts.append("### EXECUTION CONSTRAINTS (Critical)")
        prompt_parts.append("- **BASH CONSTRAINT**: Only ONE bash command per turn allowed")
        prompt_parts.append("- **BLOCKING TOOLS**: Some tools must execute alone (marked as blocking)")
        prompt_parts.append("- **SEQUENTIAL EXECUTION**: Tools execute in order, blocking tools pause execution")
        prompt_parts.append("- **DEPENDENCY AWARENESS**: Some tools require others to run first")
        prompt_parts.append("")
        
        # Message continuation pattern
        prompt_parts.append("### RESPONSE PATTERN")
        prompt_parts.append("- Provide initial explanation of what you will do")
        prompt_parts.append("- Execute tools in logical order")
        prompt_parts.append("- Provide final summary after all tools complete")
        prompt_parts.append("- Do NOT create separate user messages for tool results")
        prompt_parts.append("- Maintain conversation flow with assistant message continuation")
        prompt_parts.append("")
        
        # Dynamic completion section based on available tools
        completion_tools = [t for t in tools if "complete" in t.name.lower() or "finish" in t.name.lower()]
        if completion_tools:
            prompt_parts.append("### COMPLETION")
            for tool in completion_tools:
                prompt_parts.append(f"- When task is complete, call {tool.name}()")
                if tool.description:
                    prompt_parts.append(f"  {tool.description}")
            prompt_parts.append("")
        prompt_parts.append("The specific tools available for this turn will be listed in the user message under <TOOLS_AVAILABLE>.")
        
        return "\n".join(prompt_parts)
    
    def get_preferred_formats(self, available_dialects: List[str]) -> List[str]:
        """
        Get preferred format order based on research findings.
        
        Research shows Aider SEARCH/REPLACE has 2.3x success rate advantage.
        """
        # Map dialect names to preference order
        preference_map = {
            "aider_diff": 1,
            "opencode_patch": 2, 
            "unified_diff": 3,
            "pythonic02": 4,
            "bash_block": 5,
            "pythonic_inline": 6
        }
        
        # Sort available dialects by preference
        sorted_dialects = sorted(
            available_dialects,
            key=lambda d: preference_map.get(d, 999)
        )
        
        return sorted_dialects
    
    def format_per_turn_availability_enhanced(self, enabled_tools: List[str], preferred_formats: List[str]) -> str:
        """
        Enhanced per-turn availability with format preferences.
        """
        lines = ["<TOOLS_AVAILABLE>"]
        lines.append("")
        
        # Show preferred format first
        if preferred_formats:
            primary_format = preferred_formats[0]
            format_name = self.format_categories.get(primary_format.replace("_diff", "").replace("_patch", ""), primary_format)
            lines.append(f"**PRIMARY FORMAT (Recommended)**: {format_name}")
            lines.append("")
        
        lines.append("**Available Tools for this turn:**")
        for tool in enabled_tools:
            lines.append(f"- {tool}")
        lines.append("")
        
        lines.append("**Available Formats (in preference order):**")
        for fmt in preferred_formats:
            format_name = self.format_categories.get(fmt.replace("_diff", "").replace("_patch", ""), fmt)
            lines.append(f"- {format_name}")
        lines.append("")
        
        lines.append("</TOOLS_AVAILABLE>")
        return "\n".join(lines)
    
    def _create_metadata_header(self, tools: List[ToolDefinition], dialects: List[str], 
                               tools_hash: str, prompt_id: int) -> str:
        """Create metadata header for cached prompt file"""
        tool_names = [t.name for t in tools]
        
        metadata = {
            "prompt_id": prompt_id,
            "tools_hash": tools_hash,
            "tools": tool_names,
            "dialects": dialects,
            "version": "1.0",
            "auto_generated": True
        }
        
        header_lines = [
            "<!--",
            "METADATA (DO NOT INCLUDE IN PROMPT):",
            json.dumps(metadata, indent=2),
            "-->",
            ""
        ]
        
        return "\n".join(header_lines)
    
    def get_or_create_system_prompt(self, tools: List[ToolDefinition], dialects: List[str], primary_prompt: str = "", tool_prompt_mode: str = "system_once") -> tuple[str, str]:
        """
        Get cached system prompt or create new one
        
        Returns:
            tuple: (system_prompt_content, tools_hash)
        """
        tools_hash = self._compute_tools_hash(tools, dialects, primary_prompt, tool_prompt_mode)
        
        # For per_turn_append mode, return minimal system prompt (tools will be appended per turn)
        if tool_prompt_mode == "per_turn_append":
            # Generate minimal system prompt focused on behavior, not tool definitions
            minimal_prompt = self._generate_minimal_system_prompt(primary_prompt, tool_prompt_mode)
            return minimal_prompt, tools_hash
        
        # Check for existing cached prompt
        cached_file = self._find_cached_prompt(tools_hash)
        if cached_file and cached_file.exists():
            content = cached_file.read_text(encoding='utf-8')
            # Extract content after metadata header
            if content.startswith("<!--"):
                # Find end of metadata header
                end_marker = "-->"
                end_pos = content.find(end_marker)
                if end_pos != -1:
                    content = content[end_pos + len(end_marker):].strip()
            return content, tools_hash
        
        # Generate new prompt
        prompt_content = self._generate_comprehensive_prompt(tools, dialects, primary_prompt, tool_prompt_mode)
        
        # Save to cache with metadata
        prompt_id = self._get_next_id()
        filename = f"sys_prompt_{prompt_id:03d}_{tools_hash}.md"
        cache_file = self.cache_dir / filename
        
        # Create file with metadata header
        metadata_header = self._create_metadata_header(tools, dialects, tools_hash, prompt_id)
        full_content = metadata_header + "\n" + prompt_content
        
        cache_file.write_text(full_content, encoding='utf-8')
        
        return prompt_content, tools_hash

    # ===== Agent Schema v2 support =====
    def _read_text_if_exists(self, path: Optional[str]) -> str:
        if not path:
            return ""
        try:
            p = Path(path)
            if p.exists():
                return p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
        return ""

    def _resolve_pack_ref(self, packs: Dict[str, Dict[str, str]], ref: str, mode_name: Optional[str]) -> str:
        """
        Resolve a reference like "@pack(base).system" or mode-specific key.
        """
        if not ref:
            return ""
        if ref.startswith("@pack(") and ")" in ref and "." in ref:
            try:
                pack_name = ref.split("@pack(", 1)[1].split(")", 1)[0].strip()
                key = ref.split(")", 1)[1].lstrip(".")
                pack = packs.get(pack_name) or {}
                return self._read_text_if_exists(pack.get(key))
            except Exception:
                return ""
        if ref == "mode_specific" and mode_name:
            # Try keys matching mode_name or conventional alias mapping (build -> builder)
            alias_key = mode_name
            if mode_name == "build":
                alias_key = "builder"
            for pack in packs.values():
                if alias_key in pack:
                    return self._read_text_if_exists(pack.get(alias_key))
        return ""

    def _compute_v2_cache_key(self, config: Dict[str, Any], packs_text: List[str], tools: List[ToolDefinition]) -> str:
        """
        sha256(config+prompts+toolset) short key.
        """
        import hashlib, json
        cfg_min = {k: config.get(k) for k in ("version","providers","tools","prompts","modes","loop")}
        tools_min = [
            {
                "name": t.name,
                "desc": t.description,
                "params": [{"n": p.name, "t": p.type, "d": p.description, "def": p.default} for p in (t.parameters or [])]
            }
            for t in sorted(tools, key=lambda x: x.name)
        ]
        blob = json.dumps({"config": cfg_min, "packs": packs_text, "tools": tools_min}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def compile_v2_prompts(self, config: Dict[str, Any], mode_name: Optional[str], tools: List[ToolDefinition], dialects: List[str]) -> Dict[str, str]:
        """
        Compile system + per-turn prompts for Agent Schema v2 using packs and injection orders.
        Returns {"system": str, "per_turn": str, "cache_key": str}.
        """
        prompts_cfg = (config.get("prompts") or {})
        packs = prompts_cfg.get("packs") or {}
        injection = prompts_cfg.get("injection") or {}
        system_order = injection.get("system_order") or []
        per_turn_order = injection.get("per_turn_order") or []

        # Normalize packs to dict[str, dict[str,str]] of file paths
        packs_norm: Dict[str, Dict[str, str]] = {}
        for pack_name, pack_val in packs.items():
            if isinstance(pack_val, dict):
                packs_norm[pack_name] = {k: str(v) for k, v in pack_val.items()}

        # Build system prompt by concatenating referenced pack contents
        system_chunks: List[str] = []
        pack_texts_for_key: List[str] = []
        # Track seen content to avoid duplicates across system and per-turn
        import hashlib as _hashlib
        def _h(txt: str) -> str:
            return _hashlib.sha1((txt or "").strip().encode()).hexdigest()
        seen_text_hashes: Set[str] = set()
        for item in system_order:
            is_cached = False
            ref = item
            if isinstance(item, str) and item.startswith("[CACHE]"):
                is_cached = True
                ref = item.split("]", 1)[1].strip()
            text = self._resolve_pack_ref(packs_norm, ref, mode_name)
            if text:
                h = _h(text)
                if h not in seen_text_hashes:
                    system_chunks.append(text.rstrip())
                    pack_texts_for_key.append(text)
                    seen_text_hashes.add(h)

        # Sensible default: if no injection order provided and a pack has a 'system' key,
        # include the first available one (prefer pack named 'base' when present).
        if not system_chunks and packs_norm:
            # Prefer 'base.system' if available
            preferred_keys = []
            if "base" in packs_norm and packs_norm["base"].get("system"):
                preferred_keys.append(("base", packs_norm["base"]["system"]))
            # Fallback: any pack with 'system'
            for pname, pval in packs_norm.items():
                if pval.get("system"):
                    preferred_keys.append((pname, pval["system"]))
            for _, path in preferred_keys:
                text = self._read_text_if_exists(path)
                if text.strip():
                    h = _h(text)
                    if h not in seen_text_hashes:
                        system_chunks.append(text.rstrip())
                        pack_texts_for_key.append(text)
                        seen_text_hashes.add(h)
                        break

        # TPSL system catalog
        tpsl_meta: Dict[str, Any] = {}
        tpsl_cfg = prompts_cfg.get("tool_prompt_synthesis") or {}
        tpsl_enabled = bool(tpsl_cfg.get("enabled", False))
        if tpsl_enabled and tools:
            # Optional: set an alternate templates root from config
            tpsl_root = tpsl_cfg.get("root")
            if tpsl_root:
                try:
                    self.tpsl.set_root(tpsl_root)
                except Exception:
                    pass
            selection = tpsl_cfg.get("selection", {})
            by_mode = selection.get("by_mode", {})
            tpsl_dialect = by_mode.get(mode_name or "") or (dialects[0] if dialects else "pythonic")
            # Map tools to simple dicts
            tools_payload: List[Dict[str, Any]] = []
            for t in tools:
                params = [{"name": p.name, "type": p.type, "default": p.default, "description": p.description} for p in (t.parameters or [])]
                tools_payload.append({
                    "name": t.name,
                    "display_name": t.name,
                    "description": t.description,
                    "blocking": getattr(t, "blocking", False),
                    "parameters": params,
                })
            templates = (tpsl_cfg.get("dialects", {}) or {}).get(tpsl_dialect, {})
            detail = (tpsl_cfg.get("detail", {}) or {}).get("system", "full")
            catalog_text, template_id = self.tpsl.render(tpsl_dialect, detail, tools_payload, templates)
            if catalog_text.strip():
                system_chunks.append(catalog_text.rstrip())
                pack_texts_for_key.append(catalog_text)
                tpsl_meta["system"] = {
                    "dialect": tpsl_dialect,
                    "detail": detail,
                    "template_id": template_id,
                    "text": catalog_text,
                }
                # Ensure per-turn stage does not re-include the same chunk
                try:
                    seen_text_hashes.add(_h(catalog_text))
                except Exception:
                    pass
        elif dialects and not system_chunks:
            comp, _ = self.get_or_create_system_prompt(tools, dialects, "", "system_once")
            system_chunks.append(comp)

        system_prompt = ("\n\n".join([c for c in system_chunks if c])).strip()

        # Build per-turn prompt
        per_turn_chunks: List[str] = []
        for item in per_turn_order:
            text = self._resolve_pack_ref(packs_norm, item, mode_name)
            if text:
                h = _h(text)
                if h not in seen_text_hashes:
                    per_turn_chunks.append(text.rstrip())
                    seen_text_hashes.add(h)

        # Sensible default: if no per_turn_order provided but the active mode has a 'prompt'
        # reference, include it here (supports @pack(...).key style).
        if not per_turn_chunks and mode_name:
            try:
                modes = (config.get("modes") or [])
                for m in modes:
                    if m.get("name") == mode_name and m.get("prompt"):
                        ref = str(m.get("prompt"))
                        text = self._resolve_pack_ref(packs_norm, ref, mode_name)
                        if text:
                            per_turn_chunks.append(text.rstrip())
                        break
            except Exception:
                pass
        # TPSL per-turn short catalog
        if tpsl_enabled and tools:
            tpsl_dialect = (tpsl_cfg.get("selection", {}).get("by_mode", {}).get(mode_name or "")) or (dialects[0] if dialects else "pythonic")
            tools_payload: List[Dict[str, Any]] = []
            for t in tools:
                params = [{"name": p.name, "type": p.type, "default": p.default, "description": p.description} for p in (t.parameters or [])]
                tools_payload.append({
                    "name": t.name,
                    "display_name": t.name,
                    "description": t.description,
                    "blocking": getattr(t, "blocking", False),
                    "parameters": params,
                })
            templates = (tpsl_cfg.get("dialects", {}) or {}).get(tpsl_dialect, {})
            detail = (tpsl_cfg.get("detail", {}) or {}).get("per_turn", "short")
            short_text, template_id_pt = self.tpsl.render(tpsl_dialect, detail, tools_payload, templates)
            if short_text.strip():
                hpt = _h(short_text)
                if hpt not in seen_text_hashes:
                    per_turn_chunks.append(short_text.rstrip())
                    seen_text_hashes.add(hpt)
                tpsl_meta["per_turn"] = {
                    "dialect": tpsl_dialect,
                    "detail": detail,
                    "template_id": template_id_pt,
                    "text": short_text,
                }
        elif tools or dialects:
            try:
                per_turn_chunks.append(self.format_per_turn_availability([t.name for t in tools], dialects))
            except Exception:
                pass
        per_turn_prompt = ("\n\n".join([c for c in per_turn_chunks if c])).strip()

        cache_key = self._compute_v2_cache_key(config, pack_texts_for_key, tools)
        out = {"system": system_prompt, "per_turn": per_turn_prompt, "cache_key": cache_key}
        if tpsl_meta:
            out["tpsl"] = tpsl_meta
        return out
    
    def format_per_turn_availability(self, enabled_tools: List[str], enabled_dialects: List[str]) -> str:
        """Format small per-turn tool availability list"""
        
        lines = ["<TOOLS_AVAILABLE>"]
        
        # Group tools by format type
        tool_formats = {
            "python_tools": [],
            "bash_available": False,
            "formats_available": []
        }
        
        # Standard python tools
        python_tools = [
            "run_shell", "create_file", "read_file", "list_dir", 
            "mark_task_complete", "apply_unified_patch", "create_file_from_block"
        ]
        
        for tool in enabled_tools:
            if tool in python_tools:
                tool_formats["python_tools"].append(tool)
        
        # Check for bash block support
        if "bash_block" in enabled_dialects:
            tool_formats["bash_available"] = True
        
        # Check for diff formats
        diff_formats = []
        if "aider_diff" in enabled_dialects:
            diff_formats.append("Aider SEARCH/REPLACE")
        if "unified_diff" in enabled_dialects:
            diff_formats.append("Unified Diff Git-like")
        if "opencode_patch" in enabled_dialects:
            diff_formats.append("OpenCode Add File")
        
        tool_formats["formats_available"] = diff_formats
        
        # Format output
        for tool in tool_formats["python_tools"]:
            lines.append(f"{tool} - [TYPE: python inside <TOOL_CALL> XML]")
        
        if tool_formats["bash_available"]:
            lines.append("*General Bash Commands* - [TYPE: bash command inside of <BASH> XML]")
        
        for fmt in tool_formats["formats_available"]:
            lines.append(f"*{fmt}* - [TYPE: {fmt} format]")
        
        lines.append("</TOOLS_AVAILABLE>")
        
        return "\n".join(lines)


# Global compiler instance
_compiler = None

def get_compiler() -> SystemPromptCompiler:
    """Get global system prompt compiler instance"""
    global _compiler
    if _compiler is None:
        _compiler = SystemPromptCompiler()
    return _compiler