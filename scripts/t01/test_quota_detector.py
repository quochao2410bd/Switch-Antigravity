import os
import sys
import json
from quota_detector import detect_quota_from_file

def run_tests():
    fixtures_dir = os.path.join(os.path.dirname(__file__), "../../tests/fixtures/t01")
    
    # Test 1: Positive Quota Fixture
    pos_file = os.path.join(fixtures_dir, "quota_positive.txt")
    res_pos = detect_quota_from_file(pos_file)
    assert res_pos["quota_exhausted"] is True, f"Expected quota_exhausted=True, got {res_pos}"
    assert res_pos["confidence"] == 1.0
    assert res_pos["error_code"] == 429
    assert res_pos["resets_in"] == "3h24m48s"
    assert res_pos["total_matches"] == 2
    print("PASS: Test 1 - Positive Quota Fixture")

    # Test 2: Negative Generic RESOURCE_EXHAUSTED
    neg_gen = os.path.join(fixtures_dir, "quota_negative_generic_resource_exhausted.txt")
    res_neg_gen = detect_quota_from_file(neg_gen)
    assert res_neg_gen["quota_exhausted"] is False, f"Expected quota_exhausted=False, got {res_neg_gen}"
    assert res_neg_gen["total_matches"] == 0
    print("PASS: Test 2 - Negative Generic RESOURCE_EXHAUSTED (non-quota)")

    # Test 3: Negative Other 429
    neg_429 = os.path.join(fixtures_dir, "quota_negative_429_other.txt")
    res_neg_429 = detect_quota_from_file(neg_429)
    assert res_neg_429["quota_exhausted"] is False, f"Expected quota_exhausted=False, got {res_neg_429}"
    assert res_neg_429["total_matches"] == 0
    print("PASS: Test 3 - Negative Other 429 (non-individual-quota)")

    # Test 4: Negative Normal Log
    neg_norm = os.path.join(fixtures_dir, "quota_negative_normal_log.txt")
    res_neg_norm = detect_quota_from_file(neg_norm)
    assert res_neg_norm["quota_exhausted"] is False, f"Expected quota_exhausted=False, got {res_neg_norm}"
    assert res_neg_norm["total_matches"] == 0
    print("PASS: Test 4 - Negative Normal Log")

    # Test 5: Live Language Server Log
    live_log = os.path.expandvars(r"%APPDATA%\Antigravity\logs\language_server.log")
    if os.path.exists(live_log):
        res_live = detect_quota_from_file(live_log)
        assert res_live["quota_exhausted"] is True
        assert res_live["total_matches"] == 20
        assert res_live["error_code"] == 429
        print(f"PASS: Test 5 - Live language_server.log ({res_live['total_matches']} historical events detected)")

    print("\nALL QUOTA DETECTOR TESTS PASSED SUCCESSFULLY.")

if __name__ == "__main__":
    run_tests()
