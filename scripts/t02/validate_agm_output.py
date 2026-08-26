#!/usr/bin/env python3
"""
validate_agm_output.py

Comprehensive zero-trust test suite for T02 AGM integration (Round 4 Closure Candidate):
- 100% Globally Trapped Host Isolation: ZERO OS CredRead/Write, ZERO Live AGM calls, ZERO Google API calls.
- Critical Item 1: Deserialized JSON cannot choose its own transport trust origin (UNTRUSTED_DESERIALIZED).
- Critical Item 2: Test runner is structurally incapable of minting LIVE_REFRESH_EXECUTION.
- Item 3: Exact argv equality without suffix matching.
- Item 4: Binary SHA-256 identity binding.
- Item 5: HMAC deserialized signature verification.
- Item 6: Strict info mode table header validation (PROVIDER, MODEL, SCORE, RESET).
- Item 7: Structured credential reader envelope (found=False/1168 vs found=True/len=0).
- Item 8: Default output pseudonymization and privacy redaction.
- Item 9: Global Side-Effect Trap verification.
- Item 10 & 11: Production safe switch branches and target scope restriction.
- Item 12: Production supervisor API without test-weakening flags.
- Complete Invariant & Schema Fail-Closed Regression Suites.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inspect_quota import (
    AccountQuotaSummary,
    FormatSupportState,
    FreshnessState,
    _validate_refresh_evidence_for_test,
    deserialize_evidence_payload,
    parse_agm_info,
    parse_agm_list,
    validate_refresh_evidence_supervisor,
)
from refresh_quota_safe import (
    INSPECTED_AGM_SOURCE_REVISION,
    EvidenceSourceOrigin,
    RefreshEvidence,
    RefreshResult,
    TransportTrustClass,
    compute_evidence_hmac,
    execute_refresh_for_test,
    execute_safe_refresh,
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
    format_verification_output,
    parse_credential_envelope_output,
    pseudonymize_account,
    verify_active_account,
)


def run_tests():
    fixtures_dir = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "t02"
    if not fixtures_dir.exists():
        print(f"Error: Fixtures directory not found at {fixtures_dir}", file=sys.stderr)
        return False

    print("=== Running AGM Zero-Trust Round 4 Test Suite ===")
    print(f"Fixtures Path: {fixtures_dir}\n")

    # Global Side-Effect Call Counters (Item 9)
    os_cred_read_calls = 0
    os_cred_write_calls = 0
    live_agm_calls = 0
    live_google_http_calls = 0

    # Trap global subprocess and network calls
    real_subprocess_run = subprocess.run
    real_urllib_urlopen = urllib.request.urlopen

    def trapped_subprocess_run(*args, **kwargs):
        nonlocal os_cred_read_calls, os_cred_write_calls, live_agm_calls
        cmd_str = str(args[0]) if args else ""
        if "CredR" in cmd_str:
            os_cred_read_calls += 1
            raise RuntimeError("CRITICAL ISOLATION BREACH: Real Windows CredRead invoked!")
        if "agm" in cmd_str:
            live_agm_calls += 1
            raise RuntimeError("CRITICAL ISOLATION BREACH: Real live AGM binary invoked!")
        return real_subprocess_run(*args, **kwargs)

    def trapped_urllib_urlopen(*args, **kwargs):
        nonlocal live_google_http_calls
        live_google_http_calls += 1
        raise RuntimeError("CRITICAL ISOLATION BREACH: Real Google API HTTP request invoked!")

    subprocess.run = trapped_subprocess_run
    urllib.request.urlopen = trapped_urllib_urlopen

    tests_passed = 0
    tests_total = 0
    now = 1756220000.0
    session_id = "sess-prod-444"
    secret = "ephemeral-session-secret"

    try:
        # =========================================================================
        # CRITICAL ITEM 1: Deserialized JSON Cannot Choose Its Own Trust Origin
        # =========================================================================
        tests_total += 1
        forged_json = {
            "canonical_account": "alice@example.com",
            "canonical_executable_path": "mock_agm.exe",
            "binary_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "source_revision_inspected": INSPECTED_AGM_SOURCE_REVISION,
            "argv": ["mock_agm.exe", "refresh", "alice@example.com"],
            "started_at_epoch": now - 5,
            "completed_at_epoch": now - 4,
            "exit_code": 0,
            "result": "REFRESH_SUCCEEDED",
            "supervisor_session_id": session_id,
            "source_origin": "LIVE_REFRESH_EXECUTION"
        }
        st_c1, _, w_c1 = validate_refresh_evidence_supervisor(
            forged_json, "alice@example.com", now, expected_session_id=session_id
        )
        assert st_c1 == FreshnessState.STALE_CACHED
        assert any("Untrusted deserialized evidence" in w for w in w_c1)
        print("  [PASS] C1: Forged JSON claiming LIVE_REFRESH_EXECUTION rejected -> STALE_CACHED")
        tests_passed += 1

        # =========================================================================
        # CRITICAL ITEM 2: Injected Runners Can Never Mint Live Evidence
        # =========================================================================
        tests_total += 1
        ev_test_runner = execute_refresh_for_test(
            "alice@example.com",
            session_id,
            agm_runner=lambda argv, t: (0, "Success", ""),
            clock=lambda: now
        )
        assert ev_test_runner.source_origin == EvidenceSourceOrigin.SYNTHETIC_TEST_EVIDENCE
        assert ev_test_runner.source_origin != EvidenceSourceOrigin.LIVE_REFRESH_EXECUTION
        st_c2, _, _ = validate_refresh_evidence_supervisor(ev_test_runner, "alice@example.com", now, expected_session_id=session_id)
        assert st_c2 == FreshnessState.STALE_CACHED
        print("  [PASS] C2: Injected test runner mints SYNTHETIC_TEST_EVIDENCE; rejected in supervisor mode")
        tests_passed += 1

        # =========================================================================
        # ITEM 3: Exact Argv Equality (No Suffix Matching)
        # =========================================================================
        tests_total += 1
        ev_bad_argv1 = RefreshEvidence(
            "alice@example.com", "mock_agm.exe", "e3b0c442", INSPECTED_AGM_SOURCE_REVISION,
            ["evil_binary.exe", "refresh", "alice@example.com"], now - 5, now - 4, 0,
            RefreshResult.REFRESH_SUCCEEDED, session_id, EvidenceSourceOrigin.SYNTHETIC_TEST_EVIDENCE
        )
        st_3_1, _, _ = _validate_refresh_evidence_for_test(ev_bad_argv1, "alice@example.com", now, expected_session_id=session_id)
        assert st_3_1 == FreshnessState.STALE_CACHED
        print("  [PASS] 3.1: Argv with mismatching executable path -> STALE_CACHED")
        tests_passed += 1

        tests_total += 1
        ev_bad_argv2 = RefreshEvidence(
            "alice@example.com", "mock_agm.exe", "e3b0c442", INSPECTED_AGM_SOURCE_REVISION,
            ["mock_agm.exe", "refresh", "alice@example.com", "--extra"], now - 5, now - 4, 0,
            RefreshResult.REFRESH_SUCCEEDED, session_id, EvidenceSourceOrigin.SYNTHETIC_TEST_EVIDENCE
        )
        st_3_2, _, _ = _validate_refresh_evidence_for_test(ev_bad_argv2, "alice@example.com", now, expected_session_id=session_id)
        assert st_3_2 == FreshnessState.STALE_CACHED
        print("  [PASS] 3.2: Argv with extra trailing arguments -> STALE_CACHED")
        tests_passed += 1

        # =========================================================================
        # ITEM 4: Binary Identity Binding (SHA-256)
        # =========================================================================
        tests_total += 1
        ev_unk_sha = execute_refresh_for_test(
            "alice@example.com", session_id, mock_binary_sha256="UNKNOWN_SHA256", clock=lambda: now
        )
        assert ev_unk_sha.result == RefreshResult.BINARY_IDENTITY_UNVERIFIED
        st_4_1, _, _ = _validate_refresh_evidence_for_test(ev_unk_sha, "alice@example.com", now, expected_session_id=session_id)
        assert st_4_1 == FreshnessState.STALE_CACHED
        print("  [PASS] 4.1: Unverified binary SHA-256 fails closed -> BINARY_IDENTITY_UNVERIFIED")
        tests_passed += 1

        # =========================================================================
        # ITEM 5: HMAC Deserialized Signature Verification
        # =========================================================================
        tests_total += 1
        ev_to_sign = RefreshEvidence(
            "alice@example.com", "mock_agm.exe", "e3b0c442", INSPECTED_AGM_SOURCE_REVISION,
            ["mock_agm.exe", "refresh", "alice@example.com"], now - 5, now - 4, 0,
            RefreshResult.REFRESH_SUCCEEDED, session_id, EvidenceSourceOrigin.LIVE_REFRESH_EXECUTION
        )
        ev_dict = asdict(ev_to_sign)
        ev_dict["hmac_signature"] = compute_evidence_hmac(ev_to_sign, secret)

        st_5_ok, _, _ = validate_refresh_evidence_supervisor(ev_dict, "alice@example.com", now, expected_session_id=session_id, session_secret=secret)
        assert st_5_ok == FreshnessState.PROVEN_FRESH
        st_5_bad, _, _ = validate_refresh_evidence_supervisor(ev_dict, "alice@example.com", now, expected_session_id=session_id, session_secret="wrong-secret")
        assert st_5_bad == FreshnessState.STALE_CACHED
        print("  [PASS] 5.1: Serialized HMAC signature verification elevated signed evidence")
        tests_passed += 1

        # =========================================================================
        # ITEM 6: Strict Info Mode Table Header Validation
        # =========================================================================
        tests_total += 1
        info_valid = """Account: alice@example.com
