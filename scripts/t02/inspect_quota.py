#!/usr/bin/env python3
"""
inspect_quota.py

Safe read-only parser and normalizer for AGM (Antigravity Manager) quota output.
Converts human-oriented table output into a structured, typed AccountQuota schema
with rigorous freshness provenance tracking.

Core Principles:
1. PARSED_AT is NEVER treated as proof of quota freshness.
2. Freshness requires explicit REFRESH_CONFIRMED_AT provenance.
3. Distinguish clearly between:
   - PROVEN_FRESH: Proven by an explicit, verified successful refresh within max_age.
   - STALE_CACHED: Cached data with no recent refresh confirmation.
   - REFRESH_FAILED: Explicitly recorded refresh failure.
   - UNKNOWN_UNFETCHED: Quota missing, null, or unparseable.
4. Never interpret missing, null, error, or unknown as 0%.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Dict, List, Optional


class FreshnessState(str, Enum):
    PROVEN_FRESH = "PROVEN_FRESH"
    STALE_CACHED = "STALE_CACHED"
    REFRESH_FAILED = "REFRESH_FAILED"
    UNKNOWN_UNFETCHED = "UNKNOWN_UNFETCHED"


@dataclass
class ModelQuotaDetail:
    model_name: str
    provider: str
    remaining_pct: Optional[int]  # None if unknown / unparseable, 0-100 if integer
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
    parsed_at_epoch: int
    refresh_confirmed_at_epoch: Optional[int]
    quota_reset_time: Optional[str]
    freshness_state: FreshnessState
    source: str  # "AGM_CLI_LIST", "AGM_CLI_INFO", "DB_DIRECT", "MOCK_FIXTURE"
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


def parse_agm_list(
    text: str,
    refresh_provenance: Optional[Dict[str, int]] = None,
    failed_refreshes: Optional[List[str]] = None,
    max_freshness_age_sec: int = 300,
    source_label: str = "AGM_CLI_LIST",
    now_epoch: Optional[int] = None
) -> List[AccountQuotaSummary]:
    """
    Parses the standard output of `agm list` or `agm ls`.
    Binds parsed accounts to explicit refresh provenance.
    """
    now = now_epoch if now_epoch is not None else int(time.time())
    prov = refresh_provenance or {}
    fails = set(failed_refreshes or [])

    lines = text.strip().splitlines()
    if not lines:
        return []

    if any("No accounts yet" in line for line in lines):
        return []

    results: List[AccountQuotaSummary] = []
    header_found = False
    col_bounds = None

    for line in lines:
        line_clean = line.strip()
        if not line_clean:
            continue

        if "EMAIL" in line and "STATUS" in line:
            header_found = True
            email_idx = line.find("EMAIL")
            status_idx = line.find("STATUS")
            gp_idx = line.find("GEM-PRO")
            gf_idx = line.find("GEM-FLASH")
            cl_idx = line.find("CLAUDE")
            if all(idx >= 0 for idx in [email_idx, status_idx, gp_idx, gf_idx, cl_idx]):
                col_bounds = (email_idx, status_idx, gp_idx, gf_idx, cl_idx)
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
        else:
            tokens = line_clean.split()
            if len(tokens) < 1 or "@" not in tokens[0]:
                continue
            email_part = tokens[0]
            status_part = tokens[1] if len(tokens) > 1 else ""
            gp_part = tokens[2] if len(tokens) > 2 else ""
            gf_part = tokens[3] if len(tokens) > 3 else ""
            cl_part = tokens[4] if len(tokens) > 4 else ""

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

        # Determine Freshness Provenance
        ref_time = prov.get(email_part)
        if email_part in fails:
            freshness = FreshnessState.REFRESH_FAILED
        elif ref_time is not None:
            age = now - ref_time
            if age <= max_freshness_age_sec:
                freshness = FreshnessState.PROVEN_FRESH
            else:
                freshness = FreshnessState.STALE_CACHED
        elif all(v is None for v in [gp_val, gf_val, cl_val]):
            freshness = FreshnessState.UNKNOWN_UNFETCHED
        else:
            freshness = FreshnessState.STALE_CACHED

        all_zero = (
            any(v == 0 for v in [gp_val, gf_val, cl_val] if v is not None)
            and all(v == 0 for v in [gp_val, gf_val, cl_val] if v is not None)
        )
        eligible = (not is_expired) and (not all_zero) and (freshness == FreshnessState.PROVEN_FRESH)

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
            refresh_confirmed_at_epoch=ref_time,
            quota_reset_time=None,
            freshness_state=freshness,
            source=source_label,
            parse_warnings=warnings,
            eligible=eligible
        ))

    return results


def parse_agm_info(
    text: str,
    refresh_confirmed_at_epoch: Optional[int] = None,
    max_freshness_age_sec: int = 300,
    source_label: str = "AGM_CLI_INFO",
    now_epoch: Optional[int] = None
) -> Optional[AccountQuotaSummary]:
    """
    Parses output of `agm info <email>`.
    """
    now = now_epoch if now_epoch is not None else int(time.time())
    lines = text.strip().splitlines()
    if not lines:
        return None

    email = ""
    is_expired = False
    models: Dict[str, ModelQuotaDetail] = {}
    warnings: List[str] = []
    in_table = False
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

                if refresh_confirmed_at_epoch is not None:
                    if (now - refresh_confirmed_at_epoch) <= max_freshness_age_sec:
                        m_fresh = FreshnessState.PROVEN_FRESH
                    else:
                        m_fresh = FreshnessState.STALE_CACHED
                elif pct is None:
                    m_fresh = FreshnessState.UNKNOWN_UNFETCHED
                else:
                    m_fresh = FreshnessState.STALE_CACHED

                models[model] = ModelQuotaDetail(
                    model_name=model,
                    provider=provider,
                    remaining_pct=pct,
                    reset_time=reset_str,
                    freshness_state=m_fresh
                )

    if not email:
        return None

    gp_pcts = [m.remaining_pct for name, m in models.items() if "gemini" in name.lower() and "pro" in name.lower() and m.remaining_pct is not None]
    gf_pcts = [m.remaining_pct for name, m in models.items() if "gemini" in name.lower() and "flash" in name.lower() and m.remaining_pct is not None]
    cl_pcts = [m.remaining_pct for name, m in models.items() if "claude" in name.lower() and m.remaining_pct is not None]

    gp_val = min(gp_pcts) if gp_pcts else None
    gf_val = min(gf_pcts) if gf_pcts else None
    cl_val = min(cl_pcts) if cl_pcts else None

    if refresh_confirmed_at_epoch is not None:
        age = now - refresh_confirmed_at_epoch
        freshness = FreshnessState.PROVEN_FRESH if age <= max_freshness_age_sec else FreshnessState.STALE_CACHED
    elif len(models) == 0:
        freshness = FreshnessState.UNKNOWN_UNFETCHED
    else:
        freshness = FreshnessState.STALE_CACHED

    eligible = not is_expired and (freshness == FreshnessState.PROVEN_FRESH) and any(m.remaining_pct is not None and m.remaining_pct > 0 for m in models.values())

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
        refresh_confirmed_at_epoch=refresh_confirmed_at_epoch,
        quota_reset_time=earliest_reset,
        freshness_state=freshness,
        source=source_label,
        parse_warnings=warnings,
        eligible=eligible
    )


def main():
    parser = argparse.ArgumentParser(description="Inspect and normalize AGM quota output with freshness provenance.")
    parser.add_argument("--file", "-f", help="Read raw AGM output from file")
    parser.add_argument("--mode", "-m", choices=["list", "info", "auto"], default="auto", help="Parsing mode")
    parser.add_argument("--provenance-json", help="Optional JSON dict mapping email -> refresh_confirmed_at timestamp")
    args = parser.parse_args()

    prov = {}
    if args.provenance_json:
        try:
            prov = json.loads(args.provenance_json)
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
        res = parse_agm_info(content)
        data = asdict(res) if res else None
    else:
        res_list = parse_agm_list(content, refresh_provenance=prov)
        data = [asdict(r) for r in res_list]

    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
