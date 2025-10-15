#!/usr/bin/env python3
"""Export provider metrics from telemetry JSONL.

Reads the telemetry log (default: logging/<run>/meta/telemetry.jsonl) and
prints an aggregated summary or forwards each event to an HTTP endpoint.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import urllib.request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export provider metrics telemetry events")
    parser.add_argument(
        "telemetry_path",
        help="Path to telemetry JSONL file (e.g. logging/<run>/meta/telemetry.jsonl)",
    )
    parser.add_argument(
        "--http-endpoint",
        default=None,
        help="Optional HTTP endpoint to POST each event (application/json)",
    )
    parser.add_argument(
        "--print-summary",
        action="store_true",
        help="Print aggregated summary (default behaviour if no endpoint is provided)",
    )
    return parser.parse_args()


def load_events(path: Path) -> Iterable[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        print(f"[export-provider-metrics] telemetry file not found: {path}", file=sys.stderr)


def post_event(endpoint: str, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as exc:  # pragma: no cover - network failure path
        print(f"[export-provider-metrics] Failed to POST event: {exc}", file=sys.stderr)


def aggregate(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"calls": 0, "errors": 0, "html_errors": 0, "routes": {}}
    for event in events:
        if event.get("event") != "provider_metrics":
            continue
        details = event.get("summary") or {}
        summary["calls"] += int(details.get("calls", 0))
        summary["errors"] += int(details.get("errors", 0))
        summary["html_errors"] += int(details.get("html_errors", 0))
        for route, data in (event.get("routes") or {}).items():
            route_entry = summary["routes"].setdefault(
                route,
                {"calls": 0, "errors": 0, "html_errors": 0},
            )
            route_entry["calls"] += int(data.get("calls", 0))
            route_entry["errors"] += int(data.get("errors", 0))
            route_entry["html_errors"] += int(data.get("html_errors", 0))
    return summary


def main() -> int:
    args = parse_args()
    telemetry_path = Path(args.telemetry_path).resolve()
    events: List[Dict[str, Any]] = list(load_events(telemetry_path))

    if args.http_endpoint:
        for event in events:
            if event.get("event") == "provider_metrics":
                post_event(args.http_endpoint, event)
        return 0

    if args.print_summary or not args.http_endpoint:
        summary = aggregate(events)
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
