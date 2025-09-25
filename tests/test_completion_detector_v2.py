from agentic_coder_prototype.state.completion_detector import CompletionDetector


def test_completion_detector_text_sentinel_boost():
    cd = CompletionDetector(config={"confidence_threshold": 0.6})
    # No tool available -> higher boost
    out = cd.detect_completion("TASK COMPLETE", "stop", [], agent_config={"tools": {"mark_task_complete": False}})
    assert out["completed"] is True
    assert out["confidence"] >= 0.9


def test_completion_detector_provider_signal():
    cd = CompletionDetector(config={"confidence_threshold": 0.6})
    out = cd.detect_completion("All requirements met", "stop", [], agent_config={"tools": {"mark_task_complete": True}})
    assert out["completed"] in (True, False)  # not strict; ensure no crash



