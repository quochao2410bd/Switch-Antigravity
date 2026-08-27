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

    # =========================================================================
    # Explicit Canonical Path Policy Precedence Tests (Items 4-10)
    # =========================================================================

    def test_explicit_canonical_path_wins_over_conflicting_temp_binary(self):
        """Item 4: Configured canonical path is used even when generic discovery would return Temp binary."""
        canonical_approved_path = os.path.abspath("C:/approved/.local/bin/agm.exe")
        temp_unapproved_path = os.path.abspath("C:/Temp/agm.exe")
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            return 0, "ok", ""

        def fake_sha_computer(path):
            if os.path.abspath(path) == canonical_approved_path:
                return VALID_SHA
            if os.path.abspath(path) == temp_unapproved_path:
                return OTHER_SHA
            return None

        # Generic resolver returns Temp binary with wrong SHA
        def fake_resolver():
            return temp_unapproved_path

        result = execute_trusted_agm(
            ["list"],
            trusted_identity=TrustedAgmIdentity(
                expected_binary_sha256=VALID_SHA,
                canonical_executable_path=canonical_approved_path,
            ),
            injected_runner=runner,
            injected_resolver=fake_resolver,
            injected_sha_computer=fake_sha_computer,
        )
        self.assertTrue(result.command_executed)
        self.assertTrue(result.success)
        self.assertEqual(result.canonical_executable_path, canonical_approved_path)
        self.assertEqual(result.observed_sha_pre, VALID_SHA)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], canonical_approved_path)

    def test_explicit_canonical_path_wins_over_conflicting_path_binary(self):
        """Item 5: Explicit configured path wins over stale binary found on PATH."""
        canonical_approved_path = os.path.abspath("C:/Users/user/.local/bin/agm.exe")
        stale_path_binary = os.path.abspath("C:/stale/bin/agm.exe")
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            return 0, "ok", ""

        def fake_sha_computer(path):
            if os.path.abspath(path) == canonical_approved_path:
                return VALID_SHA
            return OTHER_SHA

        result = execute_trusted_agm(
            ["list"],
            trusted_identity=TrustedAgmIdentity(
                expected_binary_sha256=VALID_SHA,
                canonical_executable_path=canonical_approved_path,
            ),
            injected_runner=runner,
            injected_resolver=lambda: stale_path_binary,
            injected_sha_computer=fake_sha_computer,
        )
        self.assertTrue(result.command_executed)
        self.assertEqual(result.canonical_executable_path, canonical_approved_path)
        self.assertEqual(calls[0][0], canonical_approved_path)

    def test_missing_configured_canonical_path_fails_closed_zero_subprocesses(self):
        """Item 6: Missing configured path fails closed with zero subprocess calls and no fallback to generic resolver."""
        missing_canonical_path = os.path.abspath("C:/approved/.local/bin/missing_agm.exe")
        temp_binary_path = os.path.abspath("C:/Temp/agm.exe")
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            return 0, "ok", ""

        def fake_sha_computer(path):
            # Missing path returns None
            if os.path.abspath(path) == missing_canonical_path:
                return None
            return VALID_SHA

        result = execute_trusted_agm(
            ["list"],
            trusted_identity=TrustedAgmIdentity(
                expected_binary_sha256=VALID_SHA,
                canonical_executable_path=missing_canonical_path,
            ),
            injected_runner=runner,
            injected_resolver=lambda: temp_binary_path,  # Must NOT fall back to this!
            injected_sha_computer=fake_sha_computer,
        )
        self.assertFalse(result.command_executed)
        self.assertEqual(result.error_code, RunnerErrorCode.CANONICAL_PATH_NOT_FOUND)
        self.assertEqual(calls, [])

    def test_wrong_hash_at_configured_canonical_path_executes_zero_subprocesses(self):
        """Item 7: Configured path exists but has wrong hash -> BINARY_IDENTITY_MISMATCH, zero subprocess calls."""
        canonical_path = os.path.abspath("C:/approved/.local/bin/agm.exe")
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            return 0, "ok", ""

        result = execute_trusted_agm(
            ["list"],
            trusted_identity=TrustedAgmIdentity(
                expected_binary_sha256=VALID_SHA,
                canonical_executable_path=canonical_path,
            ),
            injected_runner=runner,
            injected_sha_computer=lambda path: OTHER_SHA,
        )
        self.assertFalse(result.command_executed)
        self.assertEqual(result.error_code, RunnerErrorCode.BINARY_IDENTITY_MISMATCH)
        self.assertEqual(calls, [])

    def test_correct_canonical_path_and_correct_hash_executes_successfully(self):
        """Item 8: Configured path exists and SHA matches -> executes once with exact canonical path."""
        canonical_path = os.path.abspath("C:/approved/.local/bin/agm.exe")
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            return 0, "ok", ""

        result = execute_trusted_agm(
            ["list"],
            trusted_identity=TrustedAgmIdentity(
                expected_binary_sha256=VALID_SHA,
                canonical_executable_path=canonical_path,
            ),
            injected_runner=runner,
            injected_sha_computer=lambda path: VALID_SHA,
        )
        self.assertTrue(result.command_executed)
        self.assertTrue(result.success)
        self.assertEqual(result.observed_sha_pre, VALID_SHA)
        self.assertEqual(result.observed_sha_post, VALID_SHA)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], canonical_path)

    def test_no_canonical_path_policy_falls_back_to_generic_discovery(self):
        """Item 9: When canonical_executable_path is None, generic discovery is used."""
        discovered_path = os.path.abspath("C:/discovered/agm.exe")
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            return 0, "ok", ""

        result = execute_trusted_agm(
            ["list"],
            trusted_identity=TrustedAgmIdentity(
                expected_binary_sha256=VALID_SHA,
                canonical_executable_path=None,
            ),
            injected_runner=runner,
            injected_resolver=lambda: discovered_path,
            injected_sha_computer=lambda path: VALID_SHA,
        )
        self.assertTrue(result.command_executed)
        self.assertEqual(result.canonical_executable_path, discovered_path)
        self.assertEqual(calls[0][0], discovered_path)

    def test_injected_generic_resolver_never_invoked_when_canonical_path_set(self):
        """Item 10: Injected resolver raising AssertionError is never invoked when explicit path is set."""
        canonical_path = os.path.abspath("C:/approved/.local/bin/agm.exe")
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            return 0, "ok", ""

        def explosive_resolver():
            raise AssertionError("CRITICAL VIOLATION: Generic resolver must never be invoked when canonical path is configured!")

        result = execute_trusted_agm(
            ["list"],
            trusted_identity=TrustedAgmIdentity(
                expected_binary_sha256=VALID_SHA,
                canonical_executable_path=canonical_path,
            ),
            injected_runner=runner,
            injected_resolver=explosive_resolver,
            injected_sha_computer=lambda path: VALID_SHA,
        )
        self.assertTrue(result.command_executed)
        self.assertEqual(result.canonical_executable_path, canonical_path)


if __name__ == "__main__":
    unittest.main()
