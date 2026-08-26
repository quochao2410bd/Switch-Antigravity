import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from quota_detector import create_baseline, poll_new_events, detect_historical_events

def run_all_tests():
    fixtures_dir = os.path.join(os.path.dirname(__file__), "../../tests/fixtures/t01")
    
    print("=== Running Comprehensive Incremental Quota Detector Tests ===")
    
    with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write("ERROR: logging before google.Init: I0826 10:00:00.000000  100 server.go:100] Server started.\n")
            f.write("ERROR: logging before google.Init: E0826 10:05:00.000000  100 errorreport.go:223] calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 5h.\n")
            f.write("ERROR: logging before google.Init: I0826 10:10:00.000000  100 server.go:200] Old turn ended.\n")

        baseline = create_baseline(tmp_path, ls_pid=12345)
        assert baseline["status"] == "BASELINE_INITIALIZED"
        assert baseline["byte_offset"] > 0
        
        res_1a = poll_new_events(baseline, tmp_path)
        assert res_1a["status"] == "NO_NEW_EVENT", f"Expected NO_NEW_EVENT, got {res_1a['status']}"
        assert res_1a["current_session_quota_state"] == "NORMAL"
        assert res_1a["signature_confidence"] == 0.0
        print("PASS: Scenario 1 - Stale historical quota event before baseline -> NO_NEW_EVENT")

        with open(tmp_path, "a", encoding="utf-8") as f:
            f.write("ERROR: logging before google.Init: I0826 11:00:00.000000  100 http_helpers.go:246] URL: https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent\n")
            f.write("ERROR: logging before google.Init: I0826 11:01:00.000000  100 server.go:300] Tool execution completed successfully.\n")

        res_1b = poll_new_events(baseline, tmp_path)
        assert res_1b["status"] == "NO_NEW_EVENT"
        assert res_1b["current_session_quota_state"] == "NORMAL"
        baseline["byte_offset"] = res_1b["cursor"]
        print("PASS: Scenario 2 - Normal non-quota lines appended -> NO_NEW_EVENT")

        with open(tmp_path, "a", encoding="utf-8") as f:
            f.write("ERROR: logging before google.Init: E0826 11:05:00.000000  100 errorreport.go:223] agent executor error: calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 2h30m.\n")

        res_1c = poll_new_events(baseline, tmp_path)
        assert res_1c["status"] == "NEW_CONFIRMED_QUOTA_EVENT"
        assert res_1c["event_scope"] == "NEW_SINCE_BASELINE"
        assert res_1c["current_session_quota_state"] == "CONFIRMED"
        assert res_1c["signature_confidence"] == 1.0
        assert res_1c["resets_in"] == "2h30m"
        assert res_1c["new_events_count"] == 1
        baseline["byte_offset"] = res_1c["cursor"]
        print("PASS: Scenario 3 - One new exact quota line appended -> NEW_CONFIRMED_QUOTA_EVENT")

        res_1d = poll_new_events(baseline, tmp_path)
        assert res_1d["status"] == "NO_NEW_EVENT"
        assert res_1d["current_session_quota_state"] == "NORMAL"
        print("PASS: Scenario 4 - Re-polling same file without changes -> NO_NEW_EVENT")

        with open(tmp_path, "a", encoding="utf-8") as f:
            f.write("ERROR: logging before google.Init: E0826 11:10:00.000000  100 errorreport.go:223] calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 2h25m.\n")

        res_1e = poll_new_events(baseline, tmp_path)
        assert res_1e["status"] == "NEW_CONFIRMED_QUOTA_EVENT"
        assert res_1e["resets_in"] == "2h25m"
        baseline["byte_offset"] = res_1e["cursor"]
        print("PASS: Scenario 5 - Second new quota event appended -> NEW_CONFIRMED_QUOTA_EVENT")

        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write("ERROR: Log rotated after restart.\n")
        res_1f = poll_new_events(baseline, tmp_path)
        assert res_1f["status"] == "BASELINE_INVALID"
        assert res_1f["rebaseline_required"] is True
        print("PASS: Scenario 6 - Log truncation / rotation -> BASELINE_INVALID (rebaseline_required=True)")

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    neg_503 = os.path.join(fixtures_dir, "quota_negative_code_503.txt")
    res_503 = detect_historical_events(neg_503)
    assert res_503["status"] == "NO_HISTORICAL_QUOTA_EVENT"
    assert res_503["total_matches"] == 0
    print("PASS: Scenario 7 - Strict code 429 enforcement (rejects code 503 fixture)")

    neg_gen = os.path.join(fixtures_dir, "quota_negative_generic_resource_exhausted.txt")
    res_gen = detect_historical_events(neg_gen)
    assert res_gen["status"] == "NO_HISTORICAL_QUOTA_EVENT"
    print("PASS: Scenario 8 - Generic non-quota RESOURCE_EXHAUSTED rejected")

    neg_429 = os.path.join(fixtures_dir, "quota_negative_429_other.txt")
    res_429 = detect_historical_events(neg_429)
    assert res_429["status"] == "NO_HISTORICAL_QUOTA_EVENT"
    print("PASS: Scenario 9 - Generic 429 RPM rate limit rejected")

    live_log = os.path.expandvars(r"%APPDATA%\Antigravity\logs\language_server.log")
    if os.path.exists(live_log):
        res_live = detect_historical_events(live_log)
        assert res_live["status"] == "HISTORICAL_QUOTA_EVENT_FOUND"
        assert res_live["total_matches"] >= 20
        assert res_live["signature_confidence"] == 1.0
        assert res_live["event_scope"] == "HISTORICAL"
        assert res_live["current_session_quota_state"] == "UNKNOWN_HISTORICAL_ONLY"
        print(f"PASS: Scenario 10 - Live language_server.log historical diagnostic scan ({res_live['total_matches']} historical events identified)")

    print("\n=======================================================")
    print("ALL 10 INCREMENTAL & CONTRACT TEST SCENARIOS PASSED.")
    print("=======================================================")

if __name__ == "__main__":
    run_all_tests()
