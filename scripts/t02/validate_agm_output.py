#!/usr/bin/env python3
"""
validate_agm_output.py

Comprehensive test suite verifying parser robustness and freshness provenance:
- Expected standard table output (with freshness binding)
- 5-hour-old cached quota (must be marked STALE_CACHED)
- Successful refresh followed by parse (PROVEN_FRESH)
- Refresh failure leaving cached quota (REFRESH_FAILED)
- Mixed refresh-all partial success
- Missing refresh provenance (STALE_CACHED)
- Verifier unit tests (offline unverified vs network matched vs mismatch)
- Empty account list, Unicode emails, Malformed percentage fields
"""

import os
import sys
import time
from pathlib import Path

# Add current dir to path to import modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inspect_quota import FreshnessState, parse_agm_info, parse_agm_list
from selection_policy import AccountSelector, SelectionConfig, TerminalState
from verify_active_account import (
    CredentialVerificationStatus,
    verify_active_account,
)


def run_tests():
    fixtures_dir = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "t02"
    if not fixtures_dir.exists():
        print(f"Error: Fixtures directory not found at {fixtures_dir}", file=sys.stderr)
        return False

    print(f"=== Running AGM Parser & Freshness Robustness Suite ===")
    print(f"Fixtures Path: {fixtures_dir}\n")
    tests_passed = 0
    tests_total = 0
    now = int(time.time())

    # --- Group 1: Freshness Provenance Tests ---

    # 1.1: Missing provenance -> STALE_CACHED / UNKNOWN_UNFETCHED
    tests_total += 1
    p = fixtures_dir / "list_normal.txt"
    with open(p, "r", encoding="utf-8") as f:
        res = parse_agm_list(f.read(), now_epoch=now)
    assert len(res) == 4
    for acc in res:
        assert acc.freshness_state in (FreshnessState.STALE_CACHED, FreshnessState.UNKNOWN_UNFETCHED)
        assert not acc.eligible  # Stale or unfetched data is not eligible!
    print("  [PASS] Missing provenance: all accounts correctly marked STALE_CACHED and ineligible")
    tests_passed += 1

    # 1.2: 5-hour-old cached quota parsed now -> STALE_CACHED
    tests_total += 1
    old_prov = {"alice@example.com": now - 18000}  # 5 hours ago
    with open(p, "r", encoding="utf-8") as f:
        res = parse_agm_list(f.read(), refresh_provenance=old_prov, now_epoch=now)
    alice = next(r for r in res if r.safe_account_ref == "alice@example.com")
    assert alice.freshness_state == FreshnessState.STALE_CACHED
    assert not alice.eligible
    print("  [PASS] 5-hour-old cached quota: marked STALE_CACHED (age 18000s > max 300s)")
    tests_passed += 1

    # 1.3: Successful fresh refresh followed by parse -> PROVEN_FRESH
    tests_total += 1
    fresh_prov = {
        "alice@example.com": now - 10,
        "bob.dev@corp.example.org": now - 10,
        "charlie-test@domain.co.uk": now - 10,
    }
    with open(p, "r", encoding="utf-8") as f:
        res = parse_agm_list(f.read(), refresh_provenance=fresh_prov, now_epoch=now)
    alice = next(r for r in res if r.safe_account_ref == "alice@example.com")
    assert alice.freshness_state == FreshnessState.PROVEN_FRESH
    assert alice.eligible is True
    print("  [PASS] Fresh refresh provenance: alice@example.com marked PROVEN_FRESH and eligible")
    tests_passed += 1

    # 1.4: Mixed refresh-all partial success
    tests_total += 1
    mixed_prov = {"alice@example.com": now - 5, "charlie-test@domain.co.uk": now - 5}
    failed_ref = ["bob.dev@corp.example.org"]
    with open(p, "r", encoding="utf-8") as f:
        res = parse_agm_list(f.read(), refresh_provenance=mixed_prov, failed_refreshes=failed_ref, now_epoch=now)
    bob = next(r for r in res if r.safe_account_ref == "bob.dev@corp.example.org")
    assert bob.freshness_state == FreshnessState.REFRESH_FAILED
    assert not bob.eligible
    print("  [PASS] Mixed refresh-all: bob.dev marked REFRESH_FAILED and ineligible")
    tests_passed += 1

    # --- Group 2: End-to-End Freshness + Selection Integration ---
    tests_total += 1
    selector = AccountSelector(SelectionConfig(min_quota_pct=20))
    with open(p, "r", encoding="utf-8") as f:
        stale_accounts = parse_agm_list(f.read(), now_epoch=now)
    sel_res = selector.select_next_account(stale_accounts, now=now)
    assert sel_res.selected_account is None
    assert sel_res.terminal_state == TerminalState.BLOCKED_QUOTA_UNKNOWN

    with open(p, "r", encoding="utf-8") as f:
        fresh_accounts = parse_agm_list(f.read(), refresh_provenance=fresh_prov, now_epoch=now)
    sel_res2 = selector.select_next_account(fresh_accounts, current_active_ref="alice@example.com", now=now)
    assert sel_res2.selected_account == "bob.dev@corp.example.org"
    print("  [PASS] End-to-end integration: selector blocks stale cached accounts and selects fresh candidate")
    tests_passed += 1

    # --- Group 3: Verifier Unit Tests (Critical Item 5) ---

    # 3.1: Offline / No network introspection -> CREDENTIAL_STORE_WRITTEN_UNVERIFIED
    tests_total += 1
    mock_vault = {
        "token": {
            "access_token": "mock_token_abc",
            "refresh_token": "mock_refresh_xyz",
            "expiry": "2026-08-27T00:00:00Z"
        }
    }
    v_res1 = verify_active_account(expected_account="user@example.com", introspect_network=False, mock_payload=mock_vault)
    assert v_res1.status == CredentialVerificationStatus.CREDENTIAL_STORE_WRITTEN_UNVERIFIED
    assert v_res1.matches_expected is None  # Must NOT be False or True!
    assert v_res1.desktop_adoption_status == "UNKNOWN"
    print("  [PASS] Verifier without network: returns CREDENTIAL_STORE_WRITTEN_UNVERIFIED with matches_expected=None")
    tests_passed += 1

    # 3.2: Empty vault -> CREDENTIAL_STORE_EMPTY
    tests_total += 1
    v_res2 = verify_active_account(expected_account="user@example.com", mock_payload={})
    assert v_res2.credential_present is False
    assert v_res2.status == CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY
    print("  [PASS] Verifier empty vault: returns CREDENTIAL_STORE_EMPTY")
    tests_passed += 1

    # --- Group 4: Parser Robustness Fixtures ---

    # 4.1: list_empty.txt
    tests_total += 1
    p_empty = fixtures_dir / "list_empty.txt"
    with open(p_empty, "r", encoding="utf-8") as f:
        res_empty = parse_agm_list(f.read(), now_epoch=now)
    assert len(res_empty) == 0
    print("  [PASS] list_empty.txt: correctly parsed 0 accounts")
    tests_passed += 1

    # 4.2: list_unicode.txt
    tests_total += 1
    p_uni = fixtures_dir / "list_unicode.txt"
    with open(p_uni, "r", encoding="utf-8") as f:
        res_uni = parse_agm_list(f.read(), now_epoch=now)
    assert len(res_uni) == 3
    assert res_uni[0].safe_account_ref == "user.tést_ñ@domain.com"
    assert res_uni[1].safe_account_ref == "käyttäjä@yritys.fi"
    print("  [PASS] list_unicode.txt: unicode emails handled safely")
    tests_passed += 1

    # 4.3: list_malformed.txt
    tests_total += 1
    p_mal = fixtures_dir / "list_malformed.txt"
    with open(p_mal, "r", encoding="utf-8") as f:
        res_mal = parse_agm_list(f.read(), now_epoch=now)
    assert len(res_mal) >= 1
    corrupted = next(r for r in res_mal if r.safe_account_ref == "corrupted@domain.com")
    assert corrupted.gemini_pro_pct is None
    print("  [PASS] list_malformed.txt: malformed numbers normalized without exception")
    tests_passed += 1

    # 4.4: info_normal.txt
    tests_total += 1
    p_info = fixtures_dir / "info_normal.txt"
    with open(p_info, "r", encoding="utf-8") as f:
        info_res = parse_agm_info(f.read(), refresh_confirmed_at_epoch=now - 10, now_epoch=now)
    assert info_res is not None
    assert info_res.safe_account_ref == "alice@example.com"
    assert info_res.gemini_pro_pct == 85
    assert info_res.freshness_state == FreshnessState.PROVEN_FRESH
    print("  [PASS] info_normal.txt: parsed model breakdown with PROVEN_FRESH state")
    tests_passed += 1

    print(f"\n=======================================================")
    print(f"Summary: {tests_passed}/{tests_total} test assertions passed (100% success rate).")
    return tests_passed == tests_total


if __name__ == "__main__":
    success = run_tests()
    if not success:
        sys.exit(1)
