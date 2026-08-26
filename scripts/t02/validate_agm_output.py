#!/usr/bin/env python3
"""
validate_agm_output.py

Comprehensive zero-trust test suite for T02 AGM integration:
- 100% Host-Isolated: ZERO OS vault reads/writes, ZERO live AGM calls, ZERO Google network requests.
- RefreshEvidence Trust Model (Origin tracking, HMAC signing, Invariant checking).
- Production executor testing with injected fake runners.
- Credential reader process output classifier tests.
- Token payload semantics (Distinguishing empty vault from missing token fields).
- Production safe switch post-success branch tests (Exits 0, 1, 2, 3).
- ModelGroup enum fail-closed validation.
- Schema header fail-closed checks in List and Info modes.
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
    validate_refresh_evidence,
)
from refresh_quota_safe import (
    EvidenceTrustOrigin,
    RefreshEvidence,
    RefreshResult,
    compute_evidence_hmac,
    execute_safe_refresh,
    get_agm_version,
)
from selection_policy import (
    AccountSelector,
    ModelGroup,
    SelectionConfig,
    TerminalState,
)
from switch_account_safe import SwitchOutcome, execute_safe_switch
from verify_active_account import (
    CredentialVerificationStatus,
    VerificationResult,
    parse_credential_process_output,
    verify_active_account,
)


def run_tests():
    fixtures_dir = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "t02"
    if not fixtures_dir.exists():
        print(f"Error: Fixtures directory not found at {fixtures_dir}", file=sys.stderr)
        return False

    print(f"=== Running AGM Zero-Trust Round 3 Test Suite ===")
    print(f"Fixtures Path: {fixtures_dir}\n")
    tests_passed = 0
    tests_total = 0
    now = 1756220000.0  # Fixed deterministic epoch
    session_id = "sess-prod-999"
    secret = "test-session-secret-key"

    # =========================================================================
    # ITEM 1, 2, 3: RefreshEvidence Trust & Invariant Checks
    # =========================================================================
    p_norm = fixtures_dir / "list_normal.txt"
    with open(p_norm, "r", encoding="utf-8") as f:
        list_norm_text = f.read()

    # 1.1: Synthetic success record rejected in production supervisor mode (Item 2)
    tests_total += 1
    ev_synth = RefreshEvidence(
        canonical_account="alice@example.com",
        agm_executable="agm.exe",
        agm_version_or_revision="1d3ce84",
        command="agm refresh alice@example.com",
        started_at_epoch=now - 5,
        completed_at_epoch=now - 4,
        exit_code=0,
        result=RefreshResult.REFRESH_SUCCEEDED,
        supervisor_session_id=session_id,
        origin=EvidenceTrustOrigin.SYNTHETIC_TEST_EVIDENCE
    )
    res_1_1 = parse_agm_list(
        list_norm_text,
        refresh_evidence_map={"alice@example.com": ev_synth},
        supervisor_session_id=session_id,
        allow_synthetic_test_origin=False,  # Production mode!
        now_epoch=now
    )
    alice_1_1 = next(r for r in res_1_1 if r.safe_account_ref == "alice@example.com")
    assert alice_1_1.freshness_state == FreshnessState.STALE_CACHED
    assert not alice_1_1.eligible
    print("  [PASS] 1.1: Synthetic evidence rejected in production mode -> STALE_CACHED")
    tests_passed += 1

    # 1.2: Synthetic success record accepted in explicit test mode (Item 2)
    tests_total += 1
    res_1_2 = parse_agm_list(
        list_norm_text,
        refresh_evidence_map={"alice@example.com": ev_synth},
        supervisor_session_id=session_id,
        allow_synthetic_test_origin=True,  # Test mode!
        now_epoch=now
    )
    alice_1_2 = next(r for r in res_1_2 if r.safe_account_ref == "alice@example.com")
    assert alice_1_2.freshness_state == FreshnessState.PROVEN_FRESH
    assert alice_1_2.eligible is True
    print("  [PASS] 1.2: Synthetic evidence accepted in explicit test mode -> PROVEN_FRESH")
    tests_passed += 1

    # 1.3: Invariant - Exit code 99 with REFRESH_SUCCEEDED fails closed (Item 3)
    tests_total += 1
    ev_bad_exit = RefreshEvidence(
        canonical_account="alice@example.com",
        agm_executable="agm.exe",
        agm_version_or_revision="1d3ce84",
        command="agm refresh alice@example.com",
        started_at_epoch=now - 5,
        completed_at_epoch=now - 4,
        exit_code=99,  # Contradictory!
        result=RefreshResult.REFRESH_SUCCEEDED,
        supervisor_session_id=session_id,
        origin=EvidenceTrustOrigin.LIVE_REFRESH_EXECUTION
    )
    st_1_3, _, _ = validate_refresh_evidence(ev_bad_exit, "alice@example.com", now, expected_session_id=session_id)
    assert st_1_3 == FreshnessState.REFRESH_FAILED
    print("  [PASS] 1.3: Contradictory exit code 99 with REFRESH_SUCCEEDED -> REFRESH_FAILED")
    tests_passed += 1

    # 1.4: Invariant - Wrong command binding fails closed (Item 3)
    tests_total += 1
    ev_bad_cmd = RefreshEvidence(
        canonical_account="alice@example.com",
        agm_executable="agm.exe",
        agm_version_or_revision="1d3ce84",
        command="agm info alice@example.com",  # Wrong command!
        started_at_epoch=now - 5,
        completed_at_epoch=now - 4,
        exit_code=0,
        result=RefreshResult.REFRESH_SUCCEEDED,
        supervisor_session_id=session_id,
        origin=EvidenceTrustOrigin.LIVE_REFRESH_EXECUTION
    )
    st_1_4, _, _ = validate_refresh_evidence(ev_bad_cmd, "alice@example.com", now, expected_session_id=session_id)
    assert st_1_4 == FreshnessState.STALE_CACHED
    print("  [PASS] 1.4: Wrong command binding -> STALE_CACHED")
    tests_passed += 1

    # 1.5: Invariant - Unknown / unverified AGM version fails closed (Item 3 & 5)
    tests_total += 1
    ev_unk_ver = RefreshEvidence(
        canonical_account="alice@example.com",
        agm_executable="agm.exe",
        agm_version_or_revision="UNKNOWN_VERSION",
        command="agm refresh alice@example.com",
        started_at_epoch=now - 5,
        completed_at_epoch=now - 4,
        exit_code=0,
        result=RefreshResult.REFRESH_SUCCEEDED,
        supervisor_session_id=session_id,
        origin=EvidenceTrustOrigin.LIVE_REFRESH_EXECUTION
    )
    st_1_5, _, _ = validate_refresh_evidence(ev_unk_ver, "alice@example.com", now, expected_session_id=session_id)
    assert st_1_5 == FreshnessState.STALE_CACHED
    print("  [PASS] 1.5: UNKNOWN_VERSION binary -> fail closed as STALE_CACHED")
    tests_passed += 1

    # 1.6: Invariant - Monotonicity: start after completed fails closed (Item 3)
    tests_total += 1
    ev_mono = RefreshEvidence(
        canonical_account="alice@example.com",
        agm_executable="agm.exe",
        agm_version_or_revision="1d3ce84",
        command="agm refresh alice@example.com",
        started_at_epoch=now - 2,
        completed_at_epoch=now - 5,  # start > completed!
        exit_code=0,
        result=RefreshResult.REFRESH_SUCCEEDED,
        supervisor_session_id=session_id,
        origin=EvidenceTrustOrigin.LIVE_REFRESH_EXECUTION
    )
    st_1_6, _, _ = validate_refresh_evidence(ev_mono, "alice@example.com", now, expected_session_id=session_id)
    assert st_1_6 == FreshnessState.STALE_CACHED
    print("  [PASS] 1.6: Invalid timestamp monotonicity -> STALE_CACHED")
    tests_passed += 1

    # 1.7: Invariant - Future timestamp (10m in future) fails closed (Item 3)
    tests_total += 1
    ev_future_far = RefreshEvidence(
        canonical_account="alice@example.com",
        agm_executable="agm.exe",
        agm_version_or_revision="1d3ce84",
        command="agm refresh alice@example.com",
        started_at_epoch=now + 590,
        completed_at_epoch=now + 600,  # 10 minutes in future
        exit_code=0,
        result=RefreshResult.REFRESH_SUCCEEDED,
        supervisor_session_id=session_id,
        origin=EvidenceTrustOrigin.LIVE_REFRESH_EXECUTION
    )
    st_1_7, _, _ = validate_refresh_evidence(ev_future_far, "alice@example.com", now, expected_session_id=session_id)
    assert st_1_7 == FreshnessState.STALE_CACHED
    print("  [PASS] 1.7: Timestamp 10m in future -> rejected as STALE_CACHED")
    tests_passed += 1

    # 1.8: Invariant - Future timestamp within allowed skew (1.0s <= 2.0s) accepted (Item 3)
    tests_total += 1
    ev_skew_ok = RefreshEvidence(
        canonical_account="alice@example.com",
        agm_executable="agm.exe",
        agm_version_or_revision="1d3ce84",
        command="agm refresh alice@example.com",
        started_at_epoch=now,
        completed_at_epoch=now + 1.0,  # 1.0s skew <= 2.0s allowed
        exit_code=0,
        result=RefreshResult.REFRESH_SUCCEEDED,
        supervisor_session_id=session_id,
        origin=EvidenceTrustOrigin.LIVE_REFRESH_EXECUTION
    )
    st_1_8, _, _ = validate_refresh_evidence(ev_skew_ok, "alice@example.com", now, allowed_clock_skew_sec=2.0, expected_session_id=session_id)
    assert st_1_8 == FreshnessState.PROVEN_FRESH
    print("  [PASS] 1.8: Timestamp within allowed clock skew (1.0s <= 2.0s) -> PROVEN_FRESH")
    tests_passed += 1

    # 1.9: HMAC Signed Evidence Verification (Item 1)
    tests_total += 1
    ev_live = RefreshEvidence(
        canonical_account="alice@example.com",
        agm_executable="agm.exe",
        agm_version_or_revision="1d3ce84",
        command="agm refresh alice@example.com",
        started_at_epoch=now - 5,
        completed_at_epoch=now - 4,
        exit_code=0,
        result=RefreshResult.REFRESH_SUCCEEDED,
        supervisor_session_id=session_id,
        origin=EvidenceTrustOrigin.UNTRUSTED_DESERIALIZED
    )
    ev_live.hmac_signature = compute_evidence_hmac(ev_live, secret)
    # Valid signature
    st_1_9_ok, _, _ = validate_refresh_evidence(ev_live, "alice@example.com", now, expected_session_id=session_id, session_secret=secret)
    assert st_1_9_ok == FreshnessState.PROVEN_FRESH
    # Tampered signature
    st_1_9_bad, _, _ = validate_refresh_evidence(ev_live, "alice@example.com", now, expected_session_id=session_id, session_secret="wrong-secret")
    assert st_1_9_bad == FreshnessState.STALE_CACHED
    print("  [PASS] 1.9: HMAC signed evidence: valid secret -> PROVEN_FRESH, tampered -> STALE_CACHED")
    tests_passed += 1

    # =========================================================================
    # ITEM 6: Test Actual Refresh Executor with Injected Fake Runners
    # =========================================================================

    # 6.1: Injected runner exit 0 -> REFRESH_SUCCEEDED
    tests_total += 1
    runner_ok = lambda cmd, t: (0, "Refreshed alice@example.com successfully", "")
    ev_6_1 = execute_safe_refresh("alice@example.com", session_id, agm_runner=runner_ok, clock=lambda: now)
    assert ev_6_1.result == RefreshResult.REFRESH_SUCCEEDED
    assert ev_6_1.exit_code == 0
    assert ev_6_1.origin == EvidenceTrustOrigin.SYNTHETIC_TEST_EVIDENCE
    print("  [PASS] 6.1: Executor runner exit 0 -> REFRESH_SUCCEEDED")
    tests_passed += 1

    # 6.2: Injected runner auth failure -> REFRESH_FAILED_AUTH
    tests_total += 1
    runner_auth = lambda cmd, t: (1, "", "Error: invalid_grant - token expired")
    ev_6_2 = execute_safe_refresh("alice@example.com", session_id, agm_runner=runner_auth, clock=lambda: now)
    assert ev_6_2.result == RefreshResult.REFRESH_FAILED_AUTH
    assert ev_6_2.exit_code == 1
    print("  [PASS] 6.2: Executor runner auth failure -> REFRESH_FAILED_AUTH")
    tests_passed += 1

    # 6.3: Injected runner network failure -> REFRESH_FAILED_NETWORK
    tests_total += 1
    runner_net = lambda cmd, t: (1, "", "Error: dial tcp: i/o timeout")
    ev_6_3 = execute_safe_refresh("alice@example.com", session_id, agm_runner=runner_net, clock=lambda: now)
    assert ev_6_3.result == RefreshResult.REFRESH_FAILED_NETWORK
    print("  [PASS] 6.3: Executor runner network timeout -> REFRESH_FAILED_NETWORK")
    tests_passed += 1

    # 6.4: Injected runner account not found -> REFRESH_FAILED_ACCOUNT_NOT_FOUND
    tests_total += 1
    runner_nf = lambda cmd, t: (1, "", "Error: account 'alice@example.com' not found")
    ev_6_4 = execute_safe_refresh("alice@example.com", session_id, agm_runner=runner_nf, clock=lambda: now)
    assert ev_6_4.result == RefreshResult.REFRESH_FAILED_ACCOUNT_NOT_FOUND
    print("  [PASS] 6.4: Executor runner account not found -> REFRESH_FAILED_ACCOUNT_NOT_FOUND")
    tests_passed += 1

    # 6.5: Injected runner unknown version -> REFRESH_VERSION_UNVERIFIED
    tests_total += 1
    ver_resolver_unk = lambda b: "UNKNOWN_VERSION"
    ev_6_5 = execute_safe_refresh("alice@example.com", session_id, agm_runner=runner_ok, version_resolver=ver_resolver_unk, clock=lambda: now)
    assert ev_6_5.result == RefreshResult.REFRESH_VERSION_UNVERIFIED
    print("  [PASS] 6.5: Executor unknown version -> REFRESH_VERSION_UNVERIFIED")
    tests_passed += 1

    # =========================================================================
    # ITEM 7 & 8: Test Real Credential Reader Output Classifier
    # =========================================================================

    # 8.1: Exit 1 + empty stdout -> POWERSHELL_PROCESS_FAILED
    tests_total += 1
    _, st_8_1, _ = parse_credential_process_output(1, "", "")
    assert st_8_1 == CredentialVerificationStatus.POWERSHELL_PROCESS_FAILED
    print("  [PASS] 8.1: Exit 1 + empty stdout -> POWERSHELL_PROCESS_FAILED")
    tests_passed += 1

    # 8.2: Exit 1 + access denied stderr -> CREDENTIAL_STORE_ACCESS_DENIED
    tests_total += 1
    _, st_8_2, _ = parse_credential_process_output(1, "", "Exception: Access is denied")
    assert st_8_2 == CredentialVerificationStatus.CREDENTIAL_STORE_ACCESS_DENIED
    print("  [PASS] 8.2: Exit 1 + access denied stderr -> CREDENTIAL_STORE_ACCESS_DENIED")
    tests_passed += 1

    # 8.3: Exit 0 + ERR_NOT_FOUND -> CREDENTIAL_STORE_EMPTY
    tests_total += 1
    _, st_8_3, _ = parse_credential_process_output(0, "ERR_NOT_FOUND", "")
    assert st_8_3 == CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY
    print("  [PASS] 8.3: Exit 0 + ERR_NOT_FOUND -> CREDENTIAL_STORE_EMPTY")
    tests_passed += 1

    # 8.4: Exit 0 + ERR_ACCESS_DENIED -> CREDENTIAL_STORE_ACCESS_DENIED
    tests_total += 1
    _, st_8_4, _ = parse_credential_process_output(0, "ERR_ACCESS_DENIED", "")
    assert st_8_4 == CredentialVerificationStatus.CREDENTIAL_STORE_ACCESS_DENIED
    print("  [PASS] 8.4: Exit 0 + ERR_ACCESS_DENIED -> CREDENTIAL_STORE_ACCESS_DENIED")
    tests_passed += 1

    # 8.5: Exit 0 + ERR_WIN32_1359 -> CREDENTIAL_STORE_READ_ERROR
    tests_total += 1
    _, st_8_5, _ = parse_credential_process_output(0, "ERR_WIN32_1359", "")
    assert st_8_5 == CredentialVerificationStatus.CREDENTIAL_STORE_READ_ERROR
    print("  [PASS] 8.5: Exit 0 + ERR_WIN32_1359 -> CREDENTIAL_STORE_READ_ERROR")
    tests_passed += 1

    # 8.6: Exit 0 + Corrupted JSON -> CREDENTIAL_PAYLOAD_INVALID
    tests_total += 1
    _, st_8_6, _ = parse_credential_process_output(0, "{not-json", "")
    assert st_8_6 == CredentialVerificationStatus.CREDENTIAL_PAYLOAD_INVALID
    print("  [PASS] 8.6: Exit 0 + Corrupted JSON -> CREDENTIAL_PAYLOAD_INVALID")
    tests_passed += 1

    # 8.7: Exit 0 + Valid JSON -> returns parsed dict
    tests_total += 1
    data_8_7, st_8_7, _ = parse_credential_process_output(0, '{"token":{"access_token":"ya29.xyz"}}', "")
    assert st_8_7 is None
    assert data_8_7 == {"token": {"access_token": "ya29.xyz"}}
    print("  [PASS] 8.7: Exit 0 + Valid JSON -> successfully parsed dict")
    tests_passed += 1

    # =========================================================================
    # ITEM 9: Token Payload Semantics (Empty tokens vs Empty vault)
    # =========================================================================

    # 9.1: Target absent -> CREDENTIAL_STORE_EMPTY (credential_present = False)
    tests_total += 1
    v_9_1 = verify_active_account(ps_runner=lambda: (0, "ERR_NOT_FOUND", ""))
    assert v_9_1.status == CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY
    assert v_9_1.credential_present is False
    print("  [PASS] 9.1: Target absent -> CREDENTIAL_STORE_EMPTY (credential_present=False)")
    tests_passed += 1

    # 9.2: Blob present but tokens empty -> CREDENTIAL_TOKEN_FIELDS_MISSING (credential_present = True)
    tests_total += 1
    v_9_2 = verify_active_account(ps_runner=lambda: (0, '{"token":{"access_token":"","refresh_token":""}}', ""))
    assert v_9_2.status == CredentialVerificationStatus.CREDENTIAL_TOKEN_FIELDS_MISSING
    assert v_9_2.credential_present is True
    print("  [PASS] 9.2: Empty token fields -> CREDENTIAL_TOKEN_FIELDS_MISSING (credential_present=True)")
    tests_passed += 1

    # =========================================================================
    # ITEM 10 & 11 & 14: Switch Production Branches & Target Scope Tests
    # =========================================================================

    # 10.A: AGM command non-zero -> exit 1 (SWITCH_COMMAND_FAILED)
    tests_total += 1
    sw_10_a = execute_safe_switch(
        "alice@example.com",
        confirm=True,
        agm_runner=lambda cmd, t: (1, "", "Error: switch failed"),
        verifier=lambda exp, net: VerificationResult(exp, None, None, False, CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY, "UNKNOWN", False, "CREDENTIAL_STORE_ONLY", "UNKNOWN", "WINDOWS_CREDENTIAL_MANAGER", "")
    )
    assert sw_10_a["status"] == SwitchOutcome.SWITCH_COMMAND_FAILED.value
    assert sw_10_a["exit_code"] == 1
    print("  [PASS] 10.A: AGM command non-zero -> exit 1 (SWITCH_COMMAND_FAILED)")
    tests_passed += 1

    # 10.B: AGM zero + post verifier WRITTEN_UNVERIFIED -> exit 2 (SWITCH_WRITTEN_UNVERIFIED)
    tests_total += 1
    mock_post_unverified = VerificationResult("alice@example.com", None, "fp123", True, CredentialVerificationStatus.CREDENTIAL_STORE_WRITTEN_UNVERIFIED, "MEDIUM", None, "CREDENTIAL_STORE_ONLY", "UNKNOWN", "WINDOWS_CREDENTIAL_MANAGER", "")
    sw_10_b = execute_safe_switch(
        "alice@example.com",
        confirm=True,
        agm_runner=lambda cmd, t: (0, "Switched successfully", ""),
        verifier=lambda exp, net: mock_post_unverified
    )
    assert sw_10_b["status"] == SwitchOutcome.SWITCH_WRITTEN_UNVERIFIED.value
    assert sw_10_b["exit_code"] == 2
    print("  [PASS] 10.B: AGM zero + post verifier WRITTEN_UNVERIFIED -> exit 2 (SWITCH_WRITTEN_UNVERIFIED)")
    tests_passed += 1

    # 10.C: AGM zero + post verifier IDENTITY_VERIFIED -> exit 0 (CREDENTIAL_IDENTITY_VERIFIED)
    tests_total += 1
    mock_post_verified = VerificationResult("alice@example.com", "alice@example.com", "fp123", True, CredentialVerificationStatus.CREDENTIAL_STORE_IDENTITY_VERIFIED, "STRONG", True, "CREDENTIAL_STORE_ONLY", "UNKNOWN", "GOOGLE_USERINFO_ENDPOINT", "")
    sw_10_c = execute_safe_switch(
        "alice@example.com",
        confirm=True,
        agm_runner=lambda cmd, t: (0, "Switched successfully", ""),
        verifier=lambda exp, net: mock_post_verified
    )
    assert sw_10_c["status"] == SwitchOutcome.CREDENTIAL_IDENTITY_VERIFIED.value
    assert sw_10_c["exit_code"] == 0
    print("  [PASS] 10.C: AGM zero + post verifier IDENTITY_VERIFIED -> exit 0 (CREDENTIAL_IDENTITY_VERIFIED)")
    tests_passed += 1

    # 10.D: AGM zero + post verifier IDENTITY_MISMATCH -> exit 1 (VERIFY_FAILED)
    tests_total += 1
    mock_post_mismatch = VerificationResult("alice@example.com", "bob@example.com", "fp123", True, CredentialVerificationStatus.IDENTITY_MISMATCH, "STRONG", False, "CREDENTIAL_STORE_ONLY", "UNKNOWN", "GOOGLE_USERINFO_ENDPOINT", "")
    sw_10_d = execute_safe_switch(
        "alice@example.com",
        confirm=True,
        agm_runner=lambda cmd, t: (0, "Switched successfully", ""),
        verifier=lambda exp, net: mock_post_mismatch
    )
    assert sw_10_d["status"] == SwitchOutcome.VERIFY_FAILED.value
    assert sw_10_d["exit_code"] == 1
    print("  [PASS] 10.D: AGM zero + post verifier IDENTITY_MISMATCH -> exit 1 (VERIFY_FAILED)")
    tests_passed += 1

    # 11.1: Target 'ide' / 'all' strictly rejected (Item 11)
    tests_total += 1
    sw_11_1 = execute_safe_switch("alice@example.com", target="ide")
    assert sw_11_1["status"] == SwitchOutcome.INVALID_ARGUMENT.value
    assert sw_11_1["error_code"] == "UNSUPPORTED_TARGET_SCOPE"
    assert sw_11_1["exit_code"] == 1
    print("  [PASS] 11.1: Target 'ide' strictly rejected in T02 scope")
    tests_passed += 1

    # 14.1: Dry-run with injected verifier performs ZERO real vault calls (Item 14)
    tests_total += 1
    vault_called = False
    def trap_verifier(exp, net):
        nonlocal vault_called
        vault_called = True
        return mock_post_unverified

    sw_14_1 = execute_safe_switch("alice@example.com", confirm=False, verifier=trap_verifier)
    assert sw_14_1["status"] == SwitchOutcome.DRY_RUN.value
    assert sw_14_1["exit_code"] == 3
    assert vault_called is True  # Trapped by injected mock; real OS CredRead was bypassed!
    print("  [PASS] 14.1: Switch dry-run is 100% host-isolated via verifier injection")
    tests_passed += 1

    # =========================================================================
    # ITEM 13: Fail Closed in Info Mode Table Schema Checks
    # =========================================================================

    # 13.1: Info mode with corrupted table header -> fail closed (Item 13)
    tests_total += 1
    corrupted_info = """Account: alice@example.com
