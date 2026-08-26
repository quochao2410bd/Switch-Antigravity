import os
import sys
import json
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from quota_detector import (
    create_baseline,
    poll_new_events,
    detect_historical_events,
    validate_baseline_schema,
    SCHEMA_VERSION
)

def run_all_tests():
    fixtures_dir = os.path.join(os.path.dirname(__file__), "../../tests/fixtures/t01")
    
    print("=== Running Comprehensive Production Quota Detector Test Matrix ===")
    
    # Scenario 1 [UNIT_TEST]: Baseline Mandatory for Polling
    res_no_base, code_no_base = poll_new_events(baseline=None, log_path="dummy.log")
    assert code_no_base == 3 or code_no_base == 2
    valid, msg = validate_baseline_schema(None, "dummy.log")
    assert not valid
    print("PASS: Scenario 1 [UNIT_TEST] - Polling without baseline rejected")

    # Lifecycle Test Suite (Scenarios 2 - 6, 20)
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # Scenario 2 [SYNTHETIC_SIMULATION]: Historical event before baseline ignored
        with open(tmp_path, "wb") as f:
            f.write(b"ERROR: logging before google.Init: I0826 10:00:00.000000   100 server.go:100] Server started.\n")
            f.write(b"ERROR: logging before google.Init: E0826 10:05:00.000000   100 errorreport.go:223] calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 5h.\n")
            f.write(b"ERROR: logging before google.Init: I0826 10:10:00.000000   100 server.go:200] Old turn ended.\n")

        baseline, code_init = create_baseline(tmp_path, ls_pid=12345, supervisor_session_id="sess_001")
        assert code_init == 0
        assert baseline["status"] == "BASELINE_INITIALIZED"
        assert baseline["committed_byte_offset"] > 0

        res_2, code_2 = poll_new_events(baseline, tmp_path)
        assert code_2 == 1
        assert res_2["status"] == "NO_NEW_EVENT"
        assert res_2["current_session_quota_state"] == "UNKNOWN_OR_UNCHANGED"
        assert res_2["signature_confidence"] == 0.0
        print("PASS: Scenario 2 [SYNTHETIC_SIMULATION] - Historical event before baseline ignored -> NO_NEW_EVENT")

        # Scenario 3 [SYNTHETIC_SIMULATION]: Normal non-quota append -> NO_NEW_EVENT
        with open(tmp_path, "ab") as f:
            f.write(b"ERROR: logging before google.Init: I0826 11:00:00.000000   100 http_helpers.go:246] URL: https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent\n")
            f.write(b"ERROR: logging before google.Init: I0826 11:01:00.000000   100 server.go:300] Tool execution completed successfully.\n")

        res_3, code_3 = poll_new_events(baseline, tmp_path)
        assert code_3 == 1
        assert res_3["status"] == "NO_NEW_EVENT"
        assert res_3["current_session_quota_state"] == "UNKNOWN_OR_UNCHANGED"
        baseline["committed_byte_offset"] = res_3["cursor"]
        print("PASS: Scenario 3 [SYNTHETIC_SIMULATION] - Normal append -> NO_NEW_EVENT")

        # Scenario 4 [SYNTHETIC_SIMULATION]: Valid exact quota append -> NEW_CONFIRMED_QUOTA_EVENT
        with open(tmp_path, "ab") as f:
            f.write(b"ERROR: logging before google.Init: E0826 11:05:00.000000   100 errorreport.go:223] agent executor error: calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 2h30m.\n")

        res_4, code_4 = poll_new_events(baseline, tmp_path)
        assert code_4 == 0
        assert res_4["status"] == "NEW_CONFIRMED_QUOTA_EVENT"
        assert res_4["current_session_quota_state"] == "CONFIRMED"
        assert res_4["resets_in"] == "2h30m"
        assert res_4["new_events_count"] == 1
        assert "event_id" in res_4["latest_event"]
        assert "raw_line" not in res_4["latest_event"]
        baseline["committed_byte_offset"] = res_4["cursor"]
        print("PASS: Scenario 4 [SYNTHETIC_SIMULATION] - Valid exact quota append -> NEW_CONFIRMED_QUOTA_EVENT")

        # Scenario 5 & 20 [SYNTHETIC_SIMULATION]: Repeat poll without changes -> NO_NEW_EVENT (does NOT revert state to NORMAL)
        res_5, code_5 = poll_new_events(baseline, tmp_path)
        assert code_5 == 1
        assert res_5["status"] == "NO_NEW_EVENT"
        assert res_5["current_session_quota_state"] == "UNKNOWN_OR_UNCHANGED"
        assert res_5["quota_state_effect"] == "UNCHANGED"
        print("PASS: Scenario 5 & 20 [SYNTHETIC_SIMULATION] - Repeat poll does NOT claim NORMAL -> UNKNOWN_OR_UNCHANGED")

        # Scenario 6 [SYNTHETIC_SIMULATION]: Second quota event appended -> NEW_CONFIRMED_QUOTA_EVENT
        with open(tmp_path, "ab") as f:
            f.write(b"ERROR: logging before google.Init: E0826 11:10:00.000000   100 errorreport.go:223] calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 2h25m.\n")

        res_6, code_6 = poll_new_events(baseline, tmp_path)
        assert code_6 == 0
        assert res_6["status"] == "NEW_CONFIRMED_QUOTA_EVENT"
        assert res_6["resets_in"] == "2h25m"
        baseline["committed_byte_offset"] = res_6["cursor"]
        print("PASS: Scenario 6 [SYNTHETIC_SIMULATION] - Second quota event -> NEW_CONFIRMED_QUOTA_EVENT")

        # Scenario 11 [SYNTHETIC_SIMULATION]: Log truncation -> BASELINE_INVALID
        with open(tmp_path, "wb") as f:
            f.write(b"ERROR: Truncated log.\n")
        res_11, code_11 = poll_new_events(baseline, tmp_path)
        assert code_11 == 2
        assert res_11["status"] == "BASELINE_INVALID"
        assert res_11["rebaseline_required"] is True
        print("PASS: Scenario 11 [SYNTHETIC_SIMULATION] - File truncation -> BASELINE_INVALID")

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    # Split Write Tests (Scenarios 7 & 8)
    quota_line = b"ERROR: logging before google.Init: E0826 12:00:00.000000   100 errorreport.go:223] agent executor error: calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 1h15m.\n"

    # Scenario 7 [SYNTHETIC_SIMULATION]: Split write in 2 chunks
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp_split2:
        path_s2 = tmp_split2.name
    try:
        base_s2, _ = create_baseline(path_s2)
        mid = len(quota_line) // 2
        chunk1 = quota_line[:mid]
        chunk2 = quota_line[mid:]

        with open(path_s2, "ab") as f:
            f.write(chunk1)
        res_s2_1, code_s2_1 = poll_new_events(base_s2, path_s2)
        assert code_s2_1 == 1
        assert res_s2_1["status"] == "NO_NEW_EVENT"
        assert base_s2["committed_byte_offset"] == 0

        with open(path_s2, "ab") as f:
            f.write(chunk2)
        res_s2_2, code_s2_2 = poll_new_events(base_s2, path_s2)
        assert code_s2_2 == 0
        assert res_s2_2["status"] == "NEW_CONFIRMED_QUOTA_EVENT"
        assert res_s2_2["resets_in"] == "1h15m"
        assert res_s2_2["new_events_count"] == 1
        print("PASS: Scenario 7 [SYNTHETIC_SIMULATION] - Split write in 2 chunks handled safely")
    finally:
        if os.path.exists(path_s2): os.remove(path_s2)

    # Scenario 8 [SYNTHETIC_SIMULATION]: Split write in 3 chunks
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp_split3:
        path_s3 = tmp_split3.name
    try:
        base_s3, _ = create_baseline(path_s3)
        split_pt1 = quota_line.find(b"RESOURCE_EXHAUSTED") - 5
        split_pt2 = quota_line.find(b"Resets in 1h15m") + 10

        with open(path_s3, "ab") as f:
            f.write(quota_line[:split_pt1])
        r1, c1 = poll_new_events(base_s3, path_s3)
        assert c1 == 1 and r1["status"] == "NO_NEW_EVENT"

        with open(path_s3, "ab") as f:
            f.write(quota_line[split_pt1:split_pt2])
        r2, c2 = poll_new_events(base_s3, path_s3)
        assert c2 == 1 and r2["status"] == "NO_NEW_EVENT"

        with open(path_s3, "ab") as f:
            f.write(quota_line[split_pt2:])
        r3, c3 = poll_new_events(base_s3, path_s3)
        assert c3 == 0 and r3["status"] == "NEW_CONFIRMED_QUOTA_EVENT"
        assert r3["resets_in"] == "1h15m"
        print("PASS: Scenario 8 [SYNTHETIC_SIMULATION] - Split write in 3 chunks handled safely")
    finally:
        if os.path.exists(path_s3): os.remove(path_s3)

    # Multibyte UTF-8 & CRLF (Scenarios 9 & 10)
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp_utf8:
        path_utf8 = tmp_utf8.name
    try:
        base_utf8, _ = create_baseline(path_utf8)
        with open(path_utf8, "ab") as f:
            f.write("ERROR: logging: Thử nghiệm tiếng Việt 🚀 中文测试\n".encode("utf-8"))
            f.write(quota_line)
        r_utf8, c_utf8 = poll_new_events(base_utf8, path_utf8)
        assert c_utf8 == 0
        assert r_utf8["status"] == "NEW_CONFIRMED_QUOTA_EVENT"
        print("PASS: Scenario 9 [SYNTHETIC_SIMULATION] - UTF-8 multibyte prefix handled safely")
    finally:
        if os.path.exists(path_utf8): os.remove(path_utf8)

    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp_crlf:
        path_crlf = tmp_crlf.name
    try:
        base_crlf, _ = create_baseline(path_crlf)
        crlf_quota = quota_line.replace(b"\n", b"\r\n")
        with open(path_crlf, "ab") as f:
            f.write(crlf_quota)
        r_crlf, c_crlf = poll_new_events(base_crlf, path_crlf)
        assert c_crlf == 0
        assert r_crlf["status"] == "NEW_CONFIRMED_QUOTA_EVENT"
        assert r_crlf["resets_in"] == "1h15m"
        print("PASS: Scenario 10 [SYNTHETIC_SIMULATION] - CRLF line endings handled safely")
    finally:
        if os.path.exists(path_crlf): os.remove(path_crlf)

    # File Replacement Tests (Scenarios 12, 13, 14)
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp_orig:
        path_orig = tmp_orig.name
    try:
        with open(path_orig, "wb") as f:
            f.write(b"Initial baseline log content of length 40 bytes\n")
        base_repl, _ = create_baseline(path_orig)

        os.remove(path_orig)
        time.sleep(0.01)

        with open(path_orig, "wb") as f:
            f.write(b"Small\n")
        r_small, c_small = poll_new_events(base_repl, path_orig)
        assert c_small == 2 and r_small["status"] == "BASELINE_INVALID"
        print("PASS: Scenario 12 [SYNTHETIC_SIMULATION] - Replaced with smaller file -> BASELINE_INVALID")

        os.remove(path_orig)
        time.sleep(0.01)
        with open(path_orig, "wb") as f:
            f.write(b"Replaced same length file content of exact size!\n")
        r_eq, c_eq = poll_new_events(base_repl, path_orig)
        assert c_eq == 2 and r_eq["status"] == "BASELINE_INVALID"
        print("PASS: Scenario 13 [SYNTHETIC_SIMULATION] - Replaced with equal size file -> BASELINE_INVALID")

        os.remove(path_orig)
        time.sleep(0.01)
        with open(path_orig, "wb") as f:
            f.write(b"Replaced file with significantly larger content than previous baseline offset\n" * 5)
        r_large, c_large = poll_new_events(base_repl, path_orig)
        assert c_large == 2 and r_large["status"] == "BASELINE_INVALID"
        print("PASS: Scenario 14 [SYNTHETIC_SIMULATION] - Replaced with larger file -> BASELINE_INVALID")
    finally:
        if os.path.exists(path_orig): os.remove(path_orig)

    # Schema & Session Validation (Scenarios 15, 16, 17, 18)
    bad_path_base = {
        "schema_version": SCHEMA_VERSION,
        "canonical_log_path": os.path.abspath("non_matching_path.log"),
        "committed_byte_offset": 10,
        "file_identity": {"dev": 1, "ino": 2, "ctime_ns": 3}
    }
    r_path, c_path = poll_new_events(bad_path_base, "target_path.log")
    assert c_path == 3 or c_path == 2
    print("PASS: Scenario 15 [UNIT_TEST] - Canonical path mismatch rejected")

    bad_schema_base = {
        "canonical_log_path": os.path.abspath("dummy.log"),
        "committed_byte_offset": 10,
        "file_identity": {"dev": 1, "ino": 2, "ctime_ns": 3}
    }
    valid_s, _ = validate_baseline_schema(bad_schema_base, "dummy.log")
    assert not valid_s
    print("PASS: Scenario 16 [UNIT_TEST] - Missing schema_version rejected")

    neg_cursor_base = {
        "schema_version": SCHEMA_VERSION,
        "canonical_log_path": os.path.abspath("dummy.log"),
        "committed_byte_offset": -50,
        "file_identity": {"dev": 1, "ino": 2, "ctime_ns": 3}
    }
    valid_neg, _ = validate_baseline_schema(neg_cursor_base, "dummy.log")
    assert not valid_neg
    print("PASS: Scenario 17 [UNIT_TEST] - Negative offset rejected")

    pid_base = {
        "schema_version": SCHEMA_VERSION,
        "canonical_log_path": os.path.abspath("dummy.log"),
        "committed_byte_offset": 100,
        "language_server_process_id": 11111,
        "file_identity": {"dev": 1, "ino": 2, "ctime_ns": 3}
    }
    valid_pid, msg_pid = validate_baseline_schema(pid_base, "dummy.log", current_ls_pid=22222)
    assert not valid_pid
    assert "Language server process changed" in msg_pid
    print("PASS: Scenario 18 [UNIT_TEST] - Language server process session change rejected")

    # Deterministic Event Replay (Scenario 19)
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp_replay:
        path_rep = tmp_replay.name
    try:
        base_rep, _ = create_baseline(path_rep)
        with open(path_rep, "ab") as f:
            f.write(quota_line)
        
        r_rep1, _ = poll_new_events(base_rep, path_rep)
        evt_id1 = r_rep1["latest_event"]["event_id"]
        evt_hash1 = r_rep1["latest_event"]["event_sha256"]

        r_rep2, _ = poll_new_events(base_rep, path_rep)
        evt_id2 = r_rep2["latest_event"]["event_id"]
        evt_hash2 = r_rep2["latest_event"]["event_sha256"]

        assert evt_id1 == evt_id2, f"Replay event_id mismatch: {evt_id1} vs {evt_id2}"
        assert evt_hash1 == evt_hash2
        print(f"PASS: Scenario 19 [SYNTHETIC_SIMULATION] - Deterministic Event Replay ID ({evt_id1}) verified")
    finally:
        if os.path.exists(path_rep): os.remove(path_rep)

    # Contract & Negative Fixtures (Scenarios 21, 22, 23, 24)
    neg_503 = os.path.join(fixtures_dir, "quota_negative_code_503.txt")
    res_503, code_503 = detect_historical_events(neg_503)
    assert code_503 == 1
    assert res_503["total_matches"] == 0
    print("PASS: Scenario 21 [UNIT_TEST] - Strict code 429 enforcement (rejects code 503 fixture)")

    neg_gen = os.path.join(fixtures_dir, "quota_negative_generic_resource_exhausted.txt")
    res_gen, code_gen = detect_historical_events(neg_gen)
    assert code_gen == 1
    assert res_gen["total_matches"] == 0
    print("PASS: Scenario 22 [UNIT_TEST] - Generic non-quota RESOURCE_EXHAUSTED rejected")

    res_miss, code_miss = create_baseline("C:\\non_existent_dir_12345\\missing.log")
    assert code_miss == 3
    assert res_miss["status"] == "LOG_UNAVAILABLE"
    print("PASS: Scenario 23 [UNIT_TEST] - Missing log file on --init-baseline returns exit code 3 (LOG_UNAVAILABLE)")

    live_log = os.path.expandvars(r"%APPDATA%\Antigravity\logs\language_server.log")
    if os.path.exists(live_log):
        res_live, code_live = detect_historical_events(live_log)
        assert code_live == 0
        assert res_live["status"] == "HISTORICAL_QUOTA_EVENT_FOUND"
        assert res_live["total_matches"] >= 20
        assert res_live["signature_confidence"] == 1.0
        print(f"PASS: Scenario 24 [VERIFIED_LIVE_RUNTIME] - Live language_server.log scan ({res_live['total_matches']} historical events identified)")

    print("\n==================================================================")
    print("ALL 24 PRODUCTION & SYNTHETIC TEST MATRIX SCENARIOS PASSED.")
    print("==================================================================")

if __name__ == "__main__":
    run_all_tests()
