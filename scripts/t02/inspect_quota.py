#!/usr/bin/env python3
"""
inspect_quota.py

Safe read-only parser and normalizer for AGM (Antigravity Manager) quota output.
Converts table output into a structured, typed AccountQuota schema
with strict RefreshEvidence trust invariant validation and fail-closed schema checks.

Trust Model:
1. Process-Local Trust & HMAC Session Signing.
2. Origin Invariant: Production freshness requires EvidenceTrustOrigin.LIVE_REFRESH_EXECUTION.
   Synthetic test evidence is strictly forbidden from supervisor production mode.
3. Strict Invariant Checking:
   - result == REFRESH_SUCCEEDED
   - exit_code == 0
   - canonical email RFC 5322 match
   - command exact binding
   - trusted AGM executable and supported AGM version
   - mandatory supervisor session ID match
   - start_t <= completed_t <= now + clock_skew (max 2.0s)
   - age <= max_freshness_age_sec (300.0s) and duration sane
4. Both List mode and Info mode fail closed on unexpected / corrupted table schemas.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union

# Add current dir to path to import refresh_quota_safe
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from refresh_quota_safe import (
    SUPPORTED_AGM_VERSIONS,
    EvidenceTrustOrigin,
    RefreshEvidence,
    RefreshResult,
    is_canonical_email,
    verify_evidence_signature,
)


class FreshnessState(str, Enum):
    PROVEN_FRESH = "PROVEN_FRESH"
    STALE_CACHED = "STALE_CACHED"
    REFRESH_FAILED = "REFRESH_FAILED"
    UNKNOWN_UNFETCHED = "UNKNOWN_UNFETCHED"


class FormatSupportState(str, Enum):
    FORMAT_SUPPORTED = "FORMAT_SUPPORTED"
    FORMAT_UNSUPPORTED = "FORMAT_UNSUPPORTED"


@dataclass
class ModelQuotaDetail:
    model_name: str
    provider: str
    remaining_pct: Optional[int]
    reset_time: Optional[str]
    freshness_state: FreshnessState


@dataclass
class AccountQuotaSummary:
    safe_account_ref: str
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
    parse_warnings: List[str]
    eligible: bool


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


def validate_refresh_evidence(
    evidence: Optional[Union[RefreshEvidence, dict]],
    canonical_account: str,
    now_epoch: float,
    max_freshness_age_sec: float = 300.0,
    allowed_clock_skew_sec: float = 2.0,
    expected_session_id: Optional[str] = None,
    session_secret: Optional[str] = None,
    allow_synthetic_test_origin: bool = False
) -> Tuple[FreshnessState, Optional[float], List[str]]:
    """
    Validates a RefreshEvidence record against all production trust invariants.
    Returns (FreshnessState, refresh_confirmed_at_epoch, warnings).
    """
    warnings: List[str] = []
    if evidence is None:
        return FreshnessState.STALE_CACHED, None, ["No refresh evidence provided; treating cached quota as STALE_CACHED"]

    # Convert dict to RefreshEvidence if needed (tagged as UNTRUSTED_DESERIALIZED unless verified)
    if isinstance(evidence, dict):
        try:
            res_val = evidence.get("result")
            res_enum = RefreshResult(res_val) if isinstance(res_val, str) else res_val
            orig_val = evidence.get("origin", EvidenceTrustOrigin.UNTRUSTED_DESERIALIZED.value)
            orig_enum = EvidenceTrustOrigin(orig_val) if isinstance(orig_val, str) else orig_val
            evidence = RefreshEvidence(
                canonical_account=evidence.get("canonical_account", ""),
                agm_executable=evidence.get("agm_executable", ""),
                agm_version_or_revision=evidence.get("agm_version_or_revision", "UNKNOWN_VERSION"),
                command=evidence.get("command", ""),
                started_at_epoch=float(evidence.get("started_at_epoch", 0.0)),
                completed_at_epoch=float(evidence.get("completed_at_epoch", 0.0)),
                exit_code=int(evidence.get("exit_code", 1)),
                result=res_enum,
                supervisor_session_id=evidence.get("supervisor_session_id", ""),
                origin=orig_enum,
                hmac_signature=evidence.get("hmac_signature"),
                error_summary=evidence.get("error_summary")
            )
        except Exception as e:
            return FreshnessState.STALE_CACHED, None, [f"Malformed RefreshEvidence record: {e}"]

    # INVARIANT 1: Origin Validation (Item 2)
    if evidence.origin == EvidenceTrustOrigin.SYNTHETIC_TEST_EVIDENCE:
        if not allow_synthetic_test_origin:
            return FreshnessState.STALE_CACHED, None, [
                "Synthetic test evidence is prohibited from establishing production PROVEN_FRESH status"
            ]
        warnings.append("Validated under synthetic test origin mode")
    elif evidence.origin == EvidenceTrustOrigin.DRY_RUN:
        return FreshnessState.STALE_CACHED, None, ["Dry-run evidence cannot establish freshness"]
    elif evidence.origin == EvidenceTrustOrigin.UNTRUSTED_DESERIALIZED:
        if session_secret and evidence.hmac_signature:
            if not verify_evidence_signature(evidence, session_secret):
                return FreshnessState.STALE_CACHED, None, ["HMAC signature verification failed for serialized evidence"]
        else:
            return FreshnessState.STALE_CACHED, None, [
                "Unsigned deserialized evidence is unverified across trust boundary; rejected in production mode"
            ]
    elif evidence.origin != EvidenceTrustOrigin.LIVE_REFRESH_EXECUTION:
        return FreshnessState.STALE_CACHED, None, [f"Unknown evidence origin: '{evidence.origin}'"]

    # INVARIANT 2: Account Exact Match (Item 3)
    if not is_canonical_email(evidence.canonical_account):
        return FreshnessState.STALE_CACHED, None, [f"Refresh evidence account '{evidence.canonical_account}' is non-canonical"]
    if evidence.canonical_account.lower() != canonical_account.lower():
        return FreshnessState.STALE_CACHED, None, [
            f"Refresh evidence account mismatch (expected '{canonical_account}', got '{evidence.canonical_account}')"
        ]

    # INVARIANT 3: Command Exact Binding (Item 3)
    expected_cmd = f"agm refresh {evidence.canonical_account}"
    if evidence.command.strip() != expected_cmd and not evidence.command.endswith(f"refresh {evidence.canonical_account}"):
        return FreshnessState.STALE_CACHED, None, [
            f"Refresh evidence command mismatch (expected '{expected_cmd}', got '{evidence.command}')"
        ]

    # INVARIANT 4: Trusted Executable & Known Version (Item 3 & 5)
    if not evidence.agm_executable or evidence.agm_executable == "none":
        return FreshnessState.STALE_CACHED, None, ["Refresh evidence lacks valid AGM executable path"]
    if evidence.agm_version_or_revision == "UNKNOWN_VERSION" or evidence.agm_version_or_revision not in SUPPORTED_AGM_VERSIONS:
        return FreshnessState.STALE_CACHED, None, [
            f"AGM version '{evidence.agm_version_or_revision}' is unverified or unsupported; fail closed"
        ]

    # INVARIANT 5: Session ID Exact Match (Item 3 & 4)
    if not expected_session_id or not expected_session_id.strip():
        return FreshnessState.STALE_CACHED, None, ["Mandatory supervisor expected_session_id was omitted in validation"]
    if evidence.supervisor_session_id != expected_session_id:
        return FreshnessState.STALE_CACHED, None, [
            f"Session ID mismatch (expected '{expected_session_id}', got '{evidence.supervisor_session_id}')"
        ]

    # INVARIANT 6: Result & Exit Code Consistency (Item 3)
    if evidence.result != RefreshResult.REFRESH_SUCCEEDED:
        return FreshnessState.REFRESH_FAILED, None, [
            f"Refresh failed with status {evidence.result.value}: {evidence.error_summary or 'non-zero exit'}"
        ]
    if evidence.exit_code != 0:
        return FreshnessState.REFRESH_FAILED, None, [
            f"Contradictory evidence: status is REFRESH_SUCCEEDED but exit_code is {evidence.exit_code}; fail closed"
        ]

    # INVARIANT 7: Timestamp Ordering & Monotonicity (Item 3)
    if evidence.started_at_epoch > evidence.completed_at_epoch:
        return FreshnessState.STALE_CACHED, None, [
            f"Invalid timestamp monotonicity: started ({evidence.started_at_epoch}) > completed ({evidence.completed_at_epoch})"
        ]

    # INVARIANT 8: Sane Duration (Item 3)
    duration = evidence.completed_at_epoch - evidence.started_at_epoch
    if duration > 60.0 or duration < 0.0:
        return FreshnessState.STALE_CACHED, None, [
            f"Insane execution duration: {duration:.2f}s (must be between 0.0s and 60.0s)"
        ]

    # INVARIANT 9: Clock Skew & Future Timestamp Rejection (Item 3)
    future_skew = evidence.completed_at_epoch - now_epoch
    if future_skew > allowed_clock_skew_sec:
        return FreshnessState.STALE_CACHED, None, [
            f"Refresh completed_at is in the future by {future_skew:.2f}s (> allowed skew {allowed_clock_skew_sec}s); rejected"
        ]

    # INVARIANT 10: Freshness Expiration Window (Item 3)
    age = now_epoch - evidence.completed_at_epoch
    if age > max_freshness_age_sec:
        return FreshnessState.STALE_CACHED, evidence.completed_at_epoch, [
            f"Refresh evidence expired ({age:.1f}s > max {max_freshness_age_sec:.1f}s)"
        ]

    return FreshnessState.PROVEN_FRESH, evidence.completed_at_epoch, warnings


def parse_agm_list(
    text: str,
    refresh_evidence_map: Optional[Dict[str, Union[RefreshEvidence, dict]]] = None,
    raw_unvalidated_timestamps: Optional[Dict[str, float]] = None,
    max_freshness_age_sec: float = 300.0,
    source_label: str = "AGM_CLI_LIST",
    supervisor_session_id: Optional[str] = None,
    session_secret: Optional[str] = None,
    allow_synthetic_test_origin: bool = False,
    lenient_parser: bool = False,
    now_epoch: Optional[float] = None
) -> List[AccountQuotaSummary]:
    """
    Parses the standard output of `agm list`.
    Enforces strict table header validation and RefreshEvidence trust invariants.
    """
    now = now_epoch if now_epoch is not None else time.time()
    ev_map = refresh_evidence_map or {}

    lines = text.strip().splitlines()
    if not lines:
        return []

    if any("No accounts yet" in line for line in lines):
        return []

    results: List[AccountQuotaSummary] = []
    header_found = False
    col_bounds = None
    format_state = FormatSupportState.FORMAT_UNSUPPORTED

    for line in lines:
        if "EMAIL" in line and "STATUS" in line and "GEM-PRO" in line and "GEM-FLASH" in line and "CLAUDE" in line:
            header_found = True
            email_idx = line.find("EMAIL")
            status_idx = line.find("STATUS")
            gp_idx = line.find("GEM-PRO")
            gf_idx = line.find("GEM-FLASH")
            cl_idx = line.find("CLAUDE")
            if email_idx < status_idx < gp_idx < gf_idx < cl_idx:
                col_bounds = (email_idx, status_idx, gp_idx, gf_idx, cl_idx)
                format_state = FormatSupportState.FORMAT_SUPPORTED
            break

    if (not header_found or format_state == FormatSupportState.FORMAT_UNSUPPORTED) and not lenient_parser:
        return [
            AccountQuotaSummary(
                safe_account_ref="UNKNOWN_UNSUPPORTED_SCHEMA",
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
                parse_warnings=["AGM output header does not match expected supported schema. Fail closed."],
                eligible=False
            )
        ]

    for line in lines:
        line_clean = line.strip()
        if not line_clean:
            continue
        if "EMAIL" in line and "STATUS" in line:
            continue
        if line_clean.startswith("---") or line_clean.startswith("==="):
            continue

        warnings: List[str] = []
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
            warnings.append("Parsed using research-lenient fallback tokenization")
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
            warnings.append(f"Malformed GEM-PRO quota string: '{gp_part}'")
        if gf_part and gf_part != "-" and gf_val is None:
            warnings.append(f"Malformed GEM-FLASH quota string: '{gf_part}'")
        if cl_part and cl_part != "-" and cl_val is None:
            warnings.append(f"Malformed CLAUDE quota string: '{cl_part}'")

        # Validate Refresh Evidence
        if raw_unvalidated_timestamps and email_part in raw_unvalidated_timestamps:
            warnings.append("Raw unvalidated timestamp provided without RefreshEvidence; rejected as STALE_CACHED")
            freshness = FreshnessState.STALE_CACHED
            ref_confirmed_at = None
        elif email_part in ev_map:
            freshness, ref_confirmed_at, ev_warn = validate_refresh_evidence(
                ev_map[email_part],
                canonical_account=email_part,
                now_epoch=now,
                max_freshness_age_sec=max_freshness_age_sec,
                expected_session_id=supervisor_session_id,
                session_secret=session_secret,
                allow_synthetic_test_origin=allow_synthetic_test_origin
            )
            warnings.extend(ev_warn)
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
            safe_account_ref=email_part,
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
            parse_warnings=warnings,
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
    session_secret: Optional[str] = None,
    allow_synthetic_test_origin: bool = False,
    now_epoch: Optional[float] = None
) -> Optional[AccountQuotaSummary]:
    """
    Parses output of `agm info <email>`.
    Fails closed if the model table format is corrupted or missing expected headers.
    """
    now = now_epoch if now_epoch is not None else time.time()
    lines = text.strip().splitlines()
    if not lines:
        return None

    email = ""
    is_expired = False
    models: Dict[str, ModelQuotaDetail] = {}
    warnings: List[str] = []
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
            warnings.append("No quota data recorded in store")
        elif "PROVIDER" in line and "MODEL" in line and "SCORE" in line:
            header_found = True
            in_table = True
            continue
        elif line_clean.startswith("---") or line_clean.startswith("==="):
            continue
        elif in_table and line_clean:
            tokens = line_clean.split()
            if len(tokens) >= 3:
                provider = tokens[0]
                model = tokens[1]
                score_str = tokens[2]
                reset_str = tokens[3] if len(tokens) > 3 else None
                if reset_str and not earliest_reset:
                    earliest_reset = reset_str
                pct = parse_percentage_field(score_str)
                if pct is None and score_str != "-":
                    warnings.append(f"Malformed score for model {model}: '{score_str}'")

                models[model] = ModelQuotaDetail(
                    model_name=model,
                    provider=provider,
                    remaining_pct=pct,
                    reset_time=reset_str,
                    freshness_state=FreshnessState.STALE_CACHED
                )

    if not email:
        return None

    # Fail closed if table header was missing or corrupted in info output
    format_state = FormatSupportState.FORMAT_SUPPORTED if header_found else FormatSupportState.FORMAT_UNSUPPORTED
    if not header_found:
        warnings.append("AGM info output missing expected PROVIDER/MODEL/SCORE headers; fail closed")

    # Validate provenance for info mode
    if raw_unvalidated_timestamp is not None:
        warnings.append("Raw unvalidated timestamp provided without RefreshEvidence; rejected as STALE_CACHED")
        freshness = FreshnessState.STALE_CACHED
        ref_confirmed_at = None
    elif refresh_evidence is not None:
        freshness, ref_confirmed_at, ev_warn = validate_refresh_evidence(
            refresh_evidence,
            canonical_account=email,
            now_epoch=now,
            max_freshness_age_sec=max_freshness_age_sec,
            expected_session_id=supervisor_session_id,
            session_secret=session_secret,
            allow_synthetic_test_origin=allow_synthetic_test_origin
        )
        warnings.extend(ev_warn)
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
        safe_account_ref=email,
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
        parse_warnings=warnings,
        eligible=eligible
    )


def main():
    parser = argparse.ArgumentParser(description="Inspect and normalize AGM quota output with validated provenance.")
    parser.add_argument("--file", "-f", help="Read raw AGM output from file")
    parser.add_argument("--mode", "-m", choices=["list", "info", "auto"], default="auto", help="Parsing mode")
    parser.add_argument("--session-id", help="Mandatory supervisor session ID for production validation")
    parser.add_argument("--session-secret", help="Optional secret for HMAC evidence signature verification")
    parser.add_argument("--provenance-json", help="Optional JSON dict of RefreshEvidence mapping email -> record")
    parser.add_argument("--allow-synthetic-test", action="store_true", help="Explicit test mode to allow synthetic origin")
    parser.add_argument("--research-lenient-parser", action="store_true", help="Enable research fallback tokenization")
    args = parser.parse_args()

    ev_map = {}
    if args.provenance_json:
        try:
            ev_map = json.loads(args.provenance_json)
        except Exception as e:
            print(f"Error parsing provenance JSON: {e}", file=sys.stderr)
            sys.exit(1)

    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        if not sys.stdin.isatty():
            content = sys.stdin.read()
        else:
            try:
                res = subprocess.run(["agm", "list"], capture_output=True, text=True, check=True)
                content = res.stdout
            except Exception as e:
                print(f"Error executing agm list: {e}", file=sys.stderr)
                sys.exit(1)

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
            session_secret=args.session_secret,
            allow_synthetic_test_origin=args.allow_synthetic_test
        )
        data = asdict(res) if res else None
    else:
        res_list = parse_agm_list(
            content,
            refresh_evidence_map=ev_map,
            supervisor_session_id=args.session_id,
            session_secret=args.session_secret,
            allow_synthetic_test_origin=args.allow_synthetic_test,
            lenient_parser=args.research_lenient_parser
        )
        data = [asdict(r) for r in res_list]

    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
