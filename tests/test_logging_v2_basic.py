import os
from pathlib import Path

from agentic_coder_prototype.logging_v2 import LoggerV2Manager
from agentic_coder_prototype.logging_v2.api_recorder import APIRequestRecorder
from agentic_coder_prototype.logging_v2.prompt_logger import PromptArtifactLogger


def test_logger_v2_creates_run_tree(tmp_path):
    cfg = {"logging": {"root_dir": str(tmp_path / "logging"), "redact": True, "include_raw": True}}
    lm = LoggerV2Manager(cfg)
    run = lm.start_run("session-xyz")
    runp = Path(run)
    # Ensure key subfolders
    assert (runp / "conversation").exists()
    assert (runp / "raw/requests").exists()
    assert (runp / "prompts/per_turn").exists()
    # Write some artifacts
    rec = APIRequestRecorder(lm)
    rec.save_request(1, {"model": "gpt-5-nano", "api_key": "SECRET"})
    rec.save_response(1, {"status": "ok", "authorization": "Bearer SECRET"})
    pl = PromptArtifactLogger(lm)
    pl.save_compiled_system("SYSTEM PROMPT")
    pl.save_per_turn(1, "TOOLS AVAIL")
    # Files should be created and redacted
    req = (runp / "raw/requests/turn_1.request.json").read_text()
    assert "REDACTED" in req
    resp = (runp / "raw/responses/turn_1.response.json").read_text()
    assert "REDACTED" in resp
    sys = (runp / "prompts/compiled_system.md").read_text()
    assert "SYSTEM PROMPT" in sys


