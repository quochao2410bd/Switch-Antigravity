#!/usr/bin/env python3
"""
inspect_quota.py

Safe read-only parser and normalizer for AGM (Antigravity Manager) quota output.
Converts human-oriented table output into a structured, typed AccountQuota schema.

Design Rules:
1. Never interpret missing, null, error, or unknown as 0%.
2. Distinguish between 0% (confirmed exhausted) and None/Unknown (unfetched/unparseable).
3. Validate and sanitize email / account references.
4. Support parsing from stdin, file, or direct CLI execution.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional


@dataclass
class ModelQuotaDetail:
    model_name: str
    provider: str
    remaining_pct: Optional[int]  # None if unknown / unparseable, 0-100 if integer
    reset_time: Optional[str]
    freshness_state: str  # "FRESH", "STALE", "UNKNOWN", "ERROR"


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
    observed_at_epoch: int
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


def parse_agm_list(text: str, source_label: str = "AGM_CLI_LIST") -> List[AccountQuotaSummary]:
    """
    Parses the standard output of `agm list` or `agm ls`.
    Expected header:
    EMAIL                                STATUS         GEM-PRO  GEM-FLASH     CLAUDE
    ------------------------------------------------------------------------------------
    """
    import time
    now = int(time.time())
    lines = text.strip().splitlines()
    if not lines:
        return []

    # Check for empty state message
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
            # Find column offsets if possible
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

        if not header_found:
            # If no header found yet, check if line looks like an account row
            tokens = line_clean.split()
            if len(tokens) >= 2 and "@" in tokens[0]:
                email = tokens[0]
                status_raw = tokens[1] if len(tokens) > 1 else ""
                # Parse fallback
                gp = parse_percentage_field(tokens[2]) if len(tokens) > 2 else None
                gf = parse_percentage_field(tokens[3]) if len(tokens) > 3 else None
                cl = parse_percentage_field(tokens[4]) if len(tokens) > 4 else None
                tags = [t.strip() for t in status_raw.split(",") if t.strip()]
                is_expired = "token-exp" in tags
                eligible = (not is_expired) and (gp is None or gp > 0 or gf is None or gf > 0 or cl is None or cl > 0)
                results.append(AccountQuotaSummary(
                    safe_account_ref=email,
                    status_tags=tags,
                    is_active_cli="cli" in tags,
                    is_active_ide="ide" in tags,
                    is_token_expired=is_expired,
                    gemini_pro_pct=gp,
                    gemini_flash_pct=gf,
                    claude_pct=cl,
                    models={},
                    observed_at_epoch=now,
                    source=source_label,
                    parse_warnings=["Parsed without explicit header alignment"],
                    eligible=eligible
                ))
            continue

        # Header was found, parse row
        warnings: List[str] = []
        if col_bounds:
            e_idx, s_idx, gp_idx, gf_idx, cl_idx = col_bounds
            email_part = line[e_idx:s_idx].strip() if len(line) > s_idx else line.strip()
            status_part = line[s_idx:gp_idx].strip() if len(line) > gp_idx else ""
            gp_part = line[gp_idx:gf_idx].strip() if len(line) > gf_idx else ""
            gf_part = line[gf_idx:cl_idx].strip() if len(line) > cl_idx else ""
            cl_part = line[cl_idx:].strip() if len(line) > cl_idx else ""
        else:
            tokens = line.split()
            if len(tokens) < 1 or "@" not in tokens[0]:
                continue
            email_part = tokens[0]
            status_part = tokens[1] if len(tokens) > 1 else ""
            gp_part = tokens[2] if len(tokens) > 2 else ""
            gf_part = tokens[3] if len(tokens) > 3 else ""
            cl_part = tokens[4] if len(tokens) > 4 else ""

        if "@" not in email_part:
            # Skip invalid lines
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

        # Determine eligibility: not expired and has some positive quota or unknown quota requiring refresh
        has_quota = any(v is not None and v > 0 for v in [gp_val, gf_val, cl_val])
        all_zero = all(v == 0 for v in [gp_val, gf_val, cl_val] if v is not None) and any(v == 0 for v in [gp_val, gf_val, cl_val])
        eligible = (not is_expired) and (not all_zero)

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
            observed_at_epoch=now,
            source=source_label,
            parse_warnings=warnings,
            eligible=eligible
        ))

    return results


def parse_agm_info(text: str, source_label: str = "AGM_CLI_INFO") -> Optional[AccountQuotaSummary]:
    """
    Parses output of `agm info <email>`.
    Example:
    Account: alice@example.com
    Token expiry: 2026-08-26T22:30:00+07:00

    PROVIDER     MODEL                                             SCORE  RESET
    ------------------------------------------------------------------------------------------
    GOOGLE       gemini-1.5-pro                                      85%  2026-08-27T00:00:00Z
    """
    import time
    now = int(time.time())
    lines = text.strip().splitlines()
    if not lines:
        return None

    email = ""
    is_expired = False
    models: Dict[str, ModelQuotaDetail] = {}
    warnings: List[str] = []
    in_table = False

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
                pct = parse_percentage_field(score_str)
                if pct is None and score_str != "-":
                    warnings.append(f"Malformed score for model {model}: '{score_str}'")
                models[model] = ModelQuotaDetail(
                    model_name=model,
                    provider=provider,
                    remaining_pct=pct,
                    reset_time=reset_str,
                    freshness_state="FRESH" if pct is not None else "UNKNOWN"
                )

    if not email:
        return None

    # Calculate model group averages/mins
    gp_pcts = [m.remaining_pct for name, m in models.items() if "gemini" in name.lower() and "pro" in name.lower() and m.remaining_pct is not None]
    gf_pcts = [m.remaining_pct for name, m in models.items() if "gemini" in name.lower() and "flash" in name.lower() and m.remaining_pct is not None]
    cl_pcts = [m.remaining_pct for name, m in models.items() if "claude" in name.lower() and m.remaining_pct is not None]

    gp_val = min(gp_pcts) if gp_pcts else None
    gf_val = min(gf_pcts) if gf_pcts else None
    cl_val = min(cl_pcts) if cl_pcts else None

    eligible = not is_expired and (len(models) == 0 or any(m.remaining_pct is not None and m.remaining_pct > 0 for m in models.values()))

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
        observed_at_epoch=now,
        source=source_label,
        parse_warnings=warnings,
        eligible=eligible
    )


def main():
    parser = argparse.ArgumentParser(description="Inspect and normalize AGM quota output.")
    parser.add_argument("--file", "-f", help="Read raw AGM output from file")
    parser.add_argument("--mode", "-m", choices=["list", "info", "auto"], default="auto", help="Parsing mode")
    parser.add_argument("--json", action="store_true", default=True, help="Output JSON format")
    args = parser.parse_args()

    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        if not sys.stdin.isatty():
            content = sys.stdin.read()
        else:
            # Try running `agm list`
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
        res_list = parse_agm_list(content)
        data = [asdict(r) for r in res_list]

    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
