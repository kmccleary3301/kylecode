from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


class LoggerV2Manager:
    """Orchestrates v2 logging: directory creation, manifests, redaction hook.

    Usage:
      lm = LoggerV2Manager(config)
      run_dir = lm.start_run(session_id)
      lm.write_meta({ ... })
      lm.write_text("conversation/conversation.md", header)
    """

    def __init__(self, config: Dict[str, Any] | None = None) -> None:
        self.config = config or {}
        log_cfg = (self.config.get("logging") or {})
        self.enabled = bool(log_cfg.get("enabled", True))
        self.root_dir = Path((log_cfg.get("root_dir") or "logging")).resolve()
        self.redact_enabled = bool(((self.config.get("logging") or {}).get("redact") or True))
        self.include_raw = bool(((self.config.get("logging") or {}).get("include_raw") or True))
        self.include_events = bool(((self.config.get("logging") or {}).get("include_events") or True))
        self.include_structured_requests = bool((log_cfg.get("include_structured_requests", True)))
        self.retention_max_runs = int((log_cfg.get("retention") or {}).get("max_runs", 0) or 0)
        self.run_dir: Optional[Path] = None

    def _now_ts(self) -> str:
        return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")

    def _redact(self, data: Any) -> Any:
        if not self.redact_enabled:
            return data
        try:
            # Simple key-based redaction
            secret_keys = {"api_key", "authorization", "openai_api_key", "openrouter_api_key"}
            def _rec(obj):
                if isinstance(obj, dict):
                    out = {}
                    for k, v in obj.items():
                        if str(k).lower() in secret_keys:
                            out[k] = "***REDACTED***"
                        else:
                            out[k] = _rec(v)
                    return out
                if isinstance(obj, list):
                    return [_rec(x) for x in obj]
                return obj
            return _rec(data)
        except Exception:
            return data

    def start_run(self, session_id: str) -> str:
        if not self.enabled:
            self.run_dir = None
            return ""
        ts = self._now_ts()
        # Normalize session id to avoid path separators or './'
        sid_raw = str(session_id or "session")
        sid_clean = sid_raw.replace(os.sep, "_").replace("/", "_").replace("\\", "_").replace("./", "").strip("_")
        sid = (sid_clean or "session")[:32]
        run_dir = self.root_dir / f"{ts}_{sid}"
        # Create base tree
        for sub in [
            "conversation",
            "raw/requests",
            "raw/responses",
            "errors",
            "prompts/per_turn",
            "prompts/catalogs",
            "provider_native/tools_provided",
            "provider_native/tool_calls",
            "provider_native/tool_results",
            "artifacts/tool_results",
            "artifacts/diffs",
            "meta",
            "meta/requests",
        ]:
            (run_dir / sub).mkdir(parents=True, exist_ok=True)
        self.run_dir = run_dir
        # Seed index
        (run_dir / "conversation/index.json").write_text(json.dumps({
            "run_dir": str(run_dir),
            "created_utc": ts,
            "session_id": session_id,
        }, indent=2), encoding="utf-8")
        # Optional retention cleanup
        try:
            if self.retention_max_runs > 0 and self.root_dir.exists():
                subdirs = [p for p in self.root_dir.iterdir() if p.is_dir()]
                subdirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                for old in subdirs[self.retention_max_runs:]:
                    try:
                        # Remove directory tree
                        import shutil
                        shutil.rmtree(old, ignore_errors=True)
                    except Exception:
                        pass
        except Exception:
            pass
        return str(run_dir)

    def write_json(self, rel_path: str, data: Any) -> str:
        if not self.run_dir:
            return ""
        path = (self.run_dir / rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        redacted = self._redact(data)
        try:
            path.write_text(json.dumps(redacted, indent=2), encoding="utf-8")
            return str(path)
        except Exception:
            return ""

    def write_text(self, rel_path: str, content: str) -> str:
        if not self.run_dir:
            return ""
        path = (self.run_dir / rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(content, encoding="utf-8")
            return str(path)
        except Exception:
            return ""

    def append_text(self, rel_path: str, content: str) -> str:
        if not self.run_dir:
            return ""
        path = (self.run_dir / rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(content)
            return str(path)
        except Exception:
            return ""

    def write_meta(self, meta: Dict[str, Any]) -> str:
        return self.write_json("meta/run_meta.json", meta)
