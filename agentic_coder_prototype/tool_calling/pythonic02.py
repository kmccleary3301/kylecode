from __future__ import annotations

import ast
import re
from typing import List

from .core import BaseToolDialect, ToolDefinition, ToolCallParsed


class Pythonic02Dialect(BaseToolDialect):
    type_id = "python"

    function_call_description_template = (
        "\nYou may call a python functions to execute an action.\n"
        "To do so, you must wrap it in the following template:\n\n"
        "<TOOL_CALL> function_name(arg_1=value1, arg2=value2, ...) </TOOL_CALL>\n\n"
        "and it is wrapped as <TOOL_CALL> ... </TOOL_CALL>.\n"
        "The call MUST begin with the sequence \"<TOOL_CALL>\" and MUST end with the sequence \"</TOOL_CALL>\" to be valid.\n"
        "The inner content must be valid python code.\n\n"
        "Here are your available functions:\n\n{available_functions}\n\n"
        "Syntax: strictly use parentheses with comma-separated arguments and equal signs for keyword args.\n"
        "Example: my_tool(arg1=123, arg2=\"text\"). Do NOT use colons.\n"
    )

    def prompt_for_tools(self, tools: List[ToolDefinition]) -> str:
        functions_available_strings: list[str] = []
        for func in tools:
            arg_lines: list[str] = []
            for f_arg in func.parameters:
                desc = (f_arg.description or "").replace("\n", " ")
                parts = [f_arg.name]
                if f_arg.type:
                    parts.append(f": {f_arg.type}")
                if f_arg.default is not None:
                    parts.append(f" = {f_arg.default}")
                if desc:
                    parts.append(f"\t # {desc}")
                arg_lines.append("".join(parts))
            args_str = ",\n\t".join(arg_lines)
            functions_available_strings.append(
                f"def {func.name}(\t\t{args_str}\n)\n\"\"\"\n{func.description}\n\"\"\""
            )
        available = "\n\n".join(functions_available_strings)
        return self.function_call_description_template.format(available_functions=available)

    def parse_calls(self, input_str: str, tools: List[ToolDefinition]) -> List[ToolCallParsed]:
        pattern = r"<TOOL_CALL>(.*?)</TOOL_CALL>"
        tools_lookup = {tool.name: tool for tool in tools}
        function_calls: list[ToolCallParsed] = []
        for match in re.finditer(pattern, input_str, re.DOTALL):
            call_str = match.group(1).strip()
            try:
                tree = ast.parse(call_str, mode="exec")
                if not tree.body or not isinstance(tree.body[0], ast.Expr) or not isinstance(tree.body[0].value, ast.Call):
                    continue
                call_node = tree.body[0].value
                if isinstance(call_node.func, ast.Name):
                    function_name = call_node.func.id
                else:
                    continue
                if function_name not in tools_lookup:
                    continue
                tool_def = tools_lookup[function_name]
                arguments: dict[str, object] = {}
                for i, arg_node in enumerate(call_node.args):
                    if i < len(tool_def.parameters):
                        param_name = tool_def.parameters[i].name
                        arguments[param_name] = ast.literal_eval(arg_node)
                for keyword in call_node.keywords:
                    arguments[keyword.arg] = ast.literal_eval(keyword.value)
                function_calls.append(ToolCallParsed(function=function_name, arguments=arguments))
            except (SyntaxError, ValueError):
                # Fallback: tolerate JSON-like name: value by converting top-level ':' to '=' inside parentheses
                try:
                    m = re.match(r"^(?P<fname>[a-zA-Z_]\w*)\((?P<args>[\s\S]*)\)$", call_str)
                    if not m:
                        continue
                    fname = m.group("fname")
                    if fname not in tools_lookup:
                        continue
                    args_src = m.group("args")
                    # Replace top-level key: value with key=value
                    fixed_args = re.sub(r"(\b[a-zA-Z_]\w*\s*):", r"\1=", args_src)
                    fixed_call = f"{fname}({fixed_args})"
                    tree = ast.parse(fixed_call, mode="exec")
                    call_node = tree.body[0].value  # type: ignore[index]
                    if isinstance(call_node, ast.Call) and isinstance(call_node.func, ast.Name):
                        tool_def = tools_lookup[call_node.func.id]
                        arguments: dict[str, object] = {}
                        for i, arg_node in enumerate(call_node.args):
                            if i < len(tool_def.parameters):
                                param_name = tool_def.parameters[i].name
                                arguments[param_name] = ast.literal_eval(arg_node)
                        for keyword in call_node.keywords:
                            arguments[keyword.arg] = ast.literal_eval(keyword.value)
                        function_calls.append(ToolCallParsed(function=call_node.func.id, arguments=arguments))
                except Exception:
                    continue
        return function_calls




