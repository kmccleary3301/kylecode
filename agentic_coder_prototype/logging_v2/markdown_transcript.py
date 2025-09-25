from __future__ import annotations

from typing import List, Dict, Optional


class MarkdownTranscriptWriter:
    """Renders structured sections into a readable conversation.md file content.

    This writer only returns formatted text; the manager handles file I/O.
    """

    def __init__(self) -> None:
        pass

    def section(self, title: str, body: str) -> str:
        body = body.rstrip("\n")
        return f"\n\n**{title}**\n\n{body}\n"

    def system(self, content: str) -> str:
        return self.section("System", content)

    def user(self, content: str) -> str:
        return self.section("User", content)

    def assistant(self, content: str) -> str:
        return self.section("Assistant", content)

    def tools_available_temp(self, summary: str, link: Optional[str] = None) -> str:
        hdr = "Tools Available Prompt (Temporarily Appended to User Message)"
        if link:
            summary += f"\n\nSee: {link}"
        return self.section(hdr, summary)

    def provider_tools_provided(self, ids: List[str], link: Optional[str] = None) -> str:
        body = "\n".join(ids)
        if link:
            body += f"\n\nSee: {link}"
        return self.section("Tools Provided via Provider API Tool Schema", body)

    def provider_tool_calls(self, calls_summary: str, link: Optional[str] = None) -> str:
        body = calls_summary
        if link:
            body += f"\n\nSee: {link}"
        return self.section("Model Called Provider Schema-Native Tools", body)

    def provider_tool_results(self, results_summary: str, link: Optional[str] = None) -> str:
        body = results_summary
        if link:
            body += f"\n\nSee: {link}"
        return self.section("Provider Schema-Native Tool Call Results", body)

    def text_tool_results(self, summary: str, links: Optional[List[str]] = None) -> str:
        # Render a compact artifact list; the actual assistant content should be logged separately.
        body = summary
        if links:
            body += "\n\n" + "\n".join([f"- {p}" for p in links])
        return self.section("Tool Results (Text-Based Execution)", body)


