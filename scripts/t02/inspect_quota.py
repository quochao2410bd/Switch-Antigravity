#!/usr/bin/env python3
"""
inspect_quota.py

Safe read-only parser and normalizer for AGM quota output (Round 7 Architecture).

Key Architectural Trust Improvements:
1. Structural TrustedAgmIdentity Requirement (Item 5):
   - Supervisor-facing functions strictly accept `trusted_identity: Optional[TrustedAgmIdentity]`.
   - Raw expected_binary_sha256 strings across callers are removed in favor of typed configuration.
2. Sanitized Warning Codes (Critical Item 7):
   - `SanitizedAccountQuotaDTO.sanitized_warnings` exposes ONLY normalized codes (e.g. `ACCOUNT_MISMATCH`,
     `BINARY_IDENTITY_UNCONFIGURED`, `BINARY_IDENTITY_MISMATCH`, `EVIDENCE_EXPIRED`).
   - Free-text warning strings containing emails or exceptions are strictly isolated to private diagnostics.
3. Trusted CLI Execution (Critical Item 4):
   - Live `agm list` execution fallback uses `execute_trusted_agm()` through TrustedAgmRunner.
   - Missing or unconfigured binary identity fails closed (`BINARY_IDENTITY_UNCONFIGURED`) without running PATH binary.
4. Process-Local TCB Model (Items 3 & 4):
   - Supervisor process is the trusted TCB; attestation protects against accidental module misuse.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from refresh_quota_safe import (
    INSPECTED_AGM_SOURCE_REVISION,
    EvidenceSourceOrigin,
    LiveExecutionAttestation,
    RefreshEvidence,
    RefreshResult,
    TransportTrustClass,
    is_canonical_email,
    pseudonymize_account,
    verify_evidence_signature,
    verify_live_execution_attestation,
)
from trusted_agm_runner import (
    RunnerErrorCode,
    TrustedAgmIdentity,
    execute_trusted_agm,
)


class FreshnessState(str, Enum):
    PROVEN_FRESH = "PROVEN_FRESH"
    STALE_CACHED = "STALE_CACHED"
    REFRESH_FAILED = "REFRESH_FAILED"
    UNKNOWN_UNFETCHED = "UNKNOWN_UNFETCHED"


class FormatSupportState(str, Enum):
    FORMAT_SUPPORTED = "FORMAT_SUPPORTED"
    FORMAT_UNSUPPORTED = "FORMAT_UNSUPPORTED"


class WarningCode(str, Enum):
    NO_EVIDENCE_PROVIDED = "NO_EVIDENCE_PROVIDED"
    UNTRUSTED_DESERIALIZED_EVIDENCE = "UNTRUSTED_DESERIALIZED_EVIDENCE"
    SYNTHETIC_TEST_REJECTED = "SYNTHETIC_TEST_REJECTED"
    DRY_RUN_EVIDENCE = "DRY_RUN_EVIDENCE"
    MISSING_ATTESTATION = "MISSING_ATTESTATION"
    ACCOUNT_NON_CANONICAL = "ACCOUNT_NON_CANONICAL"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    ARGV_MISMATCH = "ARGV_MISMATCH"
    BINARY_IDENTITY_UNCONFIGURED = "BINARY_IDENTITY_UNCONFIGURED"
    BINARY_IDENTITY_CONFIG_INVALID = "BINARY_IDENTITY_CONFIG_INVALID"
    BINARY_IDENTITY_MISMATCH = "BINARY_IDENTITY_MISMATCH"
    BINARY_IDENTITY_UNVERIFIED = "BINARY_IDENTITY_UNVERIFIED"
    SOURCE_REVISION_MISMATCH = "SOURCE_REVISION_MISMATCH"
    SESSION_MISMATCH = "SESSION_MISMATCH"
    SESSION_UNCONFIGURED = "SESSION_UNCONFIGURED"
    REFRESH_NON_ZERO_EXIT = "REFRESH_NON_ZERO_EXIT"
    CONTRADICTORY_EVIDENCE = "CONTRADICTORY_EVIDENCE"
    NON_MONOTONIC_TIMESTAMPS = "NON_MONOTONIC_TIMESTAMPS"
    INSANE_DURATION = "INSANE_DURATION"
    FUTURE_CLOCK_SKEW = "FUTURE_CLOCK_SKEW"
    EVIDENCE_EXPIRED = "EVIDENCE_EXPIRED"
    RAW_UNVALIDATED_TIMESTAMP = "RAW_UNVALIDATED_TIMESTAMP"
    FORMAT_UNSUPPORTED = "FORMAT_UNSUPPORTED"
    MALFORMED_SCORE = "MALFORMED_SCORE"
    RESEARCH_FALLBACK_PARSED = "RESEARCH_FALLBACK_PARSED"


@dataclass
class ModelQuotaDetail:
    model_name: str
    provider: str
    remaining_pct: Optional[int]
    reset_time: Optional[str]
    freshness_state: FreshnessState


@dataclass
class SanitizedAccountQuotaDTO:
    """Safe supervisor DTO exposing ONLY pseudonymous account_ref and normalized warning codes (Critical Item 7)."""
    account_ref: str
    status_tags: List[str]
    is_active_cli: bool
    is_active_ide: bool
    is_token_expired: bool
    gemini_pro_pct: Optional[int]
    gemini_flash_pct: Optional[int]
    claude_pct: Optional[int]
    parsed_at_epoch: float
    refresh_confirmed_at_epoch: Optional[float]
    quota_reset_time: Optional[str]
    freshness_state: str
    format_support: str
    source: str
    sanitized_warnings: List[str]  # Normalized codes ONLY! No raw emails or exception strings!
    eligible: bool


@dataclass
class AccountQuotaSummary:
    canonical_account: str
    account_ref: str
    status_tags: List[str]
    is_active_cli: bool
    is_active_ide: bool
    is_token_expired: bool
    gemini_pro_pct: Optional[int]
    gemini_flash_pct: Optional[int]
    claude_pct: Optional[int]
    models: Dict[str, ModelQuotaDetail]
    parsed_at_epoch: float
    refresh_confirmed_at_epoch: Optional[float]
    quota_reset_time: Optional[str]
    freshness_state: FreshnessState
    format_support: FormatSupportState
    source: str
    warning_codes: List[WarningCode]
    parse_warnings_private: List[str]  # Diagnostic only
    eligible: bool

    def to_sanitized_dto(self) -> SanitizedAccountQuotaDTO:
        """Converts to safe supervisor DTO containing zero raw emails."""
        return SanitizedAccountQuotaDTO(
            account_ref=self.account_ref,
            status_tags=self.status_tags,
            is_active_cli=self.is_active_cli,
            is_active_ide=self.is_active_ide,
            is_token_expired=self.is_token_expired,
            gemini_pro_pct=self.gemini_pro_pct,
            gemini_flash_pct=self.gemini_flash_pct,
            claude_pct=self.claude_pct,
            parsed_at_epoch=self.parsed_at_epoch,
            refresh_confirmed_at_epoch=self.refresh_confirmed_at_epoch,
            quota_reset_time=self.quota_reset_time,
            freshness_state=self.freshness_state.value,
            format_support=self.format_support.value,
            source=self.source,
            sanitized_warnings=[c.value for c in self.warning_codes],
            eligible=self.eligible
        )


def parse_percentage_field(val: str) -> Optional[int]:
    """Parse percentage string safely. Returns None on '-', 'NaN', or invalid."""
    if not val:
        return None
    val = val.strip()
    if val in ("-", "unknown", "N/A", "null", "none", "???"):
        return None
    m = re.match(r"^(\d{1,3})%$", val)
    if m:
        try:
            num = int(m.group(1))
            if 0 <= num <= 100:
                return num
        except ValueError:
            return None
    return None


def deserialize_evidence_payload(
    payload: dict,
    session_secret: Optional[str] = None
) -> Tuple[Optional[RefreshEvidence], List[WarningCode], List[str]]:
    """
    Safely deserializes a JSON/dict payload.
    All deserialized payloads are initialized as UNTRUSTED_DESERIALIZED regardless of fields.
    """
    codes: List[WarningCode] = []
    diag: List[str] = []
    try:
        res_val = payload.get("result")
        res_enum = RefreshResult(res_val) if isinstance(res_val, str) else res_val
        orig_val = payload.get("source_origin") or payload.get("origin")
        orig_enum = EvidenceSourceOrigin(orig_val) if isinstance(orig_val, str) else orig_val

        evidence = RefreshEvidence(
            canonical_account=payload.get("canonical_account", ""),
            canonical_executable_path=payload.get("canonical_executable_path", payload.get("agm_executable", "")),
            binary_sha256=payload.get("binary_sha256", "UNKNOWN_SHA256"),
            source_revision_inspected=payload.get("source_revision_inspected", INSPECTED_AGM_SOURCE_REVISION),
            argv=payload.get("argv", []),
            started_at_epoch=float(payload.get("started_at_epoch", 0.0)),
            completed_at_epoch=float(payload.get("completed_at_epoch", 0.0)),
            exit_code=int(payload.get("exit_code", 1)),
            result=res_enum,
            supervisor_session_id=payload.get("supervisor_session_id", ""),
            source_origin=orig_enum,
            transport_trust=TransportTrustClass.UNTRUSTED_DESERIALIZED,  # Forced untrusted!
            attestation=None,
            hmac_signature=payload.get("hmac_signature"),
            error_code=payload.get("error_code")
        )

        if session_secret and evidence.hmac_signature:
            if verify_evidence_signature(evidence, session_secret):
                evidence.transport_trust = TransportTrustClass.SIGNED_DESERIALIZED
            else:
                codes.append(WarningCode.UNTRUSTED_DESERIALIZED_EVIDENCE)
                diag.append("HMAC signature verification failed for serialized evidence")
        return evidence, codes, diag
    except Exception as e:
        codes.append(WarningCode.UNTRUSTED_DESERIALIZED_EVIDENCE)
        diag.append(f"Malformed serialized evidence dict: {e}")
        return None, codes, diag


def _validate_refresh_evidence_internal(
    evidence: Optional[Union[RefreshEvidence, dict]],
    canonical_account: str,
    now_epoch: float,
    expected_session_id: str,
    trusted_identity: Optional[TrustedAgmIdentity] = None,
    max_freshness_age_sec: float = 300.0,
    allowed_clock_skew_sec: float = 2.0,
    session_secret: Optional[str] = None,
    allow_synthetic_test: bool = False
) -> Tuple[FreshnessState, Optional[float], List[WarningCode], List[str]]:
    codes: List[WarningCode] = []
    diag: List[str] = []

    if evidence is None:
        return FreshnessState.STALE_CACHED, None, [WarningCode.NO_EVIDENCE_PROVIDED], ["No refresh evidence provided; STALE_CACHED"]

    if isinstance(evidence, dict):
        ev_obj, deser_codes, deser_diag = deserialize_evidence_payload(evidence, session_secret=session_secret)
        codes.extend(deser_codes)
        diag.extend(deser_diag)
        if ev_obj is None:
            return FreshnessState.STALE_CACHED, None, codes, diag
        evidence = ev_obj

    # 1. Transport Trust Check
    if evidence.transport_trust == TransportTrustClass.UNTRUSTED_DESERIALIZED:
        return FreshnessState.STALE_CACHED, None, [WarningCode.UNTRUSTED_DESERIALIZED_EVIDENCE], [
            "Untrusted deserialized evidence without verified HMAC signature rejected"
        ]

    # 2. Source Origin & Attestation Check
    if evidence.source_origin == EvidenceSourceOrigin.SYNTHETIC_TEST_EVIDENCE:
        if not allow_synthetic_test:
            return FreshnessState.STALE_CACHED, None, [WarningCode.SYNTHETIC_TEST_REJECTED], [
                "Synthetic test evidence is strictly forbidden from establishing production PROVEN_FRESH status"
            ]
    elif evidence.source_origin == EvidenceSourceOrigin.DRY_RUN:
        return FreshnessState.STALE_CACHED, None, [WarningCode.DRY_RUN_EVIDENCE], ["Dry-run evidence cannot establish freshness"]
    elif evidence.source_origin == EvidenceSourceOrigin.LIVE_REFRESH_EXECUTION:
        # Process-Local Attestation Verification (TCB misuse guard)
        if evidence.transport_trust == TransportTrustClass.PROCESS_LOCAL:
            if not verify_live_execution_attestation(evidence.attestation, canonical_account, evidence.binary_sha256):
                return FreshnessState.STALE_CACHED, None, [WarningCode.MISSING_ATTESTATION], [
                    "Process-local live evidence lacks valid sealed executor attestation capability"
                ]
    else:
        return FreshnessState.STALE_CACHED, None, [WarningCode.UNTRUSTED_DESERIALIZED_EVIDENCE], ["Unknown evidence origin"]

    # 3. Canonical Account Exact Match
    if not is_canonical_email(evidence.canonical_account):
        return FreshnessState.STALE_CACHED, None, [WarningCode.ACCOUNT_NON_CANONICAL], [f"Refresh evidence account is non-canonical"]
    if evidence.canonical_account.lower() != canonical_account.lower():
        return FreshnessState.STALE_CACHED, None, [WarningCode.ACCOUNT_MISMATCH], [
            f"Refresh evidence account mismatch (expected '{canonical_account}', got '{evidence.canonical_account}')"
        ]

    # 4. Exact Argv Equality Check
    expected_argv = [evidence.canonical_executable_path, "refresh", evidence.canonical_account]
    if (
        len(evidence.argv) != 3
        or evidence.argv[0] != evidence.canonical_executable_path
        or evidence.argv[1] != "refresh"
        or evidence.argv[2].lower() != canonical_account.lower()
    ):
        return FreshnessState.STALE_CACHED, None, [WarningCode.ARGV_MISMATCH], [
            f"Exact argv mismatch (expected {expected_argv}, got {evidence.argv})"
        ]

    # 5. Mandatory Independent Expected Binary Identity Binding (Critical Items 1 & 5)
    if not trusted_identity or not trusted_identity.expected_binary_sha256 or not trusted_identity.expected_binary_sha256.strip():
        return FreshnessState.STALE_CACHED, None, [WarningCode.BINARY_IDENTITY_UNCONFIGURED], [
            "BINARY_IDENTITY_UNCONFIGURED: Missing mandatory expected AGM binary SHA-256 configuration"
        ]

    expected_sha_clean = trusted_identity.expected_binary_sha256.strip().lower()
    if not re.match(r"^[0-9a-f]{64}$", expected_sha_clean):
        return FreshnessState.STALE_CACHED, None, [WarningCode.BINARY_IDENTITY_CONFIG_INVALID], [
            f"BINARY_IDENTITY_CONFIG_INVALID: Expected AGM binary SHA-256 is not a valid 64-hex string"
        ]

    if not evidence.canonical_executable_path or evidence.canonical_executable_path == "none":
        return FreshnessState.STALE_CACHED, None, [WarningCode.BINARY_IDENTITY_UNVERIFIED], ["Refresh evidence lacks valid executable path"]
    if not evidence.binary_sha256 or evidence.binary_sha256 == "UNKNOWN_SHA256":
        return FreshnessState.STALE_CACHED, None, [WarningCode.BINARY_IDENTITY_UNVERIFIED], ["AGM binary SHA-256 identity is unverified"]
    if evidence.source_revision_inspected != INSPECTED_AGM_SOURCE_REVISION:
        return FreshnessState.STALE_CACHED, None, [WarningCode.SOURCE_REVISION_MISMATCH], [
            f"AGM source revision mismatch (expected '{INSPECTED_AGM_SOURCE_REVISION}', got '{evidence.source_revision_inspected}')"
        ]
    if evidence.binary_sha256.lower() != expected_sha_clean:
        return FreshnessState.STALE_CACHED, None, [WarningCode.BINARY_IDENTITY_MISMATCH], [
            f"BINARY_IDENTITY_MISMATCH: Observed binary SHA-256 does not match expected identity"
        ]

    # 6. Mandatory Session ID Check
    if not expected_session_id or not expected_session_id.strip():
        return FreshnessState.STALE_CACHED, None, [WarningCode.SESSION_UNCONFIGURED], ["Mandatory supervisor expected_session_id missing"]
    if evidence.supervisor_session_id != expected_session_id:
        return FreshnessState.STALE_CACHED, None, [WarningCode.SESSION_MISMATCH], [
            f"Session ID mismatch (expected '{expected_session_id}', got '{evidence.supervisor_session_id}')"
        ]

    # 7. Result & Exit Code Consistency
    if evidence.result != RefreshResult.REFRESH_SUCCEEDED:
        return FreshnessState.REFRESH_FAILED, None, [WarningCode.REFRESH_NON_ZERO_EXIT], [
            f"Refresh failed with status {evidence.result.value}"
        ]
    if evidence.exit_code != 0:
        return FreshnessState.REFRESH_FAILED, None, [WarningCode.CONTRADICTORY_EVIDENCE], [
            f"Contradictory evidence: status is REFRESH_SUCCEEDED but exit_code is {evidence.exit_code}"
        ]

    # 8. Monotonic Timestamps & Sane Duration
    if evidence.started_at_epoch > evidence.completed_at_epoch:
        return FreshnessState.STALE_CACHED, None, [WarningCode.NON_MONOTONIC_TIMESTAMPS], [
            f"Invalid timestamp monotonicity: started > completed"
        ]
    duration = evidence.completed_at_epoch - evidence.started_at_epoch
    if duration > 60.0 or duration < 0.0:
        return FreshnessState.STALE_CACHED, None, [WarningCode.INSANE_DURATION], [
            f"Insane execution duration: {duration:.2f}s"
        ]

    # 9. Clock Skew Ceiling & Freshness Expiration
    future_skew = evidence.completed_at_epoch - now_epoch
    if future_skew > allowed_clock_skew_sec:
        return FreshnessState.STALE_CACHED, None, [WarningCode.FUTURE_CLOCK_SKEW], [
            f"Refresh completed_at is in the future by {future_skew:.2f}s"
        ]
    age = now_epoch - evidence.completed_at_epoch
    if age > max_freshness_age_sec:
        return FreshnessState.STALE_CACHED, evidence.completed_at_epoch, [WarningCode.EVIDENCE_EXPIRED], [
            f"Refresh evidence expired ({age:.1f}s > max {max_freshness_age_sec:.1f}s)"
        ]

    return FreshnessState.PROVEN_FRESH, evidence.completed_at_epoch, codes, diag


def validate_refresh_evidence_supervisor(
    evidence: Optional[Union[RefreshEvidence, dict]],
    canonical_account: str,
    now_epoch: float,
    expected_session_id: str,
    trusted_identity: Optional[TrustedAgmIdentity] = None,
    max_freshness_age_sec: float = 300.0,
    allowed_clock_skew_sec: float = 2.0,
    session_secret: Optional[str] = None
) -> Tuple[FreshnessState, Optional[float], List[WarningCode], List[str]]:
    """
    Production supervisor validation entry point (Item 5).
    Requires structured TrustedAgmIdentity. NO TEST-WEAKENING FLAGS EXPOSED.
    """
    return _validate_refresh_evidence_internal(
        evidence=evidence,
        canonical_account=canonical_account,
        now_epoch=now_epoch,
        expected_session_id=expected_session_id,
        trusted_identity=trusted_identity,
        max_freshness_age_sec=max_freshness_age_sec,
        allowed_clock_skew_sec=allowed_clock_skew_sec,
        session_secret=session_secret,
        allow_synthetic_test=False
    )


def _validate_refresh_evidence_for_test(
    evidence: Optional[Union[RefreshEvidence, dict]],
    canonical_account: str,
    now_epoch: float,
    expected_session_id: str,
    trusted_identity: Optional[TrustedAgmIdentity] = None,
    max_freshness_age_sec: float = 300.0,
    allowed_clock_skew_sec: float = 2.0,
    session_secret: Optional[str] = None
) -> Tuple[FreshnessState, Optional[float], List[WarningCode], List[str]]:
    """Test-only validation harness."""
    return _validate_refresh_evidence_internal(
        evidence=evidence,
        canonical_account=canonical_account,
        now_epoch=now_epoch,
        expected_session_id=expected_session_id,
        trusted_identity=trusted_identity,
        max_freshness_age_sec=max_freshness_age_sec,
        allowed_clock_skew_sec=allowed_clock_skew_sec,
        session_secret=session_secret,
        allow_synthetic_test=True
    )


def parse_agm_list(
    text: str,
    refresh_evidence_map: Optional[Dict[str, Union[RefreshEvidence, dict]]] = None,
    raw_unvalidated_timestamps: Optional[Dict[str, float]] = None,
    max_freshness_age_sec: float = 300.0,
    source_label: str = "AGM_CLI_LIST",
    supervisor_session_id: Optional[str] = None,
    trusted_identity: Optional[TrustedAgmIdentity] = None,
    session_secret: Optional[str] = None,
    lenient_parser: bool = False,
    now_epoch: Optional[float] = None,
    _test_mode_allow_synthetic: bool = False
) -> List[AccountQuotaSummary]:
    """
    Parses `agm list` output with strict table header validation and trusted binary identity binding (Item 5).
    """
    now = now_epoch if now_epoch is not None else time.time()
    ev_map = refresh_evidence_map or {}

    lines = text.strip().splitlines()
    if not lines or any("No accounts yet" in line for line in lines):
        return []

    results: List[AccountQuotaSummary] = []
    header_found = False
    col_bounds = None
    format_state = FormatSupportState.FORMAT_UNSUPPORTED

    for line in lines:
        if "EMAIL" in line and "STATUS" in line and "GEM-PRO" in line and "GEM-FLASH" in line and "CLAUDE" in line:
            email_idx = line.find("EMAIL")
            status_idx = line.find("STATUS")
            gp_idx = line.find("GEM-PRO")
            gf_idx = line.find("GEM-FLASH")
            cl_idx = line.find("CLAUDE")
            if email_idx < status_idx < gp_idx < gf_idx < cl_idx:
                col_bounds = (email_idx, status_idx, gp_idx, gf_idx, cl_idx)
                header_found = True
                format_state = FormatSupportState.FORMAT_SUPPORTED
            break

    if (not header_found or format_state == FormatSupportState.FORMAT_UNSUPPORTED) and not lenient_parser:
        return [
            AccountQuotaSummary(
                canonical_account="unknown@unsupported.schema",
                account_ref="acc_unsupported_schema",
                status_tags=[],
                is_active_cli=False,
                is_active_ide=False,
                is_token_expired=False,
                gemini_pro_pct=None,
                gemini_flash_pct=None,
                claude_pct=None,
                models={},
                parsed_at_epoch=now,
                refresh_confirmed_at_epoch=None,
                quota_reset_time=None,
                freshness_state=FreshnessState.UNKNOWN_UNFETCHED,
                format_support=FormatSupportState.FORMAT_UNSUPPORTED,
                source=source_label,
                warning_codes=[WarningCode.FORMAT_UNSUPPORTED],
                parse_warnings_private=["AGM output header does not match expected supported schema. Fail closed."],
                eligible=False
            )
        ]

    for line in lines:
        line_clean = line.strip()
        if not line_clean or ("EMAIL" in line and "STATUS" in line) or line_clean.startswith("---") or line_clean.startswith("==="):
            continue

        codes: List[WarningCode] = []
        diag: List[str] = []

        if col_bounds:
            e_idx, s_idx, gp_idx, gf_idx, cl_idx = col_bounds
            email_part = line[e_idx:s_idx].strip() if len(line) > s_idx else line.strip()
            status_part = line[s_idx:gp_idx].strip() if len(line) > gp_idx else ""
            gp_part = line[gp_idx:gf_idx].strip() if len(line) > gf_idx else ""
            gf_part = line[gf_idx:cl_idx].strip() if len(line) > cl_idx else ""
            cl_part = line[cl_idx:].strip() if len(line) > cl_idx else ""
        elif lenient_parser:
            tokens = line_clean.split()
            if len(tokens) < 1 or "@" not in tokens[0]:
                continue
            email_part = tokens[0]
            status_part = tokens[1] if len(tokens) > 1 else ""
            gp_part = tokens[2] if len(tokens) > 2 else ""
            gf_part = tokens[3] if len(tokens) > 3 else ""
            cl_part = tokens[4] if len(tokens) > 4 else ""
            codes.append(WarningCode.RESEARCH_FALLBACK_PARSED)
            diag.append("Parsed using research-lenient fallback tokenization")
        else:
            continue

        if "@" not in email_part:
            continue

        tags = [t.strip() for t in status_part.split(",") if t.strip()]
        is_expired = "token-exp" in tags

        gp_val = parse_percentage_field(gp_part)
        gf_val = parse_percentage_field(gf_part)
        cl_val = parse_percentage_field(cl_part)

        if gp_part and gp_part != "-" and gp_val is None:
            codes.append(WarningCode.MALFORMED_SCORE)
            diag.append(f"Malformed GEM-PRO quota string: '{gp_part}'")
        if gf_part and gf_part != "-" and gf_val is None:
            codes.append(WarningCode.MALFORMED_SCORE)
            diag.append(f"Malformed GEM-FLASH quota string: '{gf_part}'")
        if cl_part and cl_part != "-" and cl_val is None:
            codes.append(WarningCode.MALFORMED_SCORE)
            diag.append(f"Malformed CLAUDE quota string: '{cl_part}'")

        if raw_unvalidated_timestamps and email_part in raw_unvalidated_timestamps:
            codes.append(WarningCode.RAW_UNVALIDATED_TIMESTAMP)
            diag.append("Raw unvalidated timestamp provided without RefreshEvidence; rejected as STALE_CACHED")
            freshness = FreshnessState.STALE_CACHED
            ref_confirmed_at = None
        elif email_part in ev_map:
            val_func = _validate_refresh_evidence_for_test if _test_mode_allow_synthetic else validate_refresh_evidence_supervisor
            freshness, ref_confirmed_at, ev_codes, ev_diag = val_func(
                ev_map[email_part],
                canonical_account=email_part,
                now_epoch=now,
                expected_session_id=supervisor_session_id or "",
                trusted_identity=trusted_identity,
                max_freshness_age_sec=max_freshness_age_sec,
                session_secret=session_secret
            )
            codes.extend(ev_codes)
            diag.extend(ev_diag)
        elif all(v is None for v in [gp_val, gf_val, cl_val]):
            freshness = FreshnessState.UNKNOWN_UNFETCHED
            ref_confirmed_at = None
        else:
            freshness = FreshnessState.STALE_CACHED
            ref_confirmed_at = None

        all_zero = (
            any(v == 0 for v in [gp_val, gf_val, cl_val] if v is not None)
            and all(v == 0 for v in [gp_val, gf_val, cl_val] if v is not None)
        )
        eligible = (
            (not is_expired)
            and (not all_zero)
            and (freshness == FreshnessState.PROVEN_FRESH)
            and (format_state == FormatSupportState.FORMAT_SUPPORTED)
        )

        results.append(AccountQuotaSummary(
            canonical_account=email_part,
            account_ref=pseudonymize_account(email_part),
            status_tags=tags,
            is_active_cli="cli" in tags,
            is_active_ide="ide" in tags,
            is_token_expired=is_expired,
            gemini_pro_pct=gp_val,
            gemini_flash_pct=gf_val,
            claude_pct=cl_val,
            models={},
            parsed_at_epoch=now,
            refresh_confirmed_at_epoch=ref_confirmed_at,
            quota_reset_time=None,
            freshness_state=freshness,
            format_support=format_state,
            source=source_label,
            warning_codes=codes,
            parse_warnings_private=diag,
            eligible=eligible
        ))

    return results


def parse_agm_info(
    text: str,
    refresh_evidence: Optional[Union[RefreshEvidence, dict]] = None,
    raw_unvalidated_timestamp: Optional[float] = None,
    max_freshness_age_sec: float = 300.0,
    source_label: str = "AGM_CLI_INFO",
    supervisor_session_id: Optional[str] = None,
    trusted_identity: Optional[TrustedAgmIdentity] = None,
    session_secret: Optional[str] = None,
    now_epoch: Optional[float] = None,
    _test_mode_allow_synthetic: bool = False
) -> Optional[AccountQuotaSummary]:
    """
    Parses `agm info <email>` output with strict table header verification and trusted identity (Item 5).
    """
    now = now_epoch if now_epoch is not None else time.time()
    lines = text.strip().splitlines()
    if not lines:
        return None

    email = ""
    is_expired = False
    models: Dict[str, ModelQuotaDetail] = {}
    codes: List[WarningCode] = []
    diag: List[str] = []
    in_table = False
    header_found = False
    earliest_reset = None

    for line in lines:
        line_clean = line.strip()
        if line_clean.startswith("Account:"):
            email = line_clean.split("Account:", 1)[1].strip()
        elif line_clean.startswith("Token expiry:"):
            if "(expired)" in line_clean:
                is_expired = True
        elif "No quota data" in line_clean:
            diag.append("No quota data recorded in store")
        elif "PROVIDER" in line:
            tokens = line_clean.split()
            if tokens == ["PROVIDER", "MODEL", "SCORE", "RESET"]:
                header_found = True
                in_table = True
            else:
                codes.append(WarningCode.FORMAT_UNSUPPORTED)
                diag.append(f"Info table header deviation: expected ['PROVIDER', 'MODEL', 'SCORE', 'RESET'], got {tokens}")
            continue
        elif line_clean.startswith("---") or line_clean.startswith("==="):
            continue
        elif in_table and line_clean:
            tokens = line_clean.split()
            if len(tokens) >= 4:
                provider = tokens[0]
                model = tokens[1]
                score_str = tokens[2]
                reset_str = tokens[3]
                if reset_str and not earliest_reset:
                    earliest_reset = reset_str
                pct = parse_percentage_field(score_str)
                if pct is None and score_str != "-":
                    codes.append(WarningCode.MALFORMED_SCORE)
                    diag.append(f"Malformed score for model {model}: '{score_str}'")

                models[model] = ModelQuotaDetail(
                    model_name=model,
                    provider=provider,
                    remaining_pct=pct,
                    reset_time=reset_str,
                    freshness_state=FreshnessState.STALE_CACHED
                )
            else:
                codes.append(WarningCode.FORMAT_UNSUPPORTED)
                diag.append(f"Info row column count mismatch: '{line_clean}'")

    if not email:
        return None

    format_state = FormatSupportState.FORMAT_SUPPORTED if header_found else FormatSupportState.FORMAT_UNSUPPORTED
    if not header_found:
        codes.append(WarningCode.FORMAT_UNSUPPORTED)
        diag.append("AGM info output missing or deviated from expected table header schema; fail closed")

    if raw_unvalidated_timestamp is not None:
        codes.append(WarningCode.RAW_UNVALIDATED_TIMESTAMP)
        diag.append("Raw unvalidated timestamp provided without RefreshEvidence; rejected as STALE_CACHED")
        freshness = FreshnessState.STALE_CACHED
        ref_confirmed_at = None
    elif refresh_evidence is not None:
        val_func = _validate_refresh_evidence_for_test if _test_mode_allow_synthetic else validate_refresh_evidence_supervisor
        freshness, ref_confirmed_at, ev_codes, ev_diag = val_func(
            refresh_evidence,
            canonical_account=email,
            now_epoch=now,
            expected_session_id=supervisor_session_id or "",
            trusted_identity=trusted_identity,
            max_freshness_age_sec=max_freshness_age_sec,
            session_secret=session_secret
        )
        codes.extend(ev_codes)
        diag.extend(ev_diag)
    elif len(models) == 0:
        freshness = FreshnessState.UNKNOWN_UNFETCHED
        ref_confirmed_at = None
    else:
        freshness = FreshnessState.STALE_CACHED
        ref_confirmed_at = None

    for m in models.values():
        m.freshness_state = freshness

    gp_pcts = [m.remaining_pct for name, m in models.items() if "gemini" in name.lower() and "pro" in name.lower() and m.remaining_pct is not None]
    gf_pcts = [m.remaining_pct for name, m in models.items() if "gemini" in name.lower() and "flash" in name.lower() and m.remaining_pct is not None]
    cl_pcts = [m.remaining_pct for name, m in models.items() if "claude" in name.lower() and m.remaining_pct is not None]

    gp_val = min(gp_pcts) if gp_pcts else None
    gf_val = min(gf_pcts) if gf_pcts else None
    cl_val = min(cl_pcts) if cl_pcts else None

    eligible = (
        (not is_expired)
        and (freshness == FreshnessState.PROVEN_FRESH)
        and (format_state == FormatSupportState.FORMAT_SUPPORTED)
        and any(m.remaining_pct is not None and m.remaining_pct > 0 for m in models.values())
    )

    return AccountQuotaSummary(
        canonical_account=email,
        account_ref=pseudonymize_account(email),
        status_tags=["token-exp"] if is_expired else [],
        is_active_cli=False,
        is_active_ide=False,
        is_token_expired=is_expired,
        gemini_pro_pct=gp_val,
        gemini_flash_pct=gf_val,
        claude_pct=cl_val,
        models=models,
        parsed_at_epoch=now,
        refresh_confirmed_at_epoch=ref_confirmed_at,
        quota_reset_time=earliest_reset,
        freshness_state=freshness,
        format_support=format_state,
        source=source_label,
        warning_codes=codes,
        parse_warnings_private=diag,
        eligible=eligible
    )


def main():
    parser = argparse.ArgumentParser(description="Inspect and normalize AGM quota output.")
    parser.add_argument("--file", "-f", help="Read raw AGM output from file")
    parser.add_argument("--mode", "-m", choices=["list", "info", "auto"], default="auto", help="Parsing mode")
    parser.add_argument("--session-id", help="Mandatory supervisor session ID for production validation")
    parser.add_argument("--expected-binary-sha256", help="Mandatory expected binary SHA-256 for strict binding")
    parser.add_argument("--provenance-json", help="Optional JSON dict of RefreshEvidence mapping email -> record")
    parser.add_argument("--private-diagnostic-mode", action="store_true", help="Expose raw emails in diagnostic output")
    parser.add_argument("--research-lenient-parser", action="store_true", help="Enable research fallback tokenization")
    args = parser.parse_args()

    session_secret = os.environ.get("AGM_SESSION_SECRET")
    ev_map = {}
    if args.provenance_json:
        try:
            ev_map = json.loads(args.provenance_json)
        except Exception as e:
            print(f"Error parsing provenance JSON: {e}", file=sys.stderr)
            sys.exit(1)

    trusted_id = None
    if args.expected_binary_sha256:
        trusted_id = TrustedAgmIdentity(expected_binary_sha256=args.expected_binary_sha256)

    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        if not sys.stdin.isatty():
            content = sys.stdin.read()
        else:
            # Critical Item 4: Use TrustedAgmRunner for CLI fallback; do NOT run bare PATH binary!
            exec_res = execute_trusted_agm(["list"], trusted_identity=trusted_id)
            if not exec_res.success:
                print(f"Error executing trusted agm list: {exec_res.error_code.value} - {exec_res.stderr}", file=sys.stderr)
                sys.exit(4 if exec_res.error_code in (
                    RunnerErrorCode.BINARY_IDENTITY_UNCONFIGURED,
                    RunnerErrorCode.BINARY_IDENTITY_CONFIG_INVALID,
                    RunnerErrorCode.BINARY_IDENTITY_MISMATCH
                ) else 1)
            content = exec_res.stdout

    mode = args.mode
    if mode == "auto":
        if "PROVIDER" in content and "MODEL" in content and "Account:" in content:
            mode = "info"
        else:
            mode = "list"

    if mode == "info":
        first_ev = next(iter(ev_map.values())) if ev_map else None
        res = parse_agm_info(
            content,
            refresh_evidence=first_ev,
            supervisor_session_id=args.session_id,
            trusted_identity=trusted_id,
            session_secret=session_secret
        )
        if res:
            data = asdict(res) if args.private_diagnostic_mode else asdict(res.to_sanitized_dto())
        else:
            data = None
    else:
        res_list = parse_agm_list(
            content,
            refresh_evidence_map=ev_map,
            supervisor_session_id=args.session_id,
            trusted_identity=trusted_id,
            session_secret=session_secret,
            lenient_parser=args.research_lenient_parser
        )
        if args.private_diagnostic_mode:
            data = [asdict(r) for r in res_list]
        else:
            data = [asdict(r.to_sanitized_dto()) for r in res_list]

    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
