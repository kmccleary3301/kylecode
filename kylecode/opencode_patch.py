"""OpenCode patch parsing and application helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import unified_diff
from typing import Callable, Iterable, List, Sequence


class PatchParseError(RuntimeError):
    """Raised when an OpenCode patch cannot be parsed."""


@dataclass
class PatchChange:
    kind: str  # "keep", "add", "remove"
    content: str


@dataclass
class PatchHunk:
    context: str
    changes: List[PatchChange]


@dataclass
class PatchOperation:
    kind: str  # "add", "update", "delete"
    file_path: str
    hunks: List[PatchHunk] | None = None
    content: str | None = None


_PATCH_BLOCK_RE = re.compile(r"\*\*\* Begin Patch[\t ]*\r?\n(?P<body>[\s\S]*?)\*\*\* End Patch", re.M)


def _iter_patch_blocks(text: str) -> Iterable[str]:
    for match in _PATCH_BLOCK_RE.finditer(text):
        yield match.group("body")

    if text.strip() and not _PATCH_BLOCK_RE.search(text):
        raise PatchParseError("No *** Begin Patch ... *** End Patch block found")


def parse_opencode_patch(text: str) -> List[PatchOperation]:
    """Parse OpenCode patch text into structured operations."""

    operations: list[PatchOperation] = []

    for block in _iter_patch_blocks(text):
        lines = block.splitlines()
        idx = 0
        while idx < len(lines):
            line = lines[idx].strip()
            if not line:
                idx += 1
                continue

            if line.startswith("*** Add File:"):
                path = line.split(":", 1)[1].strip()
                if not path:
                    raise PatchParseError("Missing path in *** Add File section")
                idx += 1
                content_lines: list[str] = []
                while idx < len(lines) and not lines[idx].startswith("***"):
                    body_line = lines[idx]
                    if body_line.startswith("+"):
                        content_lines.append(body_line[1:])
                    elif body_line.startswith("@@") or not body_line:
                        # Ignore hunk markers and blank lines
                        pass
                    else:
                        content_lines.append(body_line)
                    idx += 1
                operations.append(
                    PatchOperation(kind="add", file_path=path, content="\n".join(content_lines).rstrip("\n"))
                )
                continue

            if line.startswith("*** Delete File:"):
                path = line.split(":", 1)[1].strip()
                if not path:
                    raise PatchParseError("Missing path in *** Delete File section")
                operations.append(PatchOperation(kind="delete", file_path=path))
                idx += 1
                continue

            if line.startswith("*** Update File:"):
                path = line.split(":", 1)[1].strip()
                if not path:
                    raise PatchParseError("Missing path in *** Update File section")
                idx += 1
                hunks: list[PatchHunk] = []
                while idx < len(lines) and not lines[idx].startswith("***"):
                    if lines[idx].startswith("@@"):
                        context = lines[idx][2:].strip()
                        idx += 1
                        changes: list[PatchChange] = []
                        while idx < len(lines) and not lines[idx].startswith("@@") and not lines[idx].startswith("***"):
                            change_line = lines[idx]
                            if not change_line:
                                idx += 1
                                continue
                            prefix = change_line[0]
                            payload = change_line[1:]
                            if prefix == " ":
                                changes.append(PatchChange("keep", payload))
                            elif prefix == "+":
                                changes.append(PatchChange("add", payload))
                            elif prefix == "-":
                                changes.append(PatchChange("remove", payload))
                            else:
                                # Treat un-prefixed lines as unchanged context for resilience
                                changes.append(PatchChange("keep", change_line))
                            idx += 1
                        if not changes:
                            raise PatchParseError("Empty @@ hunk in update section")
                        hunks.append(PatchHunk(context=context, changes=changes))
                    else:
                        idx += 1
                if not hunks:
                    raise PatchParseError("Update section missing @@ hunks")
                operations.append(PatchOperation(kind="update", file_path=path, hunks=hunks))
                continue

            raise PatchParseError(f"Unrecognised directive inside patch block: {line}")

    if not operations:
        raise PatchParseError("No patch operations were parsed")

    return operations


def _find_subsequence(haystack: Sequence[str], needle: Sequence[str]) -> int:
    if not needle:
        return 0
    for start in range(len(haystack) - len(needle) + 1):
        if list(haystack[start : start + len(needle)]) == list(needle):
            return start
    return -1


def apply_update_hunks(original: str, hunks: Sequence[PatchHunk]) -> str:
    lines = original.split("\n")
    for hunk in hunks:
        base_segment = [c.content for c in hunk.changes if c.kind != "add"]
        replacement_segment = [c.content for c in hunk.changes if c.kind != "remove"]
        idx = _find_subsequence(lines, base_segment)
        if idx == -1 and hunk.context:
            idx = next((i for i, val in enumerate(lines) if hunk.context in val), -1)
        if idx == -1:
            raise PatchParseError(f"Context not found for hunk: {hunk.context or '[no context]'}")
        lines[idx : idx + len(base_segment)] = replacement_segment
    return "\n".join(lines)


def summarise_changes(operations: Iterable[PatchOperation]) -> dict[str, list[str]]:
    summary = {"added": [], "modified": [], "deleted": []}
    for op in operations:
        if op.kind == "add":
            summary["added"].append(op.file_path)
        elif op.kind == "delete":
            summary["deleted"].append(op.file_path)
        elif op.kind == "update":
            summary["modified"].append(op.file_path)
    return summary


def operations_to_unified_diff(
    operations: Sequence[PatchOperation],
    fetch_current: Callable[[str], str],
) -> str:
    """Convert parsed OpenCode operations into a unified diff string."""

    chunks: list[str] = []
    for op in operations:
        path = op.file_path
        if op.kind == "add":
            new_text = (op.content or "").splitlines()
            diff_lines = list(
                unified_diff(
                    [],
                    new_text,
                    fromfile="/dev/null",
                    tofile=f"b/{path}",
                    lineterm="",
                )
            )
            if not diff_lines:
                diff_lines = [
                    "--- /dev/null",
                    f"+++ b/{path}",
                    "@@ -0,0 +0,0 @@",
                ]
        elif op.kind == "delete":
            original = fetch_current(path).splitlines()
            diff_lines = list(
                unified_diff(
                    original,
                    [],
                    fromfile=f"a/{path}",
                    tofile="/dev/null",
                    lineterm="",
                )
            )
            if not diff_lines:
                diff_lines = [
                    f"--- a/{path}",
                    "+++ /dev/null",
                    "@@ -0,0 +0,0 @@",
                ]
        elif op.kind == "update":
            original_text = fetch_current(path)
            new_text = apply_update_hunks(original_text, op.hunks or [])
            diff_lines = list(
                unified_diff(
                    original_text.splitlines(),
                    new_text.splitlines(),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                    lineterm="",
                )
            )
            if not diff_lines:
                raise PatchParseError(f"No changes detected for {path}")
        else:
            raise PatchParseError(f"Unsupported operation kind: {op.kind}")

        chunk = "\n".join(diff_lines)
        if chunk:
            chunks.append(chunk)

    if not chunks:
        raise PatchParseError("Patch contained no changes")

    return "\n".join(chunks) + "\n"


def to_unified_diff(patch_text: str, fetch_current: Callable[[str], str]) -> str:
    """Parse OpenCode patch text and convert it to unified diff."""

    operations = parse_opencode_patch(patch_text)
    return operations_to_unified_diff(operations, fetch_current)
