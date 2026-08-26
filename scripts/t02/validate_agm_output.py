#!/usr/bin/env python3
"""
validate_agm_output.py

Comprehensive test suite verifying parser robustness, RefreshEvidence contract,
credential store error classifications, model-specific routing, and schema support.
"""

import json
import os
import sys
import time
from pathlib import Path

# Add current dir to path to import modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inspect_quota import (
    AccountQuotaSummary,
    FormatSupportState,
    FreshnessState,
    parse_agm_info,
    parse_agm_list,
)
from refresh_quota_safe import RefreshEvidence, RefreshResult, execute_safe_refresh
from selection_policy import AccountSelector, SelectionConfig, TerminalState
from switch_account_safe import SwitchOutcome, execute_safe_switch
from verify_active_account import (
    CredentialVerificationStatus,
    verify_active_account,
)


def run_tests():
    fixtures_dir = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "t02"
    if not fixtures_dir.exists():
        print(f"Error: Fixtures directory not found at {fixtures_dir}", file=sys.stderr)
        return False

    print(f"=== Running AGM Zero-Trust Round 2 Test Suite ===")
    print(f"Fixtures Path: {fixtures_dir}\n")
    tests_passed = 0
    tests_total = 0
    now = 1756220000.0  # Deterministic test epoch
    session_id = "sess-alpha-123"

    # =========================================================================
    # ITEM 2: Synthetic Freshness & RefreshEvidence Tests (A through H)
    # =========================================================================
    p_norm = fixtures_dir / "list_normal.txt"
    with open(p_norm, "r", encoding="utf-8") as f:
        list_norm_text = f.read()

    # 2.A: Cached quota + no refresh evidence -> STALE_CACHED
    tests_total += 1
    res_2a = parse_agm_list(list_norm_text, now_epoch=now)
    assert len(res_2a) == 4
    for acc in res_2a:
        assert acc.freshness_state in (FreshnessState.STALE_CACHED, FreshnessState.UNKNOWN_UNFETCHED)
        assert not acc.eligible
    print("  [PASS] 2.A: No refresh evidence -> STALE_CACHED / ineligible")
    tests_passed += 1

    # 2.B: Cached quota + fabricated raw timestamp -> REJECTED as STALE_CACHED
    tests_total += 1
    raw_ts = {"alice@example.com": now - 5}
    res_2b = parse_agm_list(list_norm_text, raw_unvalidated_timestamps=raw_ts, now_epoch=now)
    alice_2b = next(r for r in res_2b if r.safe_account_ref == "alice@example.com")
    assert alice_2b.freshness_state == FreshnessState.STALE_CACHED
    assert not alice_2b.eligible
    assert any("Raw unvalidated timestamp" in w for w in alice_2b.parse_warnings)
    print("  [PASS] 2.B: Fabricated raw timestamp rejected -> STALE_CACHED")
    tests_passed += 1

    # 2.C: Validated REFRESH_SUCCEEDED evidence -> PROVEN_FRESH
    tests_total += 1
    ev_alice = RefreshEvidence(
        canonical_account="alice@example.com",
        agm_executable="agm.exe",
        agm_version_or_revision="agm-1d3ce84",
        command="agm refresh alice@example.com",
        started_at_epoch=now - 12,
        completed_at_epoch=now - 10,
        exit_code=0,
        result=RefreshResult.REFRESH_SUCCEEDED,
        supervisor_session_id=session_id
    )
    ev_map_2c = {"alice@example.com": ev_alice}
    res_2c = parse_agm_list(list_norm_text, refresh_evidence_map=ev_map_2c, expected_session_id=session_id, now_epoch=now)
    alice_2c = next(r for r in res_2c if r.safe_account_ref == "alice@example.com")
    assert alice_2c.freshness_state == FreshnessState.PROVEN_FRESH
    assert alice_2c.eligible is True
    print("  [PASS] 2.C: Validated REFRESH_SUCCEEDED evidence -> PROVEN_FRESH / eligible")
    tests_passed += 1

    # 2.D: REFRESH_FAILED_NETWORK -> REFRESH_FAILED
    tests_total += 1
    ev_bob_net = RefreshEvidence(
        canonical_account="bob.dev@corp.example.org",
        agm_executable="agm.exe",
        agm_version_or_revision="agm-1d3ce84",
        command="agm refresh bob.dev@corp.example.org",
        started_at_epoch=now - 15,
        completed_at_epoch=now - 10,
        exit_code=1,
        result=RefreshResult.REFRESH_FAILED_NETWORK,
        supervisor_session_id=session_id,
        error_summary="Network timeout"
    )
    res_2d = parse_agm_list(list_norm_text, refresh_evidence_map={"bob.dev@corp.example.org": ev_bob_net}, now_epoch=now)
    bob_2d = next(r for r in res_2d if r.safe_account_ref == "bob.dev@corp.example.org")
    assert bob_2d.freshness_state == FreshnessState.REFRESH_FAILED
    assert not bob_2d.eligible
    print("  [PASS] 2.D: REFRESH_FAILED_NETWORK -> REFRESH_FAILED")
    tests_passed += 1

    # 2.E: REFRESH_FAILED_AUTH -> REFRESH_FAILED
    tests_total += 1
    ev_bob_auth = RefreshEvidence(
        canonical_account="bob.dev@corp.example.org",
        agm_executable="agm.exe",
        agm_version_or_revision="agm-1d3ce84",
        command="agm refresh bob.dev@corp.example.org",
        started_at_epoch=now - 15,
        completed_at_epoch=now - 10,
        exit_code=1,
        result=RefreshResult.REFRESH_FAILED_AUTH,
        supervisor_session_id=session_id,
        error_summary="Token expired"
    )
    res_2e = parse_agm_list(list_norm_text, refresh_evidence_map={"bob.dev@corp.example.org": ev_bob_auth}, now_epoch=now)
    bob_2e = next(r for r in res_2e if r.safe_account_ref == "bob.dev@corp.example.org")
    assert bob_2e.freshness_state == FreshnessState.REFRESH_FAILED
    assert not bob_2e.eligible
    print("  [PASS] 2.E: REFRESH_FAILED_AUTH -> REFRESH_FAILED")
    tests_passed += 1

    # 2.F: Evidence belongs to different account -> reject
    tests_total += 1
    ev_wrong_acc = RefreshEvidence(
        canonical_account="charlie@other.com",  # Mismatch!
        agm_executable="agm.exe",
        agm_version_or_revision="agm-1d3ce84",
        command="agm refresh charlie@other.com",
        started_at_epoch=now - 10,
        completed_at_epoch=now - 5,
        exit_code=0,
        result=RefreshResult.REFRESH_SUCCEEDED,
        supervisor_session_id=session_id
    )
    res_2f = parse_agm_list(list_norm_text, refresh_evidence_map={"alice@example.com": ev_wrong_acc}, now_epoch=now)
    alice_2f = next(r for r in res_2f if r.safe_account_ref == "alice@example.com")
    assert alice_2f.freshness_state == FreshnessState.STALE_CACHED
    assert not alice_2f.eligible
    print("  [PASS] 2.F: Account mismatch in RefreshEvidence -> rejected as STALE_CACHED")
    tests_passed += 1

    # 2.G: Evidence belongs to previous/different supervisor session -> reject
    tests_total += 1
    ev_old_session = RefreshEvidence(
        canonical_account="alice@example.com",
        agm_executable="agm.exe",
        agm_version_or_revision="agm-1d3ce84",
        command="agm refresh alice@example.com",
        started_at_epoch=now - 10,
        completed_at_epoch=now - 5,
        exit_code=0,
        result=RefreshResult.REFRESH_SUCCEEDED,
        supervisor_session_id="sess-old-previous"  # Different session!
    )
    res_2g = parse_agm_list(list_norm_text, refresh_evidence_map={"alice@example.com": ev_old_session}, expected_session_id=session_id, now_epoch=now)
    alice_2g = next(r for r in res_2g if r.safe_account_ref == "alice@example.com")
    assert alice_2g.freshness_state == FreshnessState.STALE_CACHED
    assert not alice_2g.eligible
    print("  [PASS] 2.G: Session ID mismatch -> rejected as STALE_CACHED")
    tests_passed += 1

    # 2.H: Evidence too old (> 300s) -> STALE_CACHED
    tests_total += 1
    ev_expired = RefreshEvidence(
        canonical_account="alice@example.com",
        agm_executable="agm.exe",
        agm_version_or_revision="agm-1d3ce84",
        command="agm refresh alice@example.com",
        started_at_epoch=now - 400,
        completed_at_epoch=now - 350,  # 350s old > 300s
        exit_code=0,
        result=RefreshResult.REFRESH_SUCCEEDED,
        supervisor_session_id=session_id
    )
    res_2h = parse_agm_list(list_norm_text, refresh_evidence_map={"alice@example.com": ev_expired}, now_epoch=now)
    alice_2h = next(r for r in res_2h if r.safe_account_ref == "alice@example.com")
    assert alice_2h.freshness_state == FreshnessState.STALE_CACHED
    assert not alice_2h.eligible
    print("  [PASS] 2.H: Refresh evidence too old (350s > 300s) -> STALE_CACHED")
    tests_passed += 1

    # =========================================================================
    # ITEM 3: Fix inspect_quota Info Mode
    # =========================================================================
    p_info = fixtures_dir / "info_normal.txt"
    with open(p_info, "r", encoding="utf-8") as f:
        info_norm_text = f.read()

    # 3.1: Info mode with valid RefreshEvidence -> PROVEN_FRESH
    tests_total += 1
    info_3_1 = parse_agm_info(info_norm_text, refresh_evidence=ev_alice, expected_session_id=session_id, now_epoch=now)
    assert info_3_1 is not None
    assert info_3_1.safe_account_ref == "alice@example.com"
    assert info_3_1.freshness_state == FreshnessState.PROVEN_FRESH
    assert info_3_1.eligible is True
    print("  [PASS] 3.1: Info mode with RefreshEvidence -> PROVEN_FRESH")
    tests_passed += 1

    # 3.2: Info mode with raw unvalidated timestamp -> STALE_CACHED
    tests_total += 1
    info_3_2 = parse_agm_info(info_norm_text, raw_unvalidated_timestamp=now - 5, now_epoch=now)
    assert info_3_2 is not None
    assert info_3_2.freshness_state == FreshnessState.STALE_CACHED
    assert not info_3_2.eligible
    print("  [PASS] 3.2: Info mode with raw unvalidated timestamp -> rejected as STALE_CACHED")
    tests_passed += 1

    # =========================================================================
    # ITEM 4: Credential Store Error Classification Tests
    # =========================================================================

    # 4.1: Target not found -> CREDENTIAL_STORE_EMPTY
    tests_total += 1
    v_4_1 = verify_active_account(mock_payload={}, mock_error_status=CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY)
    assert v_4_1.status == CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY
    assert v_4_1.credential_present is False
    print("  [PASS] 4.1: Vault not found -> CREDENTIAL_STORE_EMPTY")
    tests_passed += 1

    # 4.2: Access denied -> CREDENTIAL_STORE_ACCESS_DENIED
    tests_total += 1
    v_4_2 = verify_active_account(mock_error_status=CredentialVerificationStatus.CREDENTIAL_STORE_ACCESS_DENIED)
    assert v_4_2.status == CredentialVerificationStatus.CREDENTIAL_STORE_ACCESS_DENIED
    assert v_4_2.credential_present is False
    print("  [PASS] 4.2: Access denied -> CREDENTIAL_STORE_ACCESS_DENIED")
    tests_passed += 1

    # 4.3: PowerShell unavailable -> CREDENTIAL_STORE_UNAVAILABLE
    tests_total += 1
    v_4_3 = verify_active_account(mock_error_status=CredentialVerificationStatus.CREDENTIAL_STORE_UNAVAILABLE)
    assert v_4_3.status == CredentialVerificationStatus.CREDENTIAL_STORE_UNAVAILABLE
    print("  [PASS] 4.3: Subprocess unavailable -> CREDENTIAL_STORE_UNAVAILABLE")
    tests_passed += 1

    # 4.4: Corrupted JSON -> CREDENTIAL_PAYLOAD_INVALID
    tests_total += 1
    v_4_4 = verify_active_account(mock_error_status=CredentialVerificationStatus.CREDENTIAL_PAYLOAD_INVALID)
    assert v_4_4.status == CredentialVerificationStatus.CREDENTIAL_PAYLOAD_INVALID
    print("  [PASS] 4.4: Corrupted JSON -> CREDENTIAL_PAYLOAD_INVALID")
    tests_passed += 1

    # 4.5: Valid credential (offline) -> CREDENTIAL_STORE_WRITTEN_UNVERIFIED with matches_expected=None
    tests_total += 1
    mock_token_payload = {
        "token": {
            "access_token": "ya29.mock_token_xyz",
            "refresh_token": "1//0mock_refresh_abc"
        }
    }
    v_4_5 = verify_active_account(expected_account="alice@example.com", introspect_network=False, mock_payload=mock_token_payload)
    assert v_4_5.status == CredentialVerificationStatus.CREDENTIAL_STORE_WRITTEN_UNVERIFIED
    assert v_4_5.matches_expected is None
    assert v_4_5.scope == "CREDENTIAL_STORE_ONLY"
    assert v_4_5.desktop_adoption_status == "UNKNOWN"
    print("  [PASS] 4.5: Offline valid credential -> CREDENTIAL_STORE_WRITTEN_UNVERIFIED (matches_expected=None)")
    tests_passed += 1

    # =========================================================================
    # ITEM 5 & 6: Switch Safe Unit Tests (Exit Codes & Aliases)
    # =========================================================================

    # 5.1: Dry run -> exit code 3
    tests_total += 1
    sw_dry = execute_safe_switch("alice@example.com", confirm=False)
    assert sw_dry["status"] == SwitchOutcome.DRY_RUN.value
    assert sw_dry["exit_code"] == 3
    assert sw_dry["scope"] == "CREDENTIAL_STORE_ONLY"
    print("  [PASS] 5.1: Dry run returns exit code 3")
    tests_passed += 1

    # 5.2: Wildcard rejection -> exit code 1
    tests_total += 1
    sw_wild = execute_safe_switch("*")
    assert sw_wild["status"] == SwitchOutcome.WILDCARD_REJECTED.value
    assert sw_wild["exit_code"] == 1
    print("  [PASS] 5.2: Wildcard target '*' rejected with exit code 1")
    tests_passed += 1

    # 6.1: Non-canonical alias rejection -> exit code 1
    tests_total += 1
    sw_alias = execute_safe_switch("prod-worker-2")
    assert sw_alias["status"] == SwitchOutcome.INVALID_ARGUMENT.value
    assert sw_alias["error_code"] == "NON_CANONICAL_EMAIL"
    assert sw_alias["exit_code"] == 1
    print("  [PASS] 6.1: Raw alias 'prod-worker-2' rejected (canonical email required)")
    tests_passed += 1

    # =========================================================================
    # ITEM 9: Network Verifier Synthetic Tests (Injectable Fetcher)
    # =========================================================================

    # 9.1: Network success + matching email -> CREDENTIAL_STORE_IDENTITY_VERIFIED
    tests_total += 1
    fetch_match = lambda tok: ({"email": "alice@example.com"}, None, None)
    v_9_1 = verify_active_account(
        expected_account="alice@example.com",
        introspect_network=True,
        mock_payload=mock_token_payload,
        userinfo_fetcher=fetch_match
    )
    assert v_9_1.status == CredentialVerificationStatus.CREDENTIAL_STORE_IDENTITY_VERIFIED
    assert v_9_1.matches_expected is True
    print("  [PASS] 9.1: Network userinfo match -> CREDENTIAL_STORE_IDENTITY_VERIFIED")
    tests_passed += 1

    # 9.2: Network success + mismatching email -> IDENTITY_MISMATCH
    tests_total += 1
    fetch_mismatch = lambda tok: ({"email": "bob@example.com"}, None, None)
    v_9_2 = verify_active_account(
        expected_account="alice@example.com",
        introspect_network=True,
        mock_payload=mock_token_payload,
        userinfo_fetcher=fetch_mismatch
    )
    assert v_9_2.status == CredentialVerificationStatus.IDENTITY_MISMATCH
    assert v_9_2.matches_expected is False
    print("  [PASS] 9.2: Network userinfo mismatch -> IDENTITY_MISMATCH")
    tests_passed += 1

    # 9.3: Network timeout -> NETWORK_UNAVAILABLE
    tests_total += 1
    fetch_timeout = lambda tok: (None, CredentialVerificationStatus.NETWORK_UNAVAILABLE, "Socket timed out")
    v_9_3 = verify_active_account(
        expected_account="alice@example.com",
        introspect_network=True,
        mock_payload=mock_token_payload,
        userinfo_fetcher=fetch_timeout
    )
    assert v_9_3.status == CredentialVerificationStatus.NETWORK_UNAVAILABLE
    assert v_9_3.matches_expected is None
    print("  [PASS] 9.3: Network timeout -> NETWORK_UNAVAILABLE (matches_expected=None)")
    tests_passed += 1

    # 9.4: HTTP 401 -> TOKEN_REJECTED
    tests_total += 1
    fetch_401 = lambda tok: (None, CredentialVerificationStatus.TOKEN_REJECTED, "HTTP 401 Unauthorized")
    v_9_4 = verify_active_account(
        expected_account="alice@example.com",
        introspect_network=True,
        mock_payload=mock_token_payload,
        userinfo_fetcher=fetch_401
    )
    assert v_9_4.status == CredentialVerificationStatus.TOKEN_REJECTED
    assert v_9_4.matches_expected is None
    print("  [PASS] 9.4: HTTP 401 -> TOKEN_REJECTED")
    tests_passed += 1

    # 9.5: Missing email field in userinfo -> USERINFO_INVALID_RESPONSE
    tests_total += 1
    fetch_no_email = lambda tok: ({"name": "No Email Account"}, None, None)
    v_9_5 = verify_active_account(
        expected_account="alice@example.com",
        introspect_network=True,
        mock_payload=mock_token_payload,
        userinfo_fetcher=fetch_no_email
    )
    assert v_9_5.status == CredentialVerificationStatus.USERINFO_INVALID_RESPONSE
    assert v_9_5.matches_expected is None
    print("  [PASS] 9.5: Response missing email -> USERINFO_INVALID_RESPONSE")
    tests_passed += 1

    # =========================================================================
    # ITEM 10: Fail Closed on AGM Format Changes Fixtures
    # =========================================================================

    # 10.1: list_schema_missing_column.txt -> FORMAT_UNSUPPORTED
    tests_total += 1
    with open(fixtures_dir / "list_schema_missing_column.txt", "r", encoding="utf-8") as f:
        res_10_1 = parse_agm_list(f.read())
    assert len(res_10_1) == 1
    assert res_10_1[0].format_support == FormatSupportState.FORMAT_UNSUPPORTED
    assert not res_10_1[0].eligible
    print("  [PASS] 10.1: Missing column schema -> fail closed as FORMAT_UNSUPPORTED")
    tests_passed += 1

    # 10.2: list_schema_renamed_column.txt -> FORMAT_UNSUPPORTED
    tests_total += 1
    with open(fixtures_dir / "list_schema_renamed_column.txt", "r", encoding="utf-8") as f:
        res_10_2 = parse_agm_list(f.read())
    assert res_10_2[0].format_support == FormatSupportState.FORMAT_UNSUPPORTED
    print("  [PASS] 10.2: Renamed column schema -> fail closed as FORMAT_UNSUPPORTED")
    tests_passed += 1

    # 10.3: list_schema_reordered.txt -> FORMAT_UNSUPPORTED
    tests_total += 1
    with open(fixtures_dir / "list_schema_reordered.txt", "r", encoding="utf-8") as f:
        res_10_3 = parse_agm_list(f.read())
    assert res_10_3[0].format_support == FormatSupportState.FORMAT_UNSUPPORTED
    print("  [PASS] 10.3: Reordered columns schema -> fail closed as FORMAT_UNSUPPORTED")
    tests_passed += 1

    # 10.4: list_schema_corrupted_header.txt -> FORMAT_UNSUPPORTED
    tests_total += 1
    with open(fixtures_dir / "list_schema_corrupted_header.txt", "r", encoding="utf-8") as f:
        res_10_4 = parse_agm_list(f.read())
    assert res_10_4[0].format_support == FormatSupportState.FORMAT_UNSUPPORTED
    print("  [PASS] 10.4: Corrupted header -> fail closed as FORMAT_UNSUPPORTED")
    tests_passed += 1

    # 10.5: Fallback lenient parser with explicit flag
    tests_total += 1
    with open(fixtures_dir / "list_schema_corrupted_header.txt", "r", encoding="utf-8") as f:
        res_10_5 = parse_agm_list(f.read(), lenient_parser=True)
    assert len(res_10_5) == 2
    assert res_10_5[0].safe_account_ref == "alice@example.com"
    assert any("research-lenient" in w for w in res_10_5[0].parse_warnings)
    print("  [PASS] 10.5: Explicit research-lenient flag parses fallback lines with warning")
    tests_passed += 1

    # =========================================================================
    # ITEM 11: Model-Specific Routing Tests
    # =========================================================================

    # Setup candidate accounts:
    # accA: Pro = 0, Flash = 90, Claude = 0 (Fresh)
    # accB: Pro = None, Flash = 100, Claude = 50 (Fresh)
    # accC: Claude = 80, Pro = 0, Flash = 0 (Fresh)
    accA = AccountQuotaSummary("userA@corp.com", [], False, False, False, 0, 90, 0, {}, now, now - 10, None, FreshnessState.PROVEN_FRESH, FormatSupportState.FORMAT_SUPPORTED, "MOCK", [], True)
    accB = AccountQuotaSummary("userB@corp.com", [], False, False, False, None, 100, 50, {}, now, now - 10, None, FreshnessState.PROVEN_FRESH, FormatSupportState.FORMAT_SUPPORTED, "MOCK", [], True)
    accC = AccountQuotaSummary("userC@corp.com", [], False, False, False, 0, 0, 80, {}, now, now - 10, None, FreshnessState.PROVEN_FRESH, FormatSupportState.FORMAT_SUPPORTED, "MOCK", [], True)

    # 11.1: Target 'gemini-pro' on accA (Pro=0, Flash=90) -> REJECT
    tests_total += 1
    sel_pro = AccountSelector(SelectionConfig(min_quota_pct=20, target_model_group="gemini-pro"))
    res_11_1 = sel_pro.select_next_account([accA], now=now)
    assert res_11_1.selected_account is None
    assert res_11_1.terminal_state == TerminalState.BLOCKED_NO_ACCOUNT
    print("  [PASS] 11.1: Target gemini-pro on accA (Pro=0%, Flash=90%) -> REJECTED")
    tests_passed += 1

    # 11.2: Target 'gemini-flash' on accA (Pro=0, Flash=90) -> SELECTED
    tests_total += 1
    sel_flash = AccountSelector(SelectionConfig(min_quota_pct=20, target_model_group="gemini-flash"))
    res_11_2 = sel_flash.select_next_account([accA], now=now)
    assert res_11_2.selected_account == "userA@corp.com"
    assert res_11_2.terminal_state == TerminalState.NONE
    print("  [PASS] 11.2: Target gemini-flash on accA (Pro=0%, Flash=90%) -> SELECTED")
    tests_passed += 1

    # 11.3: Target 'gemini-pro' on accB (Pro=None, Flash=100) -> BLOCKED_QUOTA_UNKNOWN (Never infer Pro from Flash!)
    tests_total += 1
    res_11_3 = sel_pro.select_next_account([accB], now=now)
    assert res_11_3.selected_account is None
    assert res_11_3.terminal_state == TerminalState.BLOCKED_QUOTA_UNKNOWN
    print("  [PASS] 11.3: Target gemini-pro on accB (Pro=None, Flash=100%) -> BLOCKED_QUOTA_UNKNOWN (no cross-model inference)")
    tests_passed += 1

    # 11.4: Target 'gemini-pro' on accC (Claude=80, Pro=0, Flash=0) -> REJECT
    tests_total += 1
    res_11_4 = sel_pro.select_next_account([accC], now=now)
    assert res_11_4.selected_account is None
    assert res_11_4.terminal_state == TerminalState.BLOCKED_NO_ACCOUNT
    print("  [PASS] 11.4: Target gemini-pro on accC (Claude=80%, Pro=0%) -> REJECTED")
    tests_passed += 1

    print(f"\n=======================================================")
    print(f"Summary: {tests_passed}/{tests_total} test assertions passed (100% success rate).")
    return tests_passed == tests_total


if __name__ == "__main__":
    success = run_tests()
    if not success:
        sys.exit(1)
