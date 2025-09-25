from __future__ import annotations

from typing import Any, Dict, List


class ProviderNativeLogger:
    """Persists provider-native tool schemas, calls, and results via LoggerV2Manager."""

    def __init__(self, lm) -> None:
        self.lm = lm

    def save_tools_provided(self, turn: int, tools_payload: Dict[str, Any]) -> str:
        return self.lm.write_json(f"provider_native/tools_provided/turn_{turn}.json", tools_payload)

    def save_tool_calls(self, turn: int, calls_payload: List[Dict[str, Any]]) -> str:
        return self.lm.write_json(f"provider_native/tool_calls/turn_{turn}.json", calls_payload)

    def save_tool_results(self, turn: int, results_payload: List[Dict[str, Any]]) -> str:
        return self.lm.write_json(f"provider_native/tool_results/turn_{turn}.json", results_payload)