Token expiry: 2026-08-27 (active)
CORRUPTED_HEADER_TABLE
google gemini-1.5-pro 85% 2026-08-27T00:00:00Z
"""
    info_13_1 = parse_agm_info(corrupted_info, now_epoch=now)
    assert info_13_1 is not None
    assert info_13_1.format_support == FormatSupportState.FORMAT_UNSUPPORTED
    assert not info_13_1.eligible
    print("  [PASS] 13.1: Corrupted info mode table header -> fails closed as FORMAT_UNSUPPORTED")
    tests_passed += 1

    # =========================================================================
    # ITEM 15: ModelGroup Enum Fail-Closed Validation
    # =========================================================================

    # 15.1: Typo in target_model_group 'gemni-pro' -> FAILED_SAFE (Item 15)
    tests_total += 1
    sel_typo = AccountSelector(SelectionConfig(target_model_group="gemni-pro"))
    res_15_1 = sel_typo.select_next_account([], now=now)
    assert res_15_1.terminal_state == TerminalState.FAILED_SAFE
    assert "INVALID_MODEL_GROUP" in res_15_1.decision_reason
    print("  [PASS] 15.1: Typo 'gemni-pro' in target_model_group -> FAILED_SAFE (INVALID_MODEL_GROUP)")
    tests_passed += 1

    # 15.2: Empty target_model_group -> FAILED_SAFE (Item 15)
    tests_total += 1
    sel_empty = AccountSelector(SelectionConfig(target_model_group=""))
    res_15_2 = sel_empty.select_next_account([], now=now)
    assert res_15_2.terminal_state == TerminalState.FAILED_SAFE
    assert "INVALID_MODEL_GROUP" in res_15_2.decision_reason
    print("  [PASS] 15.2: Empty target_model_group -> FAILED_SAFE (INVALID_MODEL_GROUP)")
    tests_passed += 1

    print(f"\n=======================================================")
    print(f"Summary: {tests_passed}/{tests_total} test assertions passed (100% success rate).")
    return tests_passed == tests_total


if __name__ == "__main__":
    success = run_tests()
    if not success:
        sys.exit(1)
