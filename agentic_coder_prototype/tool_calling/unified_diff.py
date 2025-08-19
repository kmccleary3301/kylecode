from __future__ import annotations

import re
from typing import List

from .core import BaseToolDialect, ToolDefinition, ToolCallParsed


class UnifiedDiffDialect(BaseToolDialect):
    """
    Parse unified-diff (git-style) code blocks and emit an apply_unified_patch tool call.

    Supported forms:
    - Fenced blocks labelled as ```patch or ```diff
    - Raw text starting with "diff --git" (best-effort)
    """

    type_id: str = "diff"

    _FENCE_RE = re.compile(r"```(?:patch|diff)\n(?P<patch>[\s\S]*?)\n```", re.M)
    _RAW_RE = re.compile(r"(diff --git[\s\S]+?)(?=\n\S|\Z)", re.M)

    def prompt_for_tools(self, tools: List[ToolDefinition]) -> str:
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

    def parse_calls(self, text: str, tools: List[ToolDefinition]) -> List[ToolCallParsed]:
        calls: list[ToolCallParsed] = []
        # Prefer fenced blocks
        for m in self._FENCE_RE.finditer(text):
            patch_text = m.group("patch").strip()
            if patch_text:
                calls.append(
                    ToolCallParsed(function="apply_unified_patch", arguments={"patch": patch_text})
                )
        if calls:
            return calls
        # Fallback: raw diff starting with diff --git
        for m in self._RAW_RE.finditer(text):
            patch_text = m.group(1).strip()
            if patch_text:
                calls.append(
                    ToolCallParsed(function="apply_unified_patch", arguments={"patch": patch_text})
                )
        return calls

