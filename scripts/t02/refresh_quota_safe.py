#!/usr/bin/env python3
"""
refresh_quota_safe.py

Production contract and safe executor for AGM quota refresh operations (Round 7 Architecture).

Key Architectural & Privacy Guarantees:
1. Pre-Execution Binary Trust Gate (Critical Item 1 & 2):
   - All live executions go through TrustedAgmRunner.
   - Expected binary SHA-256 is validated BEFORE any subprocess execution.
   - Mismatched or unconfigured binary results in ZERO subprocess calls.
2. Sanitized Refresh Output Contract (Critical Item 6 & 11):
   - Default CLI/supervisor output uses SanitizedRefreshEvidenceDTO (pseudonymous account_ref only).
   - error_code contains ONLY safe normalized enums/codes (NO free-text exceptions, emails, or paths).
   - Raw emails, capability tokens, session secrets, and process streams are strictly isolated behind --private-diagnostic-mode.
3. Process-Local TCB Model (Items 3 & 4):
   - The main supervisor Python process is the Trusted Computing Base (TCB).
   - LiveExecutionAttestation acts as a misuse/accidental-call guard within the supervisor.
4. Clean Production Live Origin (Item 5):
   - Production live path executes real binary via TrustedAgmRunner without test injection hooks.
   - Test execution uses execute_refresh_for_test() minting SYNTHETIC_TEST_EVIDENCE.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple, Union

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trusted_agm_runner import (
    INSPECTED_AGM_SOURCE_REVISION,
    RunnerErrorCode,
    TrustedAgmIdentity,
    TrustedExecutionResult,
    compute_file_sha256,
    execute_trusted_agm,
    find_canonical_agm_executable,
)

# Module-private ephemeral secret for process-local capability attestation (TCB guard)
_EXECUTOR_ATTESTATION_SECRET: str = secrets.token_hex(32)
_SUPERVISOR_SESSION_NONCE: str = secrets.token_hex(16)


class EvidenceSourceOrigin(str, Enum):
    LIVE_REFRESH_EXECUTION = "LIVE_REFRESH_EXECUTION"
    SYNTHETIC_TEST_EVIDENCE = "SYNTHETIC_TEST_EVIDENCE"
    DRY_RUN = "DRY_RUN"


class TransportTrustClass(str, Enum):
    PROCESS_LOCAL = "PROCESS_LOCAL"
    SIGNED_DESERIALIZED = "SIGNED_DESERIALIZED"
    UNTRUSTED_DESERIALIZED = "UNTRUSTED_DESERIALIZED"


class RefreshResult(str, Enum):
    REFRESH_SUCCEEDED = "REFRESH_SUCCEEDED"
    REFRESH_FAILED_AUTH = "REFRESH_FAILED_AUTH"
    REFRESH_FAILED_NETWORK = "REFRESH_FAILED_NETWORK"
    REFRESH_FAILED_ACCOUNT_NOT_FOUND = "REFRESH_FAILED_ACCOUNT_NOT_FOUND"
    REFRESH_FAILED_UNKNOWN = "REFRESH_FAILED_UNKNOWN"
    BINARY_IDENTITY_UNCONFIGURED = "BINARY_IDENTITY_UNCONFIGURED"
    BINARY_IDENTITY_CONFIG_INVALID = "BINARY_IDENTITY_CONFIG_INVALID"
    BINARY_IDENTITY_MISMATCH = "BINARY_IDENTITY_MISMATCH"
    BINARY_IDENTITY_UNVERIFIED = "BINARY_IDENTITY_UNVERIFIED"
    BINARY_CHANGED_DURING_EXECUTION = "BINARY_CHANGED_DURING_EXECUTION"
    AGM_NOT_FOUND = "AGM_NOT_FOUND"
    INVALID_ACCOUNT = "INVALID_ACCOUNT"
    DRY_RUN = "DRY_RUN"


@dataclass
class LiveExecutionAttestation:
    """Process-local capability attestation (misuse guard within supervisor TCB)."""
    session_nonce: str
    execution_nonce: str
    account: str
    binary_sha256: str
    issued_at: float
    capability_token: str


@dataclass
class SanitizedRefreshEvidenceDTO:
    """Safe supervisor DTO containing zero raw emails, free text, or capability tokens (Critical Item 6)."""
    account_ref: str
    result: str
    exit_code: int
    started_at_epoch: float
    completed_at_epoch: float
    duration_sec: float
    source_origin: str
    transport_trust: str
    error_code: Optional[str] = None  # Normalized code only! No free-text strings!


@dataclass
class RefreshEvidence:
    canonical_account: str
    canonical_executable_path: str
    binary_sha256: str
    source_revision_inspected: str
    argv: List[str]
    started_at_epoch: float
    completed_at_epoch: float
    exit_code: int
    result: RefreshResult
    supervisor_session_id: str
    source_origin: EvidenceSourceOrigin
    transport_trust: TransportTrustClass = TransportTrustClass.PROCESS_LOCAL
    attestation: Optional[LiveExecutionAttestation] = None
    hmac_signature: Optional[str] = None
    error_code: Optional[str] = None
    error_summary_private: Optional[str] = None  # Private diagnostics only

    def to_sanitized_dto(self) -> SanitizedRefreshEvidenceDTO:
        """Converts to safe supervisor DTO guaranteed free of raw emails or capability tokens."""
        return SanitizedRefreshEvidenceDTO(
            account_ref=pseudonymize_account(self.canonical_account),
            result=self.result.value,
            exit_code=self.exit_code,
            started_at_epoch=self.started_at_epoch,
            completed_at_epoch=self.completed_at_epoch,
            duration_sec=max(0.0, self.completed_at_epoch - self.started_at_epoch),
            source_origin=self.source_origin.value,
            transport_trust=self.transport_trust.value,
            error_code=self.error_code
        )

    def to_private_diagnostic_dict(self) -> dict:
        """Explicit diagnostic extraction including raw accounts and internal provenance."""
        d = asdict(self)
        d["result"] = self.result.value
        d["source_origin"] = self.source_origin.value
        d["transport_trust"] = self.transport_trust.value
        if self.attestation:
            d["attestation"] = asdict(self.attestation)
        return d


def pseudonymize_account(account: Optional[str]) -> str:
    """Generates a stable local pseudonymous identifier (acc_<sha256_prefix>)."""
    if not account:
        return "acc_none"
    h = hashlib.sha256(account.strip().lower().encode("utf-8")).hexdigest()[:12]
    return f"acc_{h}"


def issue_live_execution_attestation(
    account: str,
    binary_sha256: str,
    issued_at: float
) -> LiveExecutionAttestation:
    """Issues a process-local attestation capability."""
    exec_nonce = str(uuid.uuid4())
    payload = f"{_SUPERVISOR_SESSION_NONCE}|{exec_nonce}|{account.lower()}|{binary_sha256}|{issued_at:.4f}"
    cap_token = hmac.new(_EXECUTOR_ATTESTATION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return LiveExecutionAttestation(
        session_nonce=_SUPERVISOR_SESSION_NONCE,
        execution_nonce=exec_nonce,
        account=account.lower(),
        binary_sha256=binary_sha256,
        issued_at=issued_at,
        capability_token=cap_token
    )


def verify_live_execution_attestation(
    attestation: Optional[LiveExecutionAttestation],
    expected_account: str,
    expected_binary_sha256: str
) -> bool:
    """Verifies that an attestation capability was issued within this supervisor process."""
    if not attestation:
        return False
    if attestation.session_nonce != _SUPERVISOR_SESSION_NONCE:
        return False
    if attestation.account != expected_account.lower():
        return False
    if attestation.binary_sha256.lower() != expected_binary_sha256.lower():
        return False

    payload = f"{attestation.session_nonce}|{attestation.execution_nonce}|{attestation.account}|{attestation.binary_sha256}|{attestation.issued_at:.4f}"
    expected_token = hmac.new(_EXECUTOR_ATTESTATION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(attestation.capability_token, expected_token)


def compute_evidence_hmac(evidence: RefreshEvidence, session_secret: str) -> str:
    """Computes HMAC-SHA256 signature over all canonical evidence fields."""
    canonical_payload = (
        f"{evidence.canonical_account}|{evidence.canonical_executable_path}|{evidence.binary_sha256}|"
        f"{evidence.source_revision_inspected}|{json.dumps(evidence.argv)}|{evidence.started_at_epoch:.4f}|"
        f"{evidence.completed_at_epoch:.4f}|{evidence.exit_code}|{evidence.result.value}|"
        f"{evidence.supervisor_session_id}|{evidence.source_origin.value}"
    )
    return hmac.new(session_secret.encode("utf-8"), canonical_payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_evidence_signature(evidence: RefreshEvidence, session_secret: str) -> bool:
    """Verifies HMAC-SHA256 signature of a deserialized RefreshEvidence record."""
    if not evidence.hmac_signature or not session_secret:
        return False
    expected = compute_evidence_hmac(evidence, session_secret)
    return hmac.compare_digest(evidence.hmac_signature, expected)


def is_canonical_email(account: str) -> bool:
    """Validate canonical RFC 5322 email format."""
    if not account or "@" not in account:
        return False
    parts = account.strip().split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False
    if "." not in parts[1] or parts[1].startswith(".") or parts[1].endswith("."):
        return False
    if any(c in account for c in " \t\r\n'\"<>"):
        return False
    return True


def classify_refresh_failure(stdout: str, stderr: str, exit_code: int) -> Tuple[RefreshResult, str]:
    combined = f"{stdout}\n{stderr}".lower()
    if "invalid_grant" in combined or "unauthorized" in combined or "token expired" in combined or "auth" in combined:
        return RefreshResult.REFRESH_FAILED_AUTH, "REFRESH_AUTH_FAILED"
    if "timeout" in combined or "connection refused" in combined or "no such host" in combined or "network" in combined or "i/o timeout" in combined:
        return RefreshResult.REFRESH_FAILED_NETWORK, "REFRESH_NETWORK_FAILED"
    if "not found" in combined or "no account" in combined or "does not exist" in combined:
        return RefreshResult.REFRESH_FAILED_ACCOUNT_NOT_FOUND, "ACCOUNT_NOT_FOUND"
    return RefreshResult.REFRESH_FAILED_UNKNOWN, f"REFRESH_FAILED_CODE_{exit_code}"


def _execute_live_refresh_sealed(
    account: str,
    supervisor_session_id: str,
    trusted_identity: Optional[TrustedAgmIdentity],
    timeout_sec: int = 15
) -> RefreshEvidence:
    """
    Production live refresh execution (Round 7).
    Enforces PRE-EXECUTION binary verification through TrustedAgmRunner.
    ZERO subprocess calls if trusted_identity is missing, malformed, or mismatched.
    """
    start_t = time.time()
    if not is_canonical_email(account):
        end_t = time.time()
        return RefreshEvidence(
            canonical_account=account,
            canonical_executable_path="none",
            binary_sha256="none",
            source_revision_inspected=INSPECTED_AGM_SOURCE_REVISION,
            argv=[],
            started_at_epoch=start_t,
            completed_at_epoch=end_t,
            exit_code=1,
            result=RefreshResult.INVALID_ACCOUNT,
            supervisor_session_id=supervisor_session_id,
            source_origin=EvidenceSourceOrigin.LIVE_REFRESH_EXECUTION,
            transport_trust=TransportTrustClass.PROCESS_LOCAL,
            error_code="INVALID_ACCOUNT",
            error_summary_private="Invalid canonical email format"
        )

    # Invoke through shared trusted runner (handles pre-check, execution, and TOCTOU)
    exec_res = execute_trusted_agm(
        subcommand_args=["refresh", account],
        trusted_identity=trusted_identity,
        timeout_sec=timeout_sec
    )

    if not exec_res.command_executed:
        # Pre-execution gate rejected binary before subprocess execution
        if exec_res.error_code == RunnerErrorCode.BINARY_IDENTITY_UNCONFIGURED:
            res_val = RefreshResult.BINARY_IDENTITY_UNCONFIGURED
        elif exec_res.error_code == RunnerErrorCode.BINARY_IDENTITY_CONFIG_INVALID:
            res_val = RefreshResult.BINARY_IDENTITY_CONFIG_INVALID
        elif exec_res.error_code == RunnerErrorCode.BINARY_IDENTITY_MISMATCH:
            res_val = RefreshResult.BINARY_IDENTITY_MISMATCH
        elif exec_res.error_code == RunnerErrorCode.AGM_NOT_FOUND:
            res_val = RefreshResult.AGM_NOT_FOUND
        else:
            res_val = RefreshResult.BINARY_IDENTITY_UNVERIFIED

        return RefreshEvidence(
            canonical_account=account,
            canonical_executable_path=exec_res.canonical_executable_path,
            binary_sha256=exec_res.observed_sha_pre,
            source_revision_inspected=INSPECTED_AGM_SOURCE_REVISION,
            argv=[exec_res.canonical_executable_path, "refresh", account],
            started_at_epoch=exec_res.started_at_epoch,
            completed_at_epoch=exec_res.completed_at_epoch,
            exit_code=exec_res.exit_code,
            result=res_val,
            supervisor_session_id=supervisor_session_id,
            source_origin=EvidenceSourceOrigin.LIVE_REFRESH_EXECUTION,
            transport_trust=TransportTrustClass.PROCESS_LOCAL,
            error_code=exec_res.error_code.value,
            error_summary_private=exec_res.stderr
        )

    if exec_res.error_code == RunnerErrorCode.BINARY_CHANGED_DURING_EXECUTION:
        return RefreshEvidence(
            canonical_account=account,
            canonical_executable_path=exec_res.canonical_executable_path,
            binary_sha256="MUTATED_DURING_EXECUTION",
            source_revision_inspected=INSPECTED_AGM_SOURCE_REVISION,
            argv=[exec_res.canonical_executable_path, "refresh", account],
            started_at_epoch=exec_res.started_at_epoch,
            completed_at_epoch=exec_res.completed_at_epoch,
            exit_code=1,
            result=RefreshResult.BINARY_CHANGED_DURING_EXECUTION,
            supervisor_session_id=supervisor_session_id,
            source_origin=EvidenceSourceOrigin.LIVE_REFRESH_EXECUTION,
            transport_trust=TransportTrustClass.PROCESS_LOCAL,
            error_code="BINARY_CHANGED_DURING_EXECUTION",
            error_summary_private=exec_res.stderr
        )

    if exec_res.success:
        res_enum = RefreshResult.REFRESH_SUCCEEDED
        err_code = None
        attestation = issue_live_execution_attestation(account, exec_res.observed_sha_pre, exec_res.completed_at_epoch)
    else:
        res_enum, err_code = classify_refresh_failure(exec_res.stdout, exec_res.stderr, exec_res.exit_code)
        attestation = None

    return RefreshEvidence(
        canonical_account=account,
        canonical_executable_path=exec_res.canonical_executable_path,
        binary_sha256=exec_res.observed_sha_pre,
        source_revision_inspected=INSPECTED_AGM_SOURCE_REVISION,
        argv=[exec_res.canonical_executable_path, "refresh", account],
        started_at_epoch=exec_res.started_at_epoch,
        completed_at_epoch=exec_res.completed_at_epoch,
        exit_code=exec_res.exit_code,
        result=res_enum,
        supervisor_session_id=supervisor_session_id,
        source_origin=EvidenceSourceOrigin.LIVE_REFRESH_EXECUTION,
        transport_trust=TransportTrustClass.PROCESS_LOCAL,
        attestation=attestation,
        error_code=err_code,
        error_summary_private=exec_res.stderr if not exec_res.success else None
    )


def execute_refresh_for_test(
    account: str,
    supervisor_session_id: str,
    agm_runner: Optional[Callable[[List[str], int], Tuple[int, str, str]]] = None,
    mock_binary_path: str = "mock_agm.exe",
    mock_binary_sha256: str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    clock: Optional[Callable[[], float]] = None,
    timeout_sec: int = 15
) -> RefreshEvidence:
    """
    Test-only refresh executor.
    STRUCTURALLY INCAPABLE of minting LIVE_REFRESH_EXECUTION.
    Always mints source_origin = SYNTHETIC_TEST_EVIDENCE without live attestation.
    """
    get_time = clock or time.time
    start_t = get_time()

    if not supervisor_session_id or not supervisor_session_id.strip():
        end_t = get_time()
        return RefreshEvidence(
            canonical_account=account,
            canonical_executable_path=mock_binary_path,
            binary_sha256=mock_binary_sha256,
            source_revision_inspected=INSPECTED_AGM_SOURCE_REVISION,
            argv=[mock_binary_path, "refresh", account],
            started_at_epoch=start_t,
            completed_at_epoch=end_t,
            exit_code=1,
            result=RefreshResult.REFRESH_FAILED_UNKNOWN,
            supervisor_session_id="INVALID_SESSION",
            source_origin=EvidenceSourceOrigin.SYNTHETIC_TEST_EVIDENCE,
            transport_trust=TransportTrustClass.PROCESS_LOCAL,
            attestation=None,
            error_code="SESSION_MISSING"
        )

    if not is_canonical_email(account):
        end_t = get_time()
        return RefreshEvidence(
            canonical_account=account,
            canonical_executable_path=mock_binary_path,
            binary_sha256=mock_binary_sha256,
            source_revision_inspected=INSPECTED_AGM_SOURCE_REVISION,
            argv=[mock_binary_path, "refresh", account],
            started_at_epoch=start_t,
            completed_at_epoch=end_t,
            exit_code=1,
            result=RefreshResult.INVALID_ACCOUNT,
            supervisor_session_id=supervisor_session_id,
            source_origin=EvidenceSourceOrigin.SYNTHETIC_TEST_EVIDENCE,
            transport_trust=TransportTrustClass.PROCESS_LOCAL,
            attestation=None,
            error_code="INVALID_ACCOUNT"
        )

    if not mock_binary_sha256 or mock_binary_sha256 == "UNKNOWN_SHA256":
        end_t = get_time()
        return RefreshEvidence(
            canonical_account=account,
            canonical_executable_path=mock_binary_path,
            binary_sha256="UNKNOWN_SHA256",
            source_revision_inspected=INSPECTED_AGM_SOURCE_REVISION,
            argv=[mock_binary_path, "refresh", account],
            started_at_epoch=start_t,
            completed_at_epoch=end_t,
            exit_code=1,
            result=RefreshResult.BINARY_IDENTITY_UNVERIFIED,
            supervisor_session_id=supervisor_session_id,
            source_origin=EvidenceSourceOrigin.SYNTHETIC_TEST_EVIDENCE,
            transport_trust=TransportTrustClass.PROCESS_LOCAL,
            attestation=None,
            error_code="BINARY_IDENTITY_UNVERIFIED"
        )

    argv = [mock_binary_path, "refresh", account]
    if agm_runner is not None:
        try:
            exit_code, stdout, stderr = agm_runner(argv, timeout_sec)
            end_t = get_time()
            if exit_code == 0:
                res_enum = RefreshResult.REFRESH_SUCCEEDED
                err_code = None
            else:
                res_enum, err_code = classify_refresh_failure(stdout, stderr, exit_code)

            return RefreshEvidence(
                canonical_account=account,
                canonical_executable_path=mock_binary_path,
                binary_sha256=mock_binary_sha256,
                source_revision_inspected=INSPECTED_AGM_SOURCE_REVISION,
                argv=argv,
                started_at_epoch=start_t,
                completed_at_epoch=end_t,
                exit_code=exit_code,
                result=res_enum,
                supervisor_session_id=supervisor_session_id,
                source_origin=EvidenceSourceOrigin.SYNTHETIC_TEST_EVIDENCE,
                transport_trust=TransportTrustClass.PROCESS_LOCAL,
                attestation=None,
                error_code=err_code,
                error_summary_private=stderr if exit_code != 0 else None
            )
        except Exception as e:
            end_t = get_time()
            return RefreshEvidence(
                canonical_account=account,
                canonical_executable_path=mock_binary_path,
                binary_sha256=mock_binary_sha256,
                source_revision_inspected=INSPECTED_AGM_SOURCE_REVISION,
                argv=argv,
                started_at_epoch=start_t,
                completed_at_epoch=end_t,
                exit_code=1,
                result=RefreshResult.REFRESH_FAILED_UNKNOWN,
                supervisor_session_id=supervisor_session_id,
                source_origin=EvidenceSourceOrigin.SYNTHETIC_TEST_EVIDENCE,
                transport_trust=TransportTrustClass.PROCESS_LOCAL,
                attestation=None,
                error_code="RUNNER_EXCEPTION",
                error_summary_private=str(e)
            )

    end_t = get_time() + 0.01
    return RefreshEvidence(
        canonical_account=account,
        canonical_executable_path=mock_binary_path,
        binary_sha256=mock_binary_sha256,
        source_revision_inspected=INSPECTED_AGM_SOURCE_REVISION,
        argv=argv,
        started_at_epoch=start_t,
        completed_at_epoch=end_t,
        exit_code=0,
        result=RefreshResult.REFRESH_SUCCEEDED,
        supervisor_session_id=supervisor_session_id,
        source_origin=EvidenceSourceOrigin.SYNTHETIC_TEST_EVIDENCE,
        transport_trust=TransportTrustClass.PROCESS_LOCAL,
        attestation=None,
        error_code=None
    )


def execute_safe_refresh(
    account: str,
    supervisor_session_id: str,
    trusted_identity: Optional[TrustedAgmIdentity] = None,
    live_network: bool = False,
    timeout_sec: int = 15
) -> RefreshEvidence:
    """
    Production entry point for AGM quota refresh.
    If live_network is False, performs a safe dry-run (DRY_RUN origin).
    If live_network is True, calls the sealed live refresh execution path requiring trusted_identity.
    """
    if not supervisor_session_id or not supervisor_session_id.strip():
        now_t = time.time()
        return RefreshEvidence(
            canonical_account=account,
            canonical_executable_path="none",
            binary_sha256="none",
            source_revision_inspected=INSPECTED_AGM_SOURCE_REVISION,
            argv=[],
            started_at_epoch=now_t,
            completed_at_epoch=now_t,
            exit_code=1,
            result=RefreshResult.REFRESH_FAILED_UNKNOWN,
            supervisor_session_id="INVALID_SESSION",
            source_origin=EvidenceSourceOrigin.DRY_RUN,
            transport_trust=TransportTrustClass.PROCESS_LOCAL,
            attestation=None,
            error_code="SESSION_MISSING"
        )

    if not is_canonical_email(account):
        now_t = time.time()
        return RefreshEvidence(
            canonical_account=account,
            canonical_executable_path="none",
            binary_sha256="none",
            source_revision_inspected=INSPECTED_AGM_SOURCE_REVISION,
            argv=[],
            started_at_epoch=now_t,
            completed_at_epoch=now_t,
            exit_code=1,
            result=RefreshResult.INVALID_ACCOUNT,
            supervisor_session_id=supervisor_session_id,
            source_origin=EvidenceSourceOrigin.DRY_RUN,
            transport_trust=TransportTrustClass.PROCESS_LOCAL,
            attestation=None,
            error_code="INVALID_ACCOUNT"
        )

    if not live_network:
        now_t = time.time()
        agm_bin = find_canonical_agm_executable() or "agm.exe"
        return RefreshEvidence(
            canonical_account=account,
            canonical_executable_path=agm_bin,
            binary_sha256="DRY_RUN_SHA256",
            source_revision_inspected=INSPECTED_AGM_SOURCE_REVISION,
            argv=[agm_bin, "refresh", account],
            started_at_epoch=now_t,
            completed_at_epoch=now_t + 0.01,
            exit_code=0,
            result=RefreshResult.DRY_RUN,
            supervisor_session_id=supervisor_session_id,
            source_origin=EvidenceSourceOrigin.DRY_RUN,
            transport_trust=TransportTrustClass.PROCESS_LOCAL,
            attestation=None,
            error_code=None
        )

    return _execute_live_refresh_sealed(account, supervisor_session_id, trusted_identity=trusted_identity, timeout_sec=timeout_sec)


def main():
    parser = argparse.ArgumentParser(description="Safely execute AGM quota refresh with typed provenance.")
    parser.add_argument("account", help="Canonical email of account to refresh")
    parser.add_argument("--session-id", required=True, help="Mandatory supervisor session ID")
    parser.add_argument("--expected-binary-sha256", help="Mandatory expected AGM binary SHA-256 for live mode")
    parser.add_argument("--live", action="store_true", help="Execute live network refresh against AGM")
    parser.add_argument("--private-diagnostic-mode", action="store_true", help="Include raw email and internal provenance")
    args = parser.parse_args()

    trusted_id = None
    if args.expected_binary_sha256:
        trusted_id = TrustedAgmIdentity(expected_binary_sha256=args.expected_binary_sha256)

    evidence = execute_safe_refresh(
        args.account,
        args.session_id,
        trusted_identity=trusted_id,
        live_network=args.live
    )
    if args.private_diagnostic_mode:
        print(json.dumps(evidence.to_private_diagnostic_dict(), indent=2))
    else:
        print(json.dumps(asdict(evidence.to_sanitized_dto()), indent=2))

    if evidence.result == RefreshResult.REFRESH_SUCCEEDED:
        sys.exit(0)
    elif evidence.result == RefreshResult.DRY_RUN:
        sys.exit(3)
    elif evidence.result in (
        RefreshResult.BINARY_IDENTITY_UNCONFIGURED,
        RefreshResult.BINARY_IDENTITY_CONFIG_INVALID,
        RefreshResult.BINARY_IDENTITY_MISMATCH
    ):
        sys.exit(4)  # Explicit trust failure exit code
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
