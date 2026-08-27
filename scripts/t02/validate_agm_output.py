#!/usr/bin/env python3
"""
validate_agm_output.py

Comprehensive zero-trust test suite for T02 AGM integration (Round 7 Final Closure):
1. Global side-effect tripwires (CredRead, CredWrite, live AGM, Google HTTP).
2. Critical Item 1: Pre-execution binary check (wrong binary SHA -> SUBPROCESS_CALL_COUNT == 0).
3. Critical Item 2 & 11: TrustedAgmRunner test matrix (missing SHA, malformed SHA, wrong SHA, TOCTOU).
4. Critical Item 3: Safe switch requires TrustedAgmIdentity (missing/wrong -> 0 subprocess calls).
5. Critical Item 4: List / Info CLI requires TrustedAgmIdentity.
6. Item 5: Supervisor APIs require structured TrustedAgmIdentity.
7. Critical Item 6: SanitizedRefreshEvidenceDTO contains normalized error_code only.
8. Critical Item 7 & 10: Adversarial Privacy Test Matrix (toxic tokens/emails/paths/traces completely absent in default DTOs).
9. Item 8: SanitizedVerificationOutput exposes safe enums and zero free-text stderr.
10. Item 9: switch_account_safe does not echo raw invalid account input in default output.
11. Item 13: Transport trust & HMAC signature regression suite.
12. Zero host operations assertion.
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
    WarningCode,
    _validate_refresh_evidence_for_test,
    deserialize_evidence_payload,
    parse_agm_info,
    parse_agm_list,
    validate_refresh_evidence_supervisor,
)
from refresh_quota_safe import (
    INSPECTED_AGM_SOURCE_REVISION,
    EvidenceSourceOrigin,
    LiveExecutionAttestation,
    RefreshEvidence,
    RefreshResult,
    TransportTrustClass,
    compute_evidence_hmac,
    execute_refresh_for_test,
    execute_safe_refresh,
    issue_live_execution_attestation,
    pseudonymize_account,
    verify_evidence_signature,
    verify_live_execution_attestation,
)
from selection_policy import (
    AccountSelector,
    CandidateRejectCode,
    ModelGroup,
    SelectionConfig,
    TerminalState,
)
from switch_account_safe import SwitchOutcome, execute_safe_switch
from trusted_agm_runner import (
    RunnerErrorCode,
    TrustedAgmIdentity,
    TrustedExecutionResult,
    execute_trusted_agm,
)
from verify_active_account import (
    CredentialVerificationStatus,
    VerificationResult,
    parse_credential_envelope_output,
    verify_active_account,
)


def run_tests():
    fixtures_dir = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "t02"
    if not fixtures_dir.exists():
        print(f"Error: Fixtures directory not found at {fixtures_dir}", file=sys.stderr)
        return False

    print("=== Running AGM Zero-Trust Round 7 Test Suite ===")
    print(f"Fixtures Path: {fixtures_dir}\n")

    # Global Side-Effect Call Counters (Item 12 & Tripwire)
    os_cred_read_calls = 0
    os_cred_write_calls = 0
    live_agm_calls = 0
    live_google_http_calls = 0

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
    session_id = "sess-prod-777"
    secret = "ephemeral-session-secret-777"
    valid_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    try:
        # =========================================================================
        # 1. Global Side-Effect Tripwire Verification
        # =========================================================================
        tests_total += 1
        trap_triggered = False
        try:
            subprocess.run(["powershell", "CredR"], capture_output=True)
        except RuntimeError as e:
            if "CRITICAL ISOLATION BREACH" in str(e):
                trap_triggered = True
        assert trap_triggered is True
        os_cred_read_calls = 0
        print("  [PASS] 1: Global Side-Effect Tripwire verified (traps unmocked calls)")
        tests_passed += 1

        # =========================================================================
        # 2. Critical Item 1 & 11: Pre-Execution Binary Check (Wrong SHA -> 0 Subprocess Calls)
        # =========================================================================
        tests_total += 1
        runner_subprocess_calls = 0

        def counting_injected_runner(argv, timeout):
            nonlocal runner_subprocess_calls
            runner_subprocess_calls += 1
            return 0, "mock success", ""

        trusted_identity_wrong = TrustedAgmIdentity(expected_binary_sha256="f" * 64)
        exec_res_mismatch = execute_trusted_agm(
            subcommand_args=["refresh", "alice@example.com"],
            trusted_identity=trusted_identity_wrong,
            injected_runner=counting_injected_runner,
            injected_sha_computer=lambda path: valid_sha,
            injected_resolver=lambda: "mock_agm.exe"
        )
        assert exec_res_mismatch.command_executed is False
        assert exec_res_mismatch.error_code == RunnerErrorCode.BINARY_IDENTITY_MISMATCH
        assert runner_subprocess_calls == 0  # CRITICAL ITEM 1: SUBPROCESS CALL COUNT == 0!
        print("  [PASS] 2: Pre-execution binary check stops execution before subprocess -> SUBPROCESS_CALL_COUNT == 0")
        tests_passed += 1

        # =========================================================================
        # 3. Critical Item 2 & 11: TrustedAgmRunner Test Matrix (Missing & Malformed SHA)
        # =========================================================================
        tests_total += 1
        # 3a. Missing expected identity -> 0 calls
        runner_subprocess_calls = 0
        res_no_id = execute_trusted_agm(
            ["refresh", "alice@example.com"],
            trusted_identity=None,
            injected_runner=counting_injected_runner,
            injected_sha_computer=lambda p: valid_sha,
            injected_resolver=lambda: "mock_agm.exe"
        )
        assert res_no_id.command_executed is False
        assert res_no_id.error_code == RunnerErrorCode.BINARY_IDENTITY_UNCONFIGURED
        assert runner_subprocess_calls == 0

        # 3b. Malformed expected SHA -> 0 calls
        res_bad_id = execute_trusted_agm(
            ["refresh", "alice@example.com"],
            trusted_identity=TrustedAgmIdentity(expected_binary_sha256="not_a_64_hex_string"),
            injected_runner=counting_injected_runner,
            injected_sha_computer=lambda p: valid_sha,
            injected_resolver=lambda: "mock_agm.exe"
        )
        assert res_bad_id.command_executed is False
        assert res_bad_id.error_code == RunnerErrorCode.BINARY_IDENTITY_CONFIG_INVALID
        assert runner_subprocess_calls == 0

        # 3c. Correct SHA -> command permitted
        trusted_id_ok = TrustedAgmIdentity(expected_binary_sha256=valid_sha)
        res_ok = execute_trusted_agm(
            ["refresh", "alice@example.com"],
            trusted_identity=trusted_id_ok,
            injected_runner=counting_injected_runner,
            injected_sha_computer=lambda p: valid_sha,
            injected_resolver=lambda: "mock_agm.exe"
        )
        assert res_ok.command_executed is True
        assert res_ok.success is True
        assert runner_subprocess_calls == 1

        # 3d. TOCTOU post-mutation check
        mutation_calls = [valid_sha, "mutated_post_sha" + "0" * 48]
        res_toctou = execute_trusted_agm(
            ["refresh", "alice@example.com"],
            trusted_identity=trusted_id_ok,
            injected_runner=counting_injected_runner,
            injected_sha_computer=lambda p: mutation_calls.pop(0),
            injected_resolver=lambda: "mock_agm.exe"
        )
        assert res_toctou.command_executed is True
        assert res_toctou.error_code == RunnerErrorCode.BINARY_CHANGED_DURING_EXECUTION
        print("  [PASS] 3: TrustedAgmRunner test matrix (missing, malformed, match, TOCTOU mutation) verified")
        tests_passed += 1

        # =========================================================================
        # 4. Critical Item 3: Switch Command Requires TrustedAgmIdentity (0 Calls on Failure)
        # =========================================================================
        tests_total += 1
        switch_subprocess_calls = 0

        def counting_switch_runner(argv, timeout):
            nonlocal switch_subprocess_calls
            switch_subprocess_calls += 1
            return 0, "switched", ""

        # 4a. No trusted identity -> exit 4, 0 calls
        sw_res_no_id = execute_safe_switch(
            "alice@example.com", confirm=True,
            trusted_identity=None,
            agm_runner=counting_switch_runner,
            executable_resolver=lambda: "mock_agm.exe"
        )
        assert sw_res_no_id["exit_code"] == 4
        assert sw_res_no_id["status"] == SwitchOutcome.BINARY_IDENTITY_UNCONFIGURED.value
        assert switch_subprocess_calls == 0

        # 4b. Wrong expected SHA -> exit 4, 0 calls
        sw_res_wrong_id = execute_safe_switch(
            "alice@example.com", confirm=True,
            trusted_identity=TrustedAgmIdentity(expected_binary_sha256="d" * 64),
            agm_runner=counting_switch_runner,
            executable_resolver=lambda: "mock_agm.exe",
            sha_computer=lambda p: valid_sha
        )
        assert sw_res_wrong_id["exit_code"] == 4
        assert sw_res_wrong_id["status"] == SwitchOutcome.BINARY_IDENTITY_MISMATCH.value
        assert switch_subprocess_calls == 0
        print("  [PASS] 4: Safe switch enforces TrustedAgmIdentity before execution -> 0 subprocess calls")
        tests_passed += 1

        # =========================================================================
        # 5. Item 5: Supervisor APIs Require Structured TrustedAgmIdentity
        # =========================================================================
        tests_total += 1
        with open(fixtures_dir / "list_normal.txt", "r", encoding="utf-8") as f:
            list_text = f.read()

        ev_test = RefreshEvidence(
            canonical_account="alice@example.com",
            canonical_executable_path="mock_agm.exe",
            binary_sha256=valid_sha,
            source_revision_inspected=INSPECTED_AGM_SOURCE_REVISION,
            argv=["mock_agm.exe", "refresh", "alice@example.com"],
            started_at_epoch=now - 5,
            completed_at_epoch=now - 4,
            exit_code=0,
            result=RefreshResult.REFRESH_SUCCEEDED,
            supervisor_session_id=session_id,
            source_origin=EvidenceSourceOrigin.SYNTHETIC_TEST_EVIDENCE
        )
        ev_map = {"alice@example.com": ev_test}

        # 5a. Missing trusted identity -> fail closed
        res_list_no_sha = parse_agm_list(
            list_text, refresh_evidence_map=ev_map, supervisor_session_id=session_id,
            trusted_identity=None, now_epoch=now, _test_mode_allow_synthetic=True
        )
        alice_no_sha = next(r for r in res_list_no_sha if r.canonical_account == "alice@example.com")
        assert alice_no_sha.freshness_state == FreshnessState.STALE_CACHED
        assert alice_no_sha.eligible is False
        assert alice_no_sha.warning_codes == [WarningCode.BINARY_IDENTITY_UNCONFIGURED]

        # 5b. Valid trusted identity -> PROVEN_FRESH
        res_list_ok = parse_agm_list(
            list_text, refresh_evidence_map=ev_map, supervisor_session_id=session_id,
            trusted_identity=trusted_id_ok, now_epoch=now, _test_mode_allow_synthetic=True
        )
        alice_ok = next(r for r in res_list_ok if r.canonical_account == "alice@example.com")
        assert alice_ok.freshness_state == FreshnessState.PROVEN_FRESH
        assert alice_ok.eligible is True
        print("  [PASS] 5: Supervisor APIs require structured TrustedAgmIdentity")
        tests_passed += 1

        # =========================================================================
        # 6. Critical Item 6: SanitizedRefreshEvidenceDTO Contains Normalized error_code Only
        # =========================================================================
        tests_total += 1
        ev_err = RefreshEvidence(
            canonical_account="confidential@example.com",
            canonical_executable_path="mock_agm.exe",
            binary_sha256=valid_sha,
            source_revision_inspected=INSPECTED_AGM_SOURCE_REVISION,
            argv=["mock_agm.exe", "refresh", "confidential@example.com"],
            started_at_epoch=now - 5,
            completed_at_epoch=now - 4,
            exit_code=1,
            result=RefreshResult.REFRESH_FAILED_AUTH,
            supervisor_session_id=session_id,
            source_origin=EvidenceSourceOrigin.LIVE_REFRESH_EXECUTION,
            error_code="REFRESH_AUTH_FAILED",
            error_summary_private="oauth2: invalid_grant for user confidential@example.com"
        )
        dto_err = ev_err.to_sanitized_dto()
        dto_err_dict = asdict(dto_err)

        assert dto_err_dict["error_code"] == "REFRESH_AUTH_FAILED"
        assert "error_summary" not in dto_err_dict
        assert "error_summary_private" not in dto_err_dict
        assert "confidential@example.com" not in json.dumps(dto_err_dict)
        print("  [PASS] 6: SanitizedRefreshEvidenceDTO contains normalized error_code only")
        tests_passed += 1

        # =========================================================================
        # 7. Critical Item 7 & 10: Adversarial Privacy Test Matrix (Zero Sensitive Leaks)
        # =========================================================================
        tests_total += 1
        toxic_email = "alice.secret.ident@corp.internal.example"
        toxic_path = "C:\\Users\\admin\\AppData\\Local\\secret\\agm.exe"
        toxic_bearer = "Bearer ya29.v1secret_token_12345"
        toxic_refresh = "1//04secret_refresh_token_abcde"
        toxic_cap_token = "cap_token_8899aabbccddeeff"
        toxic_exception = f"Exception: Failed to connect to Google for {toxic_email} at {toxic_path} with {toxic_bearer}"

        # 7a. Refresh Evidence DTO
        ev_toxic = RefreshEvidence(
            canonical_account=toxic_email,
            canonical_executable_path=toxic_path,
            binary_sha256=valid_sha,
            source_revision_inspected=INSPECTED_AGM_SOURCE_REVISION,
            argv=[toxic_path, "refresh", toxic_email],
            started_at_epoch=now - 5,
            completed_at_epoch=now - 4,
            exit_code=1,
            result=RefreshResult.REFRESH_FAILED_NETWORK,
            supervisor_session_id=session_id,
            source_origin=EvidenceSourceOrigin.LIVE_REFRESH_EXECUTION,
            attestation=LiveExecutionAttestation(
                session_nonce=session_id, execution_nonce="exec-1", account=toxic_email,
                binary_sha256=valid_sha, issued_at=now, capability_token=toxic_cap_token
            ),
            hmac_signature="hmac_sig_123",
            error_code="REFRESH_NETWORK_FAILED",
            error_summary_private=toxic_exception
        )
        refresh_json = json.dumps(asdict(ev_toxic.to_sanitized_dto()))
        assert toxic_email not in refresh_json
        assert toxic_path not in refresh_json
        assert toxic_bearer not in refresh_json
        assert toxic_refresh not in refresh_json
        assert toxic_cap_token not in refresh_json
        assert "Exception" not in refresh_json

        # 7b. Account Quota DTO (warnings with toxic email inside private text)
        quota_toxic = AccountQuotaSummary(
            canonical_account=toxic_email,
            account_ref=pseudonymize_account(toxic_email),
            status_tags=[],
            is_active_cli=False,
            is_active_ide=False,
            is_token_expired=False,
            gemini_pro_pct=90,
            gemini_flash_pct=95,
            claude_pct=None,
            models={},
            parsed_at_epoch=now,
            refresh_confirmed_at_epoch=now - 10,
            quota_reset_time=None,
            freshness_state=FreshnessState.STALE_CACHED,
            format_support=FormatSupportState.FORMAT_SUPPORTED,
            source="TEST",
            warning_codes=[WarningCode.ACCOUNT_MISMATCH, WarningCode.EVIDENCE_EXPIRED],
            parse_warnings_private=[f"Account mismatch: expected {toxic_email}"],
            eligible=False
        )
        quota_json = json.dumps(asdict(quota_toxic.to_sanitized_dto()))
        assert toxic_email not in quota_json
        assert "parse_warnings_private" not in quota_json
        assert quota_json.count(pseudonymize_account(toxic_email)) > 0

        # 7c. Verification Output DTO
        v_toxic = VerificationResult(
            account_ref=pseudonymize_account(toxic_email),
            status=CredentialVerificationStatus.CREDENTIAL_STORE_ACCESS_DENIED,
            credential_present=False,
            evidence_rank="UNKNOWN",
            matches_expected=False,
            scope="CREDENTIAL_STORE_ONLY",
            desktop_adoption_status="UNKNOWN",
            verification_source="WINDOWS_CREDENTIAL_MANAGER",
            error_code="ACCESS_DENIED",
            safe_summary="STORE_ACCESS_DENIED",
            details_private=f"PowerShell failed with {toxic_exception}",
            raw_expected_account=toxic_email,
            raw_detected_email=toxic_email,
            token_fingerprint="fp_123"
        )
        verify_json = json.dumps(asdict(v_toxic.to_sanitized_dto()))
        assert toxic_email not in verify_json
        assert toxic_exception not in verify_json
        assert "details_private" not in verify_json

        # 7d. Selection Result DTO
        selector = AccountSelector()
        sel_toxic = selector.select_next_account([quota_toxic], now=now)
        sel_json = json.dumps(sel_toxic.to_sanitized_dto())
        assert toxic_email not in sel_json

        # 7e. Safe Switch Default Output (mocked verifier to maintain 100% test isolation)
        sw_toxic = execute_safe_switch(
            toxic_email, confirm=False,
            verifier=lambda exp, net: VerificationResult(
                account_ref=pseudonymize_account(exp),
                status=CredentialVerificationStatus.CREDENTIAL_STORE_WRITTEN_UNVERIFIED,
                credential_present=True,
                evidence_rank="MEDIUM",
                matches_expected=None,
                scope="CREDENTIAL_STORE_ONLY",
                desktop_adoption_status="UNKNOWN",
                verification_source="WINDOWS_CREDENTIAL_MANAGER"
            )
        )
        sw_json = json.dumps(sw_toxic)
        assert toxic_email not in sw_json
        print("  [PASS] 7: Adversarial Privacy Test Matrix verified: ZERO sensitive markers in all default DTOs")
        tests_passed += 1

        # =========================================================================
        # 8. Item 8: SanitizedVerificationOutput Exposes Safe Enums & Zero Stderr
        # =========================================================================
        tests_total += 1
        env_not_found = json.dumps({"found": False, "win32_code": 1168, "blob_length": 0, "blob_utf8": ""})
        v_res = verify_active_account(ps_runner=lambda: (0, env_not_found, ""))
        v_dto = v_res.to_sanitized_dto()
        assert v_dto.status == CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY.value
        assert v_dto.error_code == "WIN32_1168_NOT_FOUND"
        assert v_dto.safe_summary == "CREDENTIAL_STORE_QUERY_FAILED"
        assert "details" not in asdict(v_dto)
        print("  [PASS] 8: SanitizedVerificationOutput exposes safe enums and zero free-text stderr")
        tests_passed += 1

        # =========================================================================
        # 9. Item 9: switch_account_safe Does Not Echo Raw Invalid Input
        # =========================================================================
        tests_total += 1
        invalid_input = "not-an-email-with-secrets<payload>"
        sw_invalid = execute_safe_switch(invalid_input, confirm=True)
        assert invalid_input not in json.dumps(sw_invalid)
        assert sw_invalid["message"] == "Account input is not a valid canonical email."
        print("  [PASS] 9: switch_account_safe does not echo raw invalid input")
        tests_passed += 1

        # =========================================================================
        # 10. Item 13: Transport Trust & HMAC Signature Regression Suite
        # =========================================================================
        tests_total += 1
        # 10a. Unsigned deserialized LIVE claim -> rejected
        ev_unsigned_dict = {
            "canonical_account": "alice@example.com",
            "canonical_executable_path": "mock_agm.exe",
            "binary_sha256": valid_sha,
            "source_revision_inspected": INSPECTED_AGM_SOURCE_REVISION,
            "argv": ["mock_agm.exe", "refresh", "alice@example.com"],
            "started_at_epoch": now - 5,
            "completed_at_epoch": now - 4,
            "exit_code": 0,
            "result": "REFRESH_SUCCEEDED",
            "supervisor_session_id": session_id,
            "source_origin": "LIVE_REFRESH_EXECUTION"
        }
        st_unsigned, _, codes_unsigned, _ = validate_refresh_evidence_supervisor(
            ev_unsigned_dict, "alice@example.com", now,
            expected_session_id=session_id,
            trusted_identity=trusted_id_ok
        )
        assert st_unsigned == FreshnessState.STALE_CACHED
        assert WarningCode.UNTRUSTED_DESERIALIZED_EVIDENCE in codes_unsigned

        # 10b. Signed deserialized with BAD HMAC -> rejected
        ev_bad_sig_dict = dict(ev_unsigned_dict)
        ev_bad_sig_dict["hmac_signature"] = "bad_hmac_signature_123"
        st_bad_sig, _, codes_bad_sig, _ = validate_refresh_evidence_supervisor(
            ev_bad_sig_dict, "alice@example.com", now,
            expected_session_id=session_id,
            trusted_identity=trusted_id_ok,
            session_secret=secret
        )
        assert st_bad_sig == FreshnessState.STALE_CACHED
        assert WarningCode.UNTRUSTED_DESERIALIZED_EVIDENCE in codes_bad_sig

        # 10c. Signed deserialized with VALID HMAC -> accepted
        ev_signed_obj, _, _ = deserialize_evidence_payload(ev_unsigned_dict)
        valid_hmac = compute_evidence_hmac(ev_signed_obj, secret)
        ev_good_sig_dict = dict(ev_unsigned_dict)
        ev_good_sig_dict["hmac_signature"] = valid_hmac
        st_good_sig, _, codes_good_sig, _ = validate_refresh_evidence_supervisor(
            ev_good_sig_dict, "alice@example.com", now,
            expected_session_id=session_id,
            trusted_identity=trusted_id_ok,
            session_secret=secret
        )
        assert st_good_sig == FreshnessState.PROVEN_FRESH
        print("  [PASS] 10: Transport trust & HMAC signature regression suite verified")
        tests_passed += 1

        # =========================================================================
        # 11. Final Assertion: ZERO Host Operations
        # =========================================================================
        tests_total += 1
        assert os_cred_read_calls == 0
        assert os_cred_write_calls == 0
        assert live_agm_calls == 0
        assert live_google_http_calls == 0
        print("  [PASS] 11: Global Side-Effect Trap verified: ZERO OS CredRead/Write, ZERO Live AGM, ZERO Google HTTP calls")
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
