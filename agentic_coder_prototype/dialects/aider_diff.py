from __future__ import annotations

import re
from typing import List

from ..core.core import BaseToolDialect, ToolDefinition, ToolCallParsed


class AiderDiffDialect(BaseToolDialect):
    """
    Parse Aider-style SEARCH/REPLACE blocks into a single apply_search_replace tool call.
    Accepts both fenced and unfenced variants.
    """

    type_id: str = "diff"

    _BLOCK_RE = re.compile(
        r"(?:```[a-z]*\n)?(?P<filename>(?!```)[^\n]+)\n<<<<<<< SEARCH\n(?P<search>.*?)\n=======\n(?P<replace>.*?)\n>>>>>>> REPLACE\n?(?:```)?",
        re.S,
    )

    def prompt_for_tools(self, tools: List[ToolDefinition]) -> str:
        return (
            "### Editing code with SEARCH/REPLACE blocks\n"
            "For every file that needs a change, emit a fenced block exactly like:\n"
            "```\n"
            "path/to/file.py\n"
            "<<<<<<< SEARCH\n"
            "# old code snippet\n"
            "=======\n"
            "# new replacement snippet\n"
            ">>>>>>> REPLACE\n"
            "```\n"
            "Repeat for multiple files; leave other prose unchanged.\n"
        )

    def parse_calls(self, text: str, tools: List[ToolDefinition]) -> List[ToolCallParsed]:
        calls: list[ToolCallParsed] = []
        for match in self._BLOCK_RE.finditer(text):
            calls.append(
                ToolCallParsed(
                    function="apply_search_replace",
                    arguments={
                        "file_name": match.group("filename").strip(),
                        "search": match.group("search"),
                        "replace": match.group("replace"),
                    },
                )
            )
        return calls



