from __future__ import annotations

from typing import Any, Dict, List


class PromptArtifactLogger:
    """Persists compiled prompts and catalogs via LoggerV2Manager."""

    def __init__(self, lm) -> None:
        self.lm = lm

    def save_compiled_system(self, content: str) -> str:
        return self.lm.write_text("prompts/compiled_system.md", content)

    def save_per_turn(self, turn: int, content: str) -> str:
        return self.lm.write_text(f"prompts/per_turn/turn_{turn}.md", content)

    def save_catalog(self, name: str, files: List[Dict[str, str]]) -> str:
        # name can be dialect or hash; write manifest only (templates should be in files already)
        manifest = {"name": name, "files": files}
        return self.lm.write_json(f"prompts/catalogs/{name}/catalog_manifest.json", manifest)


