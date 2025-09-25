from __future__ import annotations

import re
from typing import List

from ..core.core import ToolDefinition, ToolCallParsed
from .enhanced_base_dialect import (
    EnhancedBaseDialect,
    EnhancedToolDefinition,
    ParsedToolCall,
    ToolCallFormat,
    TaskType,
)


class UnifiedDiffDialect(EnhancedBaseDialect):
    """
    Unified Diff dialect with backward compatibility.
    - Provides enhanced interface (format, parse_tool_calls, format_tools_for_prompt)
    - Preserves legacy interface (prompt_for_tools, parse_calls)
    """

    _FENCE_RE = re.compile(r"```(?:patch|diff)\n(?P<patch>[\s\S]*?)\n```", re.M)
    _RAW_RE = re.compile(r"(diff --git[\s\S]+?)(?=\n\S|\Z)", re.M)

    def __init__(self) -> None:
        super().__init__()
        self.type_id = "diff"
        self.format = ToolCallFormat.UNIFIED_DIFF
        self.provider_support = ["*"]
        self.use_cases = [TaskType.CODE_EDITING, TaskType.FILE_MODIFICATION]

    # ===== Enhanced interface =====
    def get_system_prompt_section(self) -> str:
        return (
            "### Applying changes with UNIFIED DIFF patches\n"
            "Emit one or more fenced code blocks labelled as patch or diff. Example:\n"
            "```patch\n"
            "diff --git a/path/to/file.py b/path/to/file.py\n"
            "new file mode 100644\n"
            "index 0000000..e69de29\n"
            "--- /dev/null\n"
            "+++ b/path/to/file.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+print(\"hello\")\n"
            "```\n"
            "- For new files, use /dev/null → b/<path> with new file mode lines.\n"
            "- Multiple files can be included in a single block.\n"
            "- Do not include extra prose inside the fenced block.\n"
        )

    def parse_tool_calls(self, content: str) -> List[ParsedToolCall]:
        parsed: List[ParsedToolCall] = []
        # Prefer fenced blocks
        for m in self._FENCE_RE.finditer(content):
            patch_text = (m.group("patch") or "").strip()
            if patch_text:
                parsed.append(
                    ParsedToolCall(
                        function="apply_diff",
                        arguments={"patch": patch_text, "hunks": [{"raw": patch_text}]},
                        raw_content=patch_text,
                        format=self.format.value,
                    )
                )
        if parsed:
            return parsed
        # Fallback: raw diff starting with diff --git
        for m in self._RAW_RE.finditer(content):
            patch_text = (m.group(1) or "").strip()
            if patch_text:
                parsed.append(
                    ParsedToolCall(
                        function="apply_diff",
                        arguments={"patch": patch_text, "hunks": [{"raw": patch_text}]},
                        raw_content=patch_text,
                        format=self.format.value,
                    )
                )
        return parsed

    def format_tools_for_prompt(self, tools: List[EnhancedToolDefinition]) -> str:
        lines = [self.get_system_prompt_section(), "\nTOOLS (diff-capable):"]
        for t in tools:
            lines.append(f"- {t.name}: {t.description}")
        return "\n".join(lines)

    # ===== Backward-compat interface =====
    def prompt_for_tools(self, tools: List[ToolDefinition]) -> str:
        return self.get_system_prompt_section()

    def parse_calls(self, text: str, tools: List[ToolDefinition]) -> List[ToolCallParsed]:
        results: List[ToolCallParsed] = []
        for p in self.parse_tool_calls(text):
            # Back-compat: map enhanced function name to legacy
            legacy_fn = "apply_unified_patch" if p.function == "apply_diff" else p.function
            # Legacy expects a single 'patch' argument
            legacy_args = {"patch": p.arguments.get("patch")} if legacy_fn == "apply_unified_patch" else dict(p.arguments)
            results.append(ToolCallParsed(function=legacy_fn, arguments=legacy_args))
        return results

