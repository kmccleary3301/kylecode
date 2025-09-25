from __future__ import annotations

from typing import Any, Dict


class APIRequestRecorder:
    """Thin helper that delegates to LoggerV2Manager for request/response dumps."""

    def __init__(self, lm) -> None:
        self.lm = lm

    def save_request(self, turn: int, payload: Dict[str, Any]) -> str:
        return self.lm.write_json(f"raw/requests/turn_{turn}.request.json", payload)

    def save_response(self, turn: int, payload: Dict[str, Any]) -> str:
        return self.lm.write_json(f"raw/responses/turn_{turn}.response.json", payload)