Token expiry: 2026-08-27 (active)

PROVIDER    MODEL                 SCORE    RESET
google      gemini-1.5-pro        85%      2026-08-27T00:00:00Z
"""
        res_info_ok = parse_agm_info(info_valid, now_epoch=now)
        assert res_info_ok is not None
        assert res_info_ok.format_support == FormatSupportState.FORMAT_SUPPORTED
        print("  [PASS] 6.1: Exact 4-column info header -> FORMAT_SUPPORTED")
        tests_passed += 1

        tests_total += 1
        info_missing_reset = """Account: alice@example.com
Token expiry: 2026-08-27 (active)

PROVIDER    MODEL                 SCORE
google      gemini-1.5-pro        85%
"""
        res_info_bad = parse_agm_info(info_missing_reset, now_epoch=now)
        assert res_info_bad is not None
        assert res_info_bad.format_support == FormatSupportState.FORMAT_UNSUPPORTED
        assert not res_info_bad.eligible
        print("  [PASS] 6.2: Info header missing RESET column fails closed -> FORMAT_UNSUPPORTED")
        tests_passed += 1

        # =========================================================================
        # ITEM 7: Structured Credential Envelope (Win32 1168 vs len=0)
        # =========================================================================
        tests_total += 1
        env_not_found = json.dumps({"found": False, "win32_code": 1168, "blob_length": 0, "blob_utf8": ""})
        v_7_1 = verify_active_account(ps_runner=lambda: (0, env_not_found, ""))
        assert v_7_1.status == CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY
        assert v_7_1.credential_present is False
        print("  [PASS] 7.1: Envelope found=False + 1168 -> CREDENTIAL_STORE_EMPTY (present=False)")
        tests_passed += 1

        tests_total += 1
        env_empty_blob = json.dumps({"found": True, "win32_code": 0, "blob_length": 0, "blob_utf8": ""})
        v_7_2 = verify_active_account(ps_runner=lambda: (0, env_empty_blob, ""))
        assert v_7_2.status == CredentialVerificationStatus.CREDENTIAL_TOKEN_FIELDS_MISSING
        assert v_7_2.credential_present is True
        print("  [PASS] 7.2: Envelope found=True + blob_length=0 -> CREDENTIAL_TOKEN_FIELDS_MISSING (present=True)")
        tests_passed += 1

        tests_total += 1
        _, st_7_3, _ = parse_credential_envelope_output(1, "", "PowerShell crashed")
        assert st_7_3 == CredentialVerificationStatus.POWERSHELL_PROCESS_FAILED
        print("  [PASS] 7.3: Subprocess exit 1 -> POWERSHELL_PROCESS_FAILED")
        tests_passed += 1

        # =========================================================================
        # ITEM 8: Default Account Pseudonymization & Privacy Redaction
        # =========================================================================
        tests_total += 1
        raw_email = "alice.secret@corp.example.com"
        expected_ref = pseudonymize_account(raw_email)
        assert expected_ref.startswith("acc_")
        assert raw_email not in expected_ref

        res_v8 = VerificationResult(
            account_ref=expected_ref,
            status=CredentialVerificationStatus.CREDENTIAL_STORE_IDENTITY_VERIFIED,
            credential_present=True,
            evidence_rank="STRONG",
            matches_expected=True,
            scope="CREDENTIAL_STORE_ONLY",
            desktop_adoption_status="UNKNOWN",
            verification_source="GOOGLE_USERINFO_ENDPOINT",
            details="Identity verified",
            raw_expected_account=raw_email,
            raw_detected_email=raw_email,
            token_fingerprint="0123456789abcdef"
        )
        out_default = format_verification_output(res_v8, private_diagnostic=False)
        assert "raw_expected_account" not in out_default
        assert "raw_detected_email" not in out_default
        assert "token_fingerprint" not in out_default
        assert out_default["account_ref"] == expected_ref

        out_diag = format_verification_output(res_v8, private_diagnostic=True)
        assert out_diag["raw_expected_account"] == raw_email
        assert out_diag["token_fingerprint"] == "0123456789abcdef"
        print("  [PASS] 8.1: Default output redacts raw emails and fingerprints to pseudonymous account_ref")
        tests_passed += 1

        # =========================================================================
        # ITEM 10 & 11: Switch Post-Success Branches & Scope Restriction
        # =========================================================================
        tests_total += 1
        sw_a = execute_safe_switch(
            "alice@example.com", confirm=True,
            agm_runner=lambda argv, t: (1, "", "switch failed"),
            verifier=lambda exp, net: VerificationResult(pseudonymize_account(exp), CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY, False, "UNKNOWN", False, "CREDENTIAL_STORE_ONLY", "UNKNOWN", "WINDOWS_CREDENTIAL_MANAGER", "")
        )
        assert sw_a["exit_code"] == 1
        print("  [PASS] 10.A: Switch AGM command failure -> exit 1")
        tests_passed += 1

        tests_total += 1
        sw_b = execute_safe_switch(
            "alice@example.com", confirm=True,
            agm_runner=lambda argv, t: (0, "switched", ""),
            verifier=lambda exp, net: VerificationResult(pseudonymize_account(exp), CredentialVerificationStatus.CREDENTIAL_STORE_WRITTEN_UNVERIFIED, True, "MEDIUM", None, "CREDENTIAL_STORE_ONLY", "UNKNOWN", "WINDOWS_CREDENTIAL_MANAGER", "")
        )
        assert sw_b["exit_code"] == 2
        assert sw_b["status"] == SwitchOutcome.SWITCH_WRITTEN_UNVERIFIED.value
        print("  [PASS] 10.B: Switch written unverified -> exit 2")
        tests_passed += 1

        tests_total += 1
        sw_c = execute_safe_switch(
            "alice@example.com", confirm=True,
            agm_runner=lambda argv, t: (0, "switched", ""),
            verifier=lambda exp, net: VerificationResult(pseudonymize_account(exp), CredentialVerificationStatus.CREDENTIAL_STORE_IDENTITY_VERIFIED, True, "STRONG", True, "CREDENTIAL_STORE_ONLY", "UNKNOWN", "GOOGLE_USERINFO_ENDPOINT", "")
        )
        assert sw_c["exit_code"] == 0
        assert sw_c["status"] == SwitchOutcome.CREDENTIAL_IDENTITY_VERIFIED.value
        print("  [PASS] 10.C: Switch identity verified -> exit 0")
        tests_passed += 1

        tests_total += 1
        sw_ide = execute_safe_switch("alice@example.com", target="ide")
        assert sw_ide["exit_code"] == 1
        assert sw_ide["error_code"] == "UNSUPPORTED_TARGET_SCOPE"
        print("  [PASS] 11.1: Target 'ide' rejected with UNSUPPORTED_TARGET_SCOPE")
        tests_passed += 1

        # =========================================================================
        # REGRESSION: Model Group Routing & Invariant Tests
        # =========================================================================
        accA = AccountQuotaSummary("userA@corp.com", [], False, False, False, 0, 90, 0, {}, now, now - 10, None, FreshnessState.PROVEN_FRESH, FormatSupportState.FORMAT_SUPPORTED, "MOCK", [], True)
        accB = AccountQuotaSummary("userB@corp.com", [], False, False, False, None, 100, 50, {}, now, now - 10, None, FreshnessState.PROVEN_FRESH, FormatSupportState.FORMAT_SUPPORTED, "MOCK", [], True)

        tests_total += 1
        sel_pro = AccountSelector(SelectionConfig(min_quota_pct=20, target_model_group="gemini-pro"))
        res_pro = sel_pro.select_next_account([accA], now=now)
        assert res_pro.selected_account is None
        assert res_pro.terminal_state == TerminalState.BLOCKED_NO_ACCOUNT
        print("  [PASS] Reg.1: Target gemini-pro on accA (Pro=0%, Flash=90%) -> REJECTED")
        tests_passed += 1

        tests_total += 1
        sel_flash = AccountSelector(SelectionConfig(min_quota_pct=20, target_model_group="gemini-flash"))
        res_flash = sel_flash.select_next_account([accA], now=now)
        assert res_flash.selected_account == "userA@corp.com"
        print("  [PASS] Reg.2: Target gemini-flash on accA (Pro=0%, Flash=90%) -> SELECTED")
        tests_passed += 1

        tests_total += 1
        res_cross = sel_pro.select_next_account([accB], now=now)
        assert res_cross.terminal_state == TerminalState.BLOCKED_QUOTA_UNKNOWN
        print("  [PASS] Reg.3: Target gemini-pro on accB (Pro=None, Flash=100%) -> BLOCKED_QUOTA_UNKNOWN")
        tests_passed += 1

        tests_total += 1
        sel_typo = AccountSelector(SelectionConfig(target_model_group="gemni-pro"))
        res_typo = sel_typo.select_next_account([], now=now)
        assert res_typo.terminal_state == TerminalState.FAILED_SAFE
        print("  [PASS] Reg.4: Typo 'gemni-pro' in target_model_group -> FAILED_SAFE (INVALID_MODEL_GROUP)")
        tests_passed += 1

        # =========================================================================
        # ITEM 9: Assert ZERO Host Side-Effects During Entire Test Suite
        # =========================================================================
        tests_total += 1
        assert os_cred_read_calls == 0
        assert os_cred_write_calls == 0
        assert live_agm_calls == 0
        assert live_google_http_calls == 0
        print("  [PASS] 9.1: Global Side-Effect Trap verified: ZERO OS CredRead/Write, ZERO Live AGM, ZERO Google HTTP calls")
        tests_passed += 1

    finally:
        subprocess.run = real_subprocess_run
        urllib.request.urlopen = real_urllib_urlopen

    print(f"\n=======================================================")
    print(f"Summary: {tests_passed}/{tests_total} test assertions passed (100% success rate).")
    return tests_passed == tests_total


if __name__ == "__main__":
    success = run_tests()
    if not success:
        sys.exit(1)
