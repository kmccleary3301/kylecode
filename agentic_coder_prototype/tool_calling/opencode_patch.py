from __future__ import annotations

import re
from typing import List

from .core import BaseToolDialect, ToolDefinition, ToolCallParsed


class OpenCodePatchDialect(BaseToolDialect):
    """
    Parse OpenCode-style patch blocks for file creation:

    *** Begin Patch
    *** Add File: path/to/file.ext
    @@
    +line 1
    +line 2
    @@
    *** End Patch

    We are permissive:
    - If '+' prefixes are present, we collect only '+' lines (stripping the '+').
    - If no '+' lines exist, we take the body as-is (excluding header markers and '@@').
    - Multiple Add File sections in one Begin/End Patch are supported (one ToolCallParsed per file).
    """

    type_id: str = "diff"

    _PATCH_BLOCK_RE = re.compile(r"\*\*\* Begin Patch\n(?P<body>[\s\S]*?)\*\*\* End Patch", re.M)
    _ADD_FILE_RE = re.compile(r"\*\*\* Add File: (?P<path>.+?)\n(?P<content>[\s\S]*?)(?=(\n\*\*\* |\Z))", re.M)

    def prompt_for_tools(self, tools: List[ToolDefinition]) -> str:
        return (
            "### Creating new files with Add File blocks (OpenCode-style)\n"
            "Wrap additions between *** Begin Patch and *** End Patch. For each new file, emit:\n"
            "```\n"
            "*** Begin Patch\n"
            "*** Add File: path/to/file.ext\n"
            "@@\n"
            "+<file content lines starting with '+'>\n"
            "@@\n"
            "*** End Patch\n"
            "```\n"
            "Notes:\n"
            "- Prefer prefixing content lines with '+'; we will strip it.\n"
            "- You may include multiple *** Add File sections inside one patch.\n"
            "- Do not include extra prose inside the patch fences.\n"
        )

    def parse_calls(self, text: str, tools: List[ToolDefinition]) -> List[ToolCallParsed]:
        calls: list[ToolCallParsed] = []
        for patch in self._PATCH_BLOCK_RE.finditer(text):
            body = patch.group("body")
            for add in self._ADD_FILE_RE.finditer(body):
                path = add.group("path").strip()
                raw = add.group("content")
                # Extract content lines
                lines = []
                plus_found = False
                for line in raw.splitlines():
                    if line.startswith("*** "):
                        continue
                    if line.strip() == "@@":
                        continue
                    if line.startswith("+"):
                        plus_found = True
                        lines.append(line[1:])
                    elif not plus_found:
                        # If no '+' lines seen yet, collect plain lines (fallback mode)
                        lines.append(line)
                content = "\n".join(lines).rstrip("\n")
                calls.append(
                    ToolCallParsed(
                        function="create_file_from_block",
                        arguments={"file_name": path, "content": content},
                    )
                )
        return calls



