from __future__ import annotations

from typing import List

from .core import BaseToolDialect, ToolDefinition, ToolCallParsed


class CompositeToolCaller:
    def __init__(self, dialects: List[BaseToolDialect]):
        self._dialects = list(dialects)

    def build_prompt(self, tool_defs: List[ToolDefinition]) -> str:
        sections: list[str] = []
        for d in self._dialects:
            subset = [t for t in tool_defs if t.type_id == d.type_id]
            if subset:
                sections.append(d.prompt_for_tools(subset))
        return "\n\n".join(sections)

    def parse_all(self, text: str, tool_defs: List[ToolDefinition]) -> List[ToolCallParsed]:
        # Aggregate parsed calls from all dialects and de-duplicate by (function, arguments)
        all_calls: list[ToolCallParsed] = []
        seen: set[str] = set()
        for d in self._dialects:
            subset = [t for t in tool_defs if t.type_id == d.type_id]
            for call in d.parse_calls(text, subset):
                # Build a stable key from function name and sorted arguments representation
                try:
                    import json as _json
                    key = f"{call.function}|" + _json.dumps(call.arguments, sort_keys=True, separators=(",", ":"))
                except Exception:
                    key = f"{call.function}|{str(call.arguments)}"
                if key in seen:
                    continue
                seen.add(key)
                all_calls.append(call)
        return all_calls




