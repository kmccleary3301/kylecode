from __future__ import annotations

import re
from typing import List

from ..core.core import BaseToolDialect, ToolDefinition, ToolCallParsed


class OpenCodePatchDialect(BaseToolDialect):
    """Parse OpenCode-style patch blocks into a single `patch` tool call."""

    type_id: str = "diff"

    _PATCH_BLOCK_RE = re.compile(r"\*\*\* Begin Patch[\t ]*\r?\n(?P<body>[\s\S]*?)\*\*\* End Patch", re.M)

    def prompt_for_tools(self, tools: List[ToolDefinition]) -> str:
        return (
            "### Applying changes with OpenCode patch blocks\n"
            "Wrap every edit between *** Begin Patch and *** End Patch markers. Within a patch block you may emit\n"
            "*** Add File, *** Update File, or *** Delete File directives.\n"
            "- Use '+' prefixes for new lines, '-' for removals, and leading spaces for unchanged context inside @@ hunks.\n"
            "- Keep related edits in a single patch block and avoid narrative text inside the markers.\n"
            "- Prefer patch blocks over legacy diff syntaxes when modifying files.\n"
        )

    def parse_calls(self, text: str, tools: List[ToolDefinition]) -> List[ToolCallParsed]:
        calls: list[ToolCallParsed] = []
        for patch in self._PATCH_BLOCK_RE.finditer(text):
            block = patch.group(0).strip()
            if not block:
                continue
            calls.append(
                ToolCallParsed(
                    function="patch",
                    arguments={"patchText": block},
                )
            )
        return calls
