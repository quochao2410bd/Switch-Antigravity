import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
T02 = os.path.abspath(os.path.join(HERE, "..", "t02"))
if T02 not in sys.path:
    sys.path.insert(0, T02)

from inspect_quota import FreshnessState, WarningCode, validate_refresh_evidence_supervisor
from refresh_quota_safe import execute_refresh_for_test
from switch_account_safe import SwitchOutcome, execute_safe_switch
from trusted_agm_runner import RunnerErrorCode, TrustedAgmIdentity, execute_trusted_agm
from verify_active_account import CredentialVerificationStatus, parse_credential_envelope_output


VALID_SHA = "a" * 64
OTHER_SHA = "b" * 64


class T02SupervisorContractTests(unittest.TestCase):
    def test_missing_trusted_identity_executes_zero_subprocesses(self):
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            return 0, "ok", ""

        result = execute_trusted_agm(
            ["list"],
            trusted_identity=None,
            injected_runner=runner,
            injected_resolver=lambda: "C:/synthetic/agm.exe",
            injected_sha_computer=lambda path: VALID_SHA,
        )
        self.assertFalse(result.command_executed)
        self.assertEqual(result.error_code, RunnerErrorCode.BINARY_IDENTITY_UNCONFIGURED)
        self.assertEqual(calls, [])

    def test_binary_hash_mismatch_executes_zero_subprocesses(self):
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            return 0, "ok", ""

        result = execute_trusted_agm(
            ["list"],
            trusted_identity=TrustedAgmIdentity(expected_binary_sha256=OTHER_SHA),
            injected_runner=runner,
            injected_resolver=lambda: "C:/synthetic/agm.exe",
            injected_sha_computer=lambda path: VALID_SHA,
        )
        self.assertFalse(result.command_executed)
        self.assertEqual(result.error_code, RunnerErrorCode.BINARY_IDENTITY_MISMATCH)
        self.assertEqual(calls, [])

    def test_forged_deserialized_live_claim_cannot_become_proven_fresh(self):
        payload = {
            "canonical_account": "alice@example.com",
            "canonical_executable_path": "C:/synthetic/agm.exe",
            "binary_sha256": VALID_SHA,
            "source_revision_inspected": "1d3ce8497e36ffa60c3b4e369168315a7ae4d469",
            "argv": ["C:/synthetic/agm.exe", "refresh", "alice@example.com"],
            "started_at_epoch": 100.0,
            "completed_at_epoch": 101.0,
            "exit_code": 0,
            "result": "REFRESH_SUCCEEDED",
            "supervisor_session_id": "session-test",
            "source_origin": "LIVE_REFRESH_EXECUTION",
        }
        state, _, codes, _ = validate_refresh_evidence_supervisor(
            payload,
            canonical_account="alice@example.com",
            now_epoch=102.0,
            expected_session_id="session-test",
            trusted_identity=TrustedAgmIdentity(
                expected_binary_sha256=VALID_SHA,
                canonical_executable_path="C:/synthetic/agm.exe",
            ),
        )
        self.assertEqual(state, FreshnessState.STALE_CACHED)
        self.assertIn(WarningCode.UNTRUSTED_DESERIALIZED_EVIDENCE, codes)

    def test_synthetic_refresh_cannot_become_production_fresh(self):
        evidence = execute_refresh_for_test(
            "alice@example.com",
            "session-test",
            agm_runner=lambda argv, timeout: (0, "ok", ""),
            mock_binary_path="C:/synthetic/agm.exe",
            mock_binary_sha256=VALID_SHA,
            clock=lambda: 100.0,
        )
        state, _, codes, _ = validate_refresh_evidence_supervisor(
            evidence,
            canonical_account="alice@example.com",
            now_epoch=101.0,
            expected_session_id="session-test",
            trusted_identity=TrustedAgmIdentity(
                expected_binary_sha256=VALID_SHA,
                canonical_executable_path="C:/synthetic/agm.exe",
            ),
        )
        self.assertEqual(state, FreshnessState.STALE_CACHED)
        self.assertIn(WarningCode.SYNTHETIC_TEST_REJECTED, codes)

    def test_blank_credential_reader_output_is_not_classified_empty_store(self):
        payload, status, code, _ = parse_credential_envelope_output(0, "", "")
        self.assertIsNone(payload)
        self.assertEqual(status, CredentialVerificationStatus.POWERSHELL_PROCESS_FAILED)
        self.assertEqual(code, "EMPTY_STDOUT")

    def test_explicit_win32_not_found_is_classified_empty_store(self):
        envelope = json.dumps({
            "found": False,
            "win32_code": 1168,
            "blob_length": 0,
            "blob_utf8": "",
        })
        payload, status, code, _ = parse_credential_envelope_output(0, envelope, "")
        self.assertIsNone(payload)
        self.assertEqual(status, CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY)
        self.assertEqual(code, "WIN32_1168_NOT_FOUND")

    def test_wildcard_switch_is_rejected_before_any_runner(self):
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            return 0, "ok", ""

        result = execute_safe_switch("*", confirm=True, agm_runner=runner)
        self.assertEqual(result["status"], SwitchOutcome.WILDCARD_REJECTED.value)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
