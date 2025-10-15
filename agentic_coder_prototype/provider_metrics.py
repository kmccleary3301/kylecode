from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CallMetric:
    route: str
    stream: bool
    elapsed: float
    outcome: str  # success | error
    error_reason: Optional[str] = None
    html_detected: bool = False
    details: Optional[Dict[str, Any]] = None


@dataclass
class ProviderMetricsCollector:
    """Collects per-call metrics for provider invocations."""

    calls: List[CallMetric] = field(default_factory=list)
    fallbacks: List[Dict[str, Any]] = field(default_factory=list)
    circuit_skips: List[str] = field(default_factory=list)
    stream_overrides: List[Dict[str, Any]] = field(default_factory=list)
    tool_overrides: List[Dict[str, Any]] = field(default_factory=list)

    def reset(self) -> None:
        self.calls.clear()
        self.fallbacks.clear()
        self.circuit_skips.clear()
        self.stream_overrides.clear()
        self.tool_overrides.clear()

    def add_call(
        self,
        route: str,
        *,
        stream: bool,
        elapsed: float,
        outcome: str,
        error_reason: Optional[str] = None,
        html_detected: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.calls.append(
            CallMetric(
                route=route,
                stream=stream,
                elapsed=elapsed,
                outcome=outcome,
                error_reason=error_reason,
                html_detected=html_detected,
                details=details,
            )
        )

    def add_fallback(self, *, primary: str, fallback: str, reason: str) -> None:
        self.fallbacks.append(
            {
                "from": primary,
                "to": fallback,
                "reason": reason,
            }
        )

    def add_circuit_skip(self, route: str) -> None:
        self.circuit_skips.append(route)

    def add_stream_override(self, *, route: Optional[str], reason: str) -> None:
        self.stream_overrides.append(
            {
                "route": route,
                "reason": reason,
            }
        )

    def add_tool_override(self, *, route: Optional[str], reason: str) -> None:
        self.tool_overrides.append(
            {
                "route": route,
                "reason": reason,
            }
        )

    def _aggregate_routes(self) -> Dict[str, Dict[str, Any]]:
        routes: Dict[str, Dict[str, Any]] = {}
        for call in self.calls:
            entry = routes.setdefault(
                call.route,
                {
                    "calls": 0,
                    "success": 0,
                    "errors": 0,
                    "stream_calls": 0,
                    "html_errors": 0,
                    "latency_sum": 0.0,
                    "latency_max": 0.0,
                },
            )
            entry["calls"] += 1
            entry["latency_sum"] += call.elapsed
            entry["latency_max"] = max(entry["latency_max"], call.elapsed)
            if call.stream:
                entry["stream_calls"] += 1
            if call.outcome == "success":
                entry["success"] += 1
            else:
                entry["errors"] += 1
            if call.html_detected:
                entry["html_errors"] += 1
        for entry in routes.values():
            calls = entry["calls"] or 1
            entry["latency_avg"] = entry["latency_sum"] / calls
        return routes

    def snapshot(self) -> Dict[str, Any]:
        routes = self._aggregate_routes()
        total_calls = len(self.calls)
        total_success = sum(1 for call in self.calls if call.outcome == "success")
        total_errors = total_calls - total_success
        html_errors = sum(1 for call in self.calls if call.html_detected)

        return {
            "summary": {
                "calls": total_calls,
                "success": total_success,
                "errors": total_errors,
                "html_errors": html_errors,
                "circuit_skips": len(self.circuit_skips),
                "stream_overrides": len(self.stream_overrides),
                "tool_overrides": len(self.tool_overrides),
            },
            "routes": routes,
            "fallbacks": list(self.fallbacks),
            "circuit_skips": list(self.circuit_skips),
            "stream_overrides": list(self.stream_overrides),
            "tool_overrides": list(self.tool_overrides),
        }
