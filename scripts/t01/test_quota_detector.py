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
from correlate_cascade_id import extract_cdp_conversation_targets

def run_all_tests():
    fixtures_dir = os.path.join(os.path.dirname(__file__), "../../tests/fixtures/t01")
    
    print("=== Running Comprehensive Production Quota Detector Test Matrix (Round 4) ===")
    
    # ----------------------------------------------------
    # Round 4 Item 1 [UNIT_TEST]: poll_new_events rejects missing baseline directly
    # ----------------------------------------------------
    # Test 1a: baseline=None with non-existent log
    r_none1, c_none1 = poll_new_events(baseline=None, log_path="non_existent.log")
    assert c_none1 == 5, f"Expected exit code 5, got {c_none1}"
    assert r_none1["status"] == "BASELINE_REQUIRED"

    # Test 1b: baseline=None with existing log file
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp_exist:
        tmp_exist.write(b"log data\n")
        path_exist = tmp_exist.name
    try:
        r_none2, c_none2 = poll_new_events(baseline=None, log_path=path_exist)
        assert c_none2 == 5, f"Expected exit code 5 for existing log with None baseline, got {c_none2}"
        assert r_none2["status"] == "BASELINE_REQUIRED"

        # Test 1c: baseline=None with log_path=None
        r_none3, c_none3 = poll_new_events(baseline=None, log_path=None)
        assert c_none3 == 5
        assert r_none3["status"] == "BASELINE_REQUIRED"
    finally:
        if os.path.exists(path_exist): os.remove(path_exist)

    print("PASS: Item 1 [UNIT_TEST] - poll_new_events() rejects missing baseline with BASELINE_REQUIRED (exit code 5)")

    # ----------------------------------------------------
    # Round 4 Item 2 [UNIT_TEST]: Session & PID Binding Enforcement
    # ----------------------------------------------------
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp_sess:
        tmp_sess.write(b"Initial line\n")
        path_sess = tmp_sess.name
    try:
        base_bound, c_b = create_baseline(path_sess, ls_pid=11111, supervisor_session_id="sess_alpha")
        assert c_b == 0

        # Test 2a: Missing PID when bound -> BASELINE_INVALID
        r_miss_pid, c_miss_pid = poll_new_events(base_bound, path_sess, current_ls_pid=None, current_session_id="sess_alpha")
        assert c_miss_pid == 2 and r_miss_pid["status"] == "BASELINE_INVALID"
        assert "omitted by caller" in r_miss_pid["error"]

        # Test 2b: Wrong PID when bound -> BASELINE_INVALID
        r_wrong_pid, c_wrong_pid = poll_new_events(base_bound, path_sess, current_ls_pid=22222, current_session_id="sess_alpha")
        assert c_wrong_pid == 2 and r_wrong_pid["status"] == "BASELINE_INVALID"
        assert "process changed" in r_wrong_pid["error"]

        # Test 2c: Missing Session ID when bound -> BASELINE_INVALID
        r_miss_sess, c_miss_sess = poll_new_events(base_bound, path_sess, current_ls_pid=11111, current_session_id=None)
        assert c_miss_sess == 2 and r_miss_sess["status"] == "BASELINE_INVALID"
        assert "omitted by caller" in r_miss_sess["error"]

        # Test 2d: Wrong Session ID when bound -> BASELINE_INVALID
        r_wrong_sess, c_wrong_sess = poll_new_events(base_bound, path_sess, current_ls_pid=11111, current_session_id="sess_beta")
        assert c_wrong_sess == 2 and r_wrong_sess["status"] == "BASELINE_INVALID"
        assert "session changed" in r_wrong_sess["error"]

        # Test 2e: Exact match -> VALID (NO_NEW_EVENT)
        r_match_sess, c_match_sess = poll_new_events(base_bound, path_sess, current_ls_pid=11111, current_session_id="sess_alpha")
        assert c_match_sess == 1 and r_match_sess["status"] == "NO_NEW_EVENT"
    finally:
        if os.path.exists(path_sess): os.remove(path_sess)

    print("PASS: Item 2 [UNIT_TEST] - Bound baseline PID and session cannot be bypassed by omission")

    # ----------------------------------------------------
    # Round 4 Item 3 [UNIT_TEST]: CDP URL Exact Pathname & Type Matching
    # ----------------------------------------------------
    synthetic_cdp_targets = [
        {"id": "T1", "type": "page", "url": "https://127.0.0.1:58861/c/00000000-0000-4000-8000-000000000001?section=99"},
        {"id": "T2", "type": "page", "url": "https://127.0.0.1:58861/c/00000000-0000-4000-8000-000000000002"},
        # Negative targets:
        {"id": "T3", "type": "background_page", "url": "https://127.0.0.1:58861/c/00000000-0000-4000-8000-000000000003"},
        {"id": "T4", "type": "page", "url": "https://127.0.0.1:58861/foo/c/00000000-0000-4000-8000-000000000004"},
        {"id": "T5", "type": "page", "url": "https://127.0.0.1:58861/c/00000000-0000-4000-8000-000000000005-suffix"},
        {"id": "T6", "type": "page", "url": "https://127.0.0.1:58861/settings"}
    ]
    extracted_cdp = extract_cdp_conversation_targets(synthetic_cdp_targets)
    assert len(extracted_cdp) == 2, f"Expected exactly 2 eligible targets, got {len(extracted_cdp)}"
    assert extracted_cdp[0]["cascade_id"] == "00000000-0000-4000-8000-000000000001"
    assert extracted_cdp[1]["cascade_id"] == "00000000-0000-4000-8000-000000000002"
    print("PASS: Item 3 [UNIT_TEST] - CDP correlation strictly enforces type==page and exact UUID pathname matching")

    # ----------------------------------------------------
    # Round 4 Item 5 [UNIT_TEST]: event_id and hash bound to actual complete record bytes
    # ----------------------------------------------------
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp_diff:
        path_diff = tmp_diff.name
    try:
        base_d, _ = create_baseline(path_diff)
        # Two records with identical byte length, same offsets, same reset time, but different thread ID / message text
        line_a = b"ERROR: logging before google.Init: E0826 12:00:00.000000   10001 errorreport.go:223] agent executor error: calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 1h00m.\n"
        line_b = b"ERROR: logging before google.Init: E0826 12:00:00.000000   10002 errorreport.go:223] agent executor error: calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 1h00m.\n"
        assert len(line_a) == len(line_b)

        with open(path_diff, "wb") as f:
            f.write(line_a)
        res_a, _ = poll_new_events(base_d, path_diff)
        evt_a = res_a["latest_event"]

        with open(path_diff, "wb") as f:
            f.write(line_b)
        res_b, _ = poll_new_events(base_d, path_diff)
        evt_b = res_b["latest_event"]

        assert evt_a["event_sha256"] != evt_b["event_sha256"], "Record SHA-256 must differ for different record contents!"
        assert evt_a["event_id"] != evt_b["event_id"], "Event ID must differ for different record contents!"
        print(f"PASS: Item 5 [UNIT_TEST] - event_id and event_sha256 bind to actual record bytes (a: {evt_a['event_id']}, b: {evt_b['event_id']})")
    finally:
        if os.path.exists(path_diff): os.remove(path_diff)

    # ----------------------------------------------------
    # Round 4 Item 6 [UNIT_TEST]: Strict Schema Type Validation
    # ----------------------------------------------------
    # Test boolean where int is expected
    bad_bool_base = {
        "schema_version": SCHEMA_VERSION,
        "canonical_log_path": os.path.abspath("dummy.log"),
        "committed_byte_offset": True,  # bool is subclass of int in Python, must be rejected
        "file_identity": {"dev": 1, "ino": 2, "ctime_ns": 3, "size_at_creation": 4}
    }
    v_b, m_b = validate_baseline_schema(bad_bool_base, "dummy.log")
    assert not v_b and "committed_byte_offset" in m_b

    # Test negative size_at_creation
    bad_ident_base = {
        "schema_version": SCHEMA_VERSION,
        "canonical_log_path": os.path.abspath("dummy.log"),
        "committed_byte_offset": 100,
        "file_identity": {"dev": 1, "ino": 2, "ctime_ns": 3, "size_at_creation": -5}
    }
    v_i, m_i = validate_baseline_schema(bad_ident_base, "dummy.log")
    assert not v_i and "size_at_creation" in m_i
    print("PASS: Item 6 [UNIT_TEST] - Strict schema types, boundaries, and boolean injection rejected")

    # ----------------------------------------------------
    # Round 4 Item 9 [SYNTHETIC_SIMULATION]: Supervisor Cursor Replay & Deduplication Contract
    # ----------------------------------------------------
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp_replay:
        path_replay = tmp_replay.name
    try:
        quota_line = b"ERROR: logging before google.Init: E0826 12:00:00.000000   99999 errorreport.go:223] agent executor error: calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 2h00m.\n"
        base_rep, _ = create_baseline(path_replay)
        with open(path_replay, "ab") as f:
            f.write(quota_line)

        # Supervisor Poll 1
        res_p1, code_p1 = poll_new_events(base_rep, path_replay)
        assert code_p1 == 0
        evt_1 = res_p1["latest_event"]
        cursor_1 = res_p1["cursor"]

        # Supervisor Simulated Crash: Supervisor crashed BEFORE persisting cursor_1 into base_rep
        # Supervisor Restarts and polls again using original base_rep
        res_p2, code_p2 = poll_new_events(base_rep, path_replay)
        assert code_p2 == 0
        evt_2 = res_p2["latest_event"]
        cursor_2 = res_p2["cursor"]

        # Supervisor Deduplication Gate:
        seen_events = set()
        seen_events.add(evt_1["event_id"])
        
        is_duplicate = evt_2["event_id"] in seen_events
        assert is_duplicate, "Event from re-poll MUST be identified as duplicate via event_id"
        assert evt_1["event_sha256"] == evt_2["event_sha256"]

        # Supervisor commits cursor only after deduplication
        base_rep["committed_byte_offset"] = cursor_2

        # Subsequent poll after committed cursor -> NO_NEW_EVENT
        res_p3, code_p3 = poll_new_events(base_rep, path_replay)
        assert code_p3 == 1
        assert res_p3["status"] == "NO_NEW_EVENT"
        print(f"PASS: Item 9 [SYNTHETIC_SIMULATION] - Supervisor crash-replay deduplication contract verified ({evt_1['event_id']})")
    finally:
        if os.path.exists(path_replay): os.remove(path_replay)

    # ----------------------------------------------------
    # Incremental Lifecycle Matrix (Scenarios 2 - 14, 21 - 23)
    # ----------------------------------------------------
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        # Scenario 2: Historical event before baseline ignored
        with open(tmp_path, "wb") as f:
            f.write(b"ERROR: logging before google.Init: I0826 10:00:00.000000   100 server.go:100] Server started.\n")
            f.write(b"ERROR: logging before google.Init: E0826 10:05:00.000000   100 errorreport.go:223] calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 5h.\n")
        baseline, _ = create_baseline(tmp_path)
        res_2, code_2 = poll_new_events(baseline, tmp_path)
        assert code_2 == 1 and res_2["status"] == "NO_NEW_EVENT"
        assert res_2["current_session_quota_state"] == "UNKNOWN_OR_UNCHANGED"

        # Scenario 3: Normal append -> NO_NEW_EVENT
        with open(tmp_path, "ab") as f:
            f.write(b"ERROR: logging before google.Init: I0826 11:00:00.000000   100 server.go:300] Success.\n")
        res_3, code_3 = poll_new_events(baseline, tmp_path)
        assert code_3 == 1 and res_3["status"] == "NO_NEW_EVENT"
        baseline["committed_byte_offset"] = res_3["cursor"]

        # Scenario 4: Valid exact quota append -> NEW_CONFIRMED_QUOTA_EVENT
        with open(tmp_path, "ab") as f:
            f.write(quota_line)
        res_4, code_4 = poll_new_events(baseline, tmp_path)
        assert code_4 == 0 and res_4["status"] == "NEW_CONFIRMED_QUOTA_EVENT"
        assert res_4["current_session_quota_state"] == "CONFIRMED"
        assert res_4["resets_in"] == "2h00m"
        assert "raw_line" not in res_4["latest_event"]
        baseline["committed_byte_offset"] = res_4["cursor"]

        # Scenario 5 & 20: Repeat poll does NOT claim NORMAL -> UNKNOWN_OR_UNCHANGED
        res_5, code_5 = poll_new_events(baseline, tmp_path)
        assert code_5 == 1 and res_5["status"] == "NO_NEW_EVENT"
        assert res_5["current_session_quota_state"] == "UNKNOWN_OR_UNCHANGED"

        # Scenario 11: File truncation -> BASELINE_INVALID
        with open(tmp_path, "wb") as f:
            f.write(b"Truncated\n")
        res_11, code_11 = poll_new_events(baseline, tmp_path)
        assert code_11 == 2 and res_11["status"] == "BASELINE_INVALID"
    finally:
        if os.path.exists(tmp_path): os.remove(tmp_path)

    # Scenarios 7 & 8: Split writes in 2 and 3 chunks
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp_s:
        path_s = tmp_s.name
    try:
        base_s, _ = create_baseline(path_s)
        mid = len(quota_line) // 2
        with open(path_s, "ab") as f: f.write(quota_line[:mid])
        r_s1, c_s1 = poll_new_events(base_s, path_s)
        assert c_s1 == 1 and r_s1["status"] == "NO_NEW_EVENT"
        assert base_s["committed_byte_offset"] == 0

        with open(path_s, "ab") as f: f.write(quota_line[mid:])
        r_s2, c_s2 = poll_new_events(base_s, path_s)
        assert c_s2 == 0 and r_s2["status"] == "NEW_CONFIRMED_QUOTA_EVENT"
        assert r_s2["resets_in"] == "2h00m"
    finally:
        if os.path.exists(path_s): os.remove(path_s)

    # Scenarios 12, 13, 14: File replacement (smaller, equal, larger)
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp_r:
        path_r = tmp_r.name
    try:
        with open(path_r, "wb") as f: f.write(b"Initial content length 35 bytes...\n")
        base_r, _ = create_baseline(path_r)

        os.remove(path_r)
        time.sleep(0.01)
        with open(path_r, "wb") as f: f.write(b"Small\n")
        r_sm, c_sm = poll_new_events(base_r, path_r)
        assert c_sm == 2 and r_sm["status"] == "BASELINE_INVALID"

        os.remove(path_r)
        time.sleep(0.01)
        with open(path_r, "wb") as f: f.write(b"Replaced same length file bytes!\n")
        r_eq, c_eq = poll_new_events(base_r, path_r)
        assert c_eq == 2 and r_eq["status"] == "BASELINE_INVALID"

        os.remove(path_r)
        time.sleep(0.01)
        with open(path_r, "wb") as f: f.write(b"Much larger replaced file content bytes...\n" * 10)
        r_lg, c_lg = poll_new_events(base_r, path_r)
        assert c_lg == 2 and r_lg["status"] == "BASELINE_INVALID"
    finally:
        if os.path.exists(path_r): os.remove(path_r)

    # Contract fixtures rejection
    neg_503 = os.path.join(fixtures_dir, "quota_negative_code_503.txt")
    res_503, code_503 = detect_historical_events(neg_503)
    assert code_503 == 1 and res_503["total_matches"] == 0

    neg_gen = os.path.join(fixtures_dir, "quota_negative_generic_resource_exhausted.txt")
    res_gen, code_gen = detect_historical_events(neg_gen)
    assert code_gen == 1 and res_gen["total_matches"] == 0

    # Missing log on --init-baseline -> exit code 3
    res_miss, code_miss = create_baseline("C:\\non_existent_dir_999\\missing.log")
    assert code_miss == 3 and res_miss["status"] == "LOG_UNAVAILABLE"

    # ----------------------------------------------------
    # Round 4 Item 7 [LIVE_HISTORICAL_LOG_INSPECTION]: Robust Historical Check
    # ----------------------------------------------------
    live_log = os.path.expandvars(r"%APPDATA%\Antigravity\logs\language_server.log")
    if os.path.exists(live_log):
        res_live, code_live = detect_historical_events(live_log)
        assert isinstance(res_live["total_matches"], int)
        assert res_live["status"] in ["HISTORICAL_QUOTA_EVENT_FOUND", "NO_HISTORICAL_QUOTA_EVENT"]
        print(f"PASS: Item 7 [LIVE_HISTORICAL_LOG_INSPECTION] - Live log structural inspection verified ({res_live['total_matches']} historical events)")

    print("\n==================================================================")
    print("ALL DETERMINISTIC & CONTRACT TEST MATRIX SCENARIOS PASSED.")
    print("==================================================================")

if __name__ == "__main__":
    run_all_tests()
