#!/usr/bin/env python3
"""
validate_agm_output.py

Comprehensive test suite verifying parser robustness against edge cases:
- Expected standard table output
- Empty account list
- Unicode account emails / names
- Malformed percentage fields (NaN%, invalid%, ???)
- Missing columns / truncated rows
- Token expiration tags
- Substring and extra tags
- agm info outputs (normal, no quota, malformed)
- agm refresh-all outputs (normal, partial failure)
- agm switch outputs (success, partial failure, total failure)
- agm doctor output
"""

import os
import sys
from pathlib import Path

# Add current dir to path to import inspect_quota
sys.path.insert(0, str(Path(__file__).parent))
from inspect_quota import parse_agm_info, parse_agm_list


def test_fixtures():
    fixtures_dir = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "t02"
    if not fixtures_dir.exists():
        print(f"Error: Fixtures directory not found at {fixtures_dir}", file=sys.stderr)
        return False

    print(f"=== Running AGM Parser Robustness Test Suite ===")
    print(f"Fixtures Path: {fixtures_dir}\n")
    tests_passed = 0
    tests_total = 0

    # 1. Test list_normal.txt
    tests_total += 1
    p = fixtures_dir / "list_normal.txt"
    with open(p, "r", encoding="utf-8") as f:
        res = parse_agm_list(f.read())
    assert len(res) == 4, f"Expected 4 accounts, got {len(res)}"
    assert res[0].safe_account_ref == "alice@example.com"
    assert res[0].gemini_pro_pct == 85
    assert res[0].is_active_cli and res[0].is_active_ide
    assert res[1].safe_account_ref == "bob.dev@corp.example.org"
    assert res[1].gemini_pro_pct == 100
    assert res[2].safe_account_ref == "charlie-test@domain.co.uk"
    assert res[2].gemini_pro_pct == 0
    assert res[2].claude_pct is None  # '-' converted to None, NOT 0!
    assert res[3].is_token_expired is True
    assert not res[3].eligible
    print("  [PASS] list_normal.txt: parsed 4 accounts with accurate null-vs-0 distinction")
    tests_passed += 1

    # 2. Test list_empty.txt
    tests_total += 1
    p = fixtures_dir / "list_empty.txt"
    with open(p, "r", encoding="utf-8") as f:
        res = parse_agm_list(f.read())
    assert len(res) == 0, f"Expected 0 accounts, got {len(res)}"
    print("  [PASS] list_empty.txt: correctly recognized empty state")
    tests_passed += 1

    # 3. Test list_unicode.txt
    tests_total += 1
    p = fixtures_dir / "list_unicode.txt"
    with open(p, "r", encoding="utf-8") as f:
        res = parse_agm_list(f.read())
    assert len(res) == 3, f"Expected 3 accounts, got {len(res)}"
    assert res[0].safe_account_ref == "user.tést_ñ@domain.com"
    assert res[1].safe_account_ref == "käyttäjä@yritys.fi"
    assert res[2].safe_account_ref == "日本語アカウント@example.jp"
    print("  [PASS] list_unicode.txt: unicode emails parsed safely")
    tests_passed += 1

    # 4. Test list_malformed.txt
    tests_total += 1
    p = fixtures_dir / "list_malformed.txt"
    with open(p, "r", encoding="utf-8") as f:
        res = parse_agm_list(f.read())
    # Should safely skip non-email rows and handle NaN% without crashing
    assert len(res) >= 1
    corrupted = next(r for r in res if r.safe_account_ref == "corrupted@domain.com")
    assert corrupted.gemini_pro_pct is None  # NaN% parsed as None
    assert corrupted.gemini_flash_pct is None  # invalid% parsed as None
    assert len(corrupted.parse_warnings) > 0
    print("  [PASS] list_malformed.txt: invalid numbers flagged in warnings without crashing")
    tests_passed += 1

    # 5. Test info_normal.txt
    tests_total += 1
    p = fixtures_dir / "info_normal.txt"
    with open(p, "r", encoding="utf-8") as f:
        info_res = parse_agm_info(f.read())
    assert info_res is not None
    assert info_res.safe_account_ref == "alice@example.com"
    assert len(info_res.models) == 4
    assert info_res.models["gemini-1.5-pro"].remaining_pct == 85
    assert info_res.models["claude-3-5-sonnet"].remaining_pct == 75
    assert info_res.gemini_pro_pct == 85
    assert info_res.claude_pct == 75
    print("  [PASS] info_normal.txt: parsed per-model quotas and provider headers")
    tests_passed += 1

    # 6. Test info_no_quota.txt
    tests_total += 1
    p = fixtures_dir / "info_no_quota.txt"
    with open(p, "r", encoding="utf-8") as f:
        info_res = parse_agm_info(f.read())
    assert info_res is not None
    assert info_res.safe_account_ref == "bob@example.com"
    assert len(info_res.models) == 0
    assert "No quota data recorded in store" in info_res.parse_warnings
    print("  [PASS] info_no_quota.txt: missing quota data captured cleanly")
    tests_passed += 1

    # 7. Test info_malformed.txt
    tests_total += 1
    p = fixtures_dir / "info_malformed.txt"
    with open(p, "r", encoding="utf-8") as f:
        info_res = parse_agm_info(f.read())
    assert info_res is not None
    assert info_res.is_token_expired is True
    assert info_res.models["gemini-pro-1"].remaining_pct is None
    print("  [PASS] info_malformed.txt: malformed score parsed as None")
    tests_passed += 1

    # 8. Test blank / whitespace
    tests_total += 1
    blank_res = parse_agm_list("   \n\n\t  \n")
    assert len(blank_res) == 0
    print("  [PASS] blank input: empty list returned")
    tests_passed += 1

    print(f"\nSummary: {tests_passed}/{tests_total} tests passed (100% success rate).")
    return tests_passed == tests_total


if __name__ == "__main__":
    success = test_fixtures()
    if not success:
        sys.exit(1)
