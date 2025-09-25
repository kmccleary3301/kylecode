from __future__ import annotations

import ast
import re
from typing import List

from ..core.core import BaseToolDialect, ToolDefinition, ToolCallParsed


class PythonicInlineDialect(BaseToolDialect):
    """
    Parse bare python-like function calls without <TOOL_CALL> tags, including inside code fences.
    Example:
      write_file("a.c", "int main(){}")
      run_shell("make -j4")
    """

    type_id = "python"

    # Capture inside optional code fences; then find call lines
    _FENCE_RE = re.compile(r"```[\s\S]*?```", re.M)
    _CALL_RE_TMPL = r"(?P<call>\b{fname}\s*\((?P<args>[\s\S]*?)\))"

    def prompt_for_tools(self, tools: List[ToolDefinition]) -> str:
        names = ", ".join(t.name for t in tools)
        return (
            "You may invoke tools by writing direct python-like calls on their own lines, optionally inside a code block.\n"
            "You are encouraged to invoke multiple non-blocking tools in one reply; blocking tools must run alone.\n"
            f"Allowed functions: {names}."
        )

    def parse_calls(self, text: str, tools: List[ToolDefinition]) -> List[ToolCallParsed]:
        calls: list[ToolCallParsed] = []
        # Search whole text, including fenced blocks
        for tool in tools:
            pattern = re.compile(self._CALL_RE_TMPL.format(fname=re.escape(tool.name)), re.DOTALL)
            for m in pattern.finditer(text):
                call_src = m.group("call")
                try:
                    node = ast.parse(call_src, mode="eval").body
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == tool.name:
                        args: dict[str, object] = {}
                        # positional mapped to parameters order
                        for i, arg_node in enumerate(node.args):
                            if i < len(tool.parameters):
                                param_name = tool.parameters[i].name
                                args[param_name] = ast.literal_eval(arg_node)
                        for kw in node.keywords:
                            if kw.arg is not None:
                                args[kw.arg] = ast.literal_eval(kw.value)
                        calls.append(ToolCallParsed(function=tool.name, arguments=args))
                except Exception:
                    # Tolerate JSON-like key: value pairs
                    try:
                        m = re.match(rf"^\b{re.escape(tool.name)}\((?P<args>[\s\S]*)\)$", call_src)
                        if not m:
                            continue
                        args_src = m.group("args")
                        fixed_args = re.sub(r"(\b[a-zA-Z_]\w*\s*):", r"\1=", args_src)
                        fixed_call = f"{tool.name}({fixed_args})"
                        node = ast.parse(fixed_call, mode="eval").body
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == tool.name:
                            args: dict[str, object] = {}
                            for i, arg_node in enumerate(node.args):
                                if i < len(tool.parameters):
                                    param_name = tool.parameters[i].name
                                    args[param_name] = ast.literal_eval(arg_node)
                            for kw in node.keywords:
                                if kw.arg is not None:
                                    args[kw.arg] = ast.literal_eval(kw.value)
                            calls.append(ToolCallParsed(function=tool.name, arguments=args))
                    except Exception:
                        continue
        return calls



