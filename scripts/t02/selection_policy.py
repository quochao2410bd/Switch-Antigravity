#!/usr/bin/env python3
"""
selection_policy.py

Deterministic account selection engine for Switch-Antigravity watchdog.

Guarantees:
1. Stale cached quota is NEVER eligible for selection without explicit fresh provenance.
2. Never repeatedly choose the same exhausted account.
3. Never rotate indefinitely (enforces max_rotations).
4. Never choose accounts in active failure penalty / cooldown.
5. Deterministic, stable tie-breaking.
6. Explicit terminal state classification:
   - BLOCKED_NO_ACCOUNT
   - BLOCKED_QUOTA_UNKNOWN
   - SWITCH_FAILED
   - VERIFY_FAILED
   - FAILED_SAFE
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inspect_quota import AccountQuotaSummary, FreshnessState


class TerminalState(str, Enum):
    NONE = "NONE"
    BLOCKED_NO_ACCOUNT = "BLOCKED_NO_ACCOUNT"
    BLOCKED_QUOTA_UNKNOWN = "BLOCKED_QUOTA_UNKNOWN"
    SWITCH_FAILED = "SWITCH_FAILED"
    VERIFY_FAILED = "VERIFY_FAILED"
    FAILED_SAFE = "FAILED_SAFE"


@dataclass
class AccountPenaltyState:
    account_ref: str
    failure_count: int = 0
    last_failure_reason: str = ""
    last_attempt_epoch: int = 0
    cooldown_until_epoch: int = 0


@dataclass
class SelectionConfig:
    min_quota_pct: int = 20
    max_quota_age_sec: int = 300
    cooldown_sec: int = 600
    max_rotation_attempts: int = 3
    target_model_group: str = "gemini-pro"  # "gemini-pro", "gemini-flash", "claude"


@dataclass
class SelectionResult:
    selected_account: Optional[str]
    terminal_state: TerminalState
    decision_reason: str
    evaluated_candidates: List[Dict[str, any]]
    rotation_count: int


class AccountSelector:
    def __init__(self, config: Optional[SelectionConfig] = None):
        self.config = config or SelectionConfig()
        self.penalties: Dict[str, AccountPenaltyState] = {}
        self.rotation_attempts: int = 0

    def record_failure(self, account_ref: str, reason: str, now: Optional[int] = None) -> None:
        """Record a switch or verification failure for an account and apply cooldown."""
        t = now if now is not None else int(time.time())
        state = self.penalties.get(account_ref, AccountPenaltyState(account_ref=account_ref))
        state.failure_count += 1
        state.last_failure_reason = reason
        state.last_attempt_epoch = t
        state.cooldown_until_epoch = t + (self.config.cooldown_sec * state.failure_count)
        self.penalties[account_ref] = state
        self.rotation_attempts += 1

    def record_success(self, account_ref: str, now: Optional[int] = None) -> None:
        """Reset failure penalties upon verified successful activation."""
        if account_ref in self.penalties:
            self.penalties[account_ref].failure_count = 0
            self.penalties[account_ref].cooldown_until_epoch = 0
        self.rotation_attempts = 0

    def select_next_account(
        self,
        accounts: List[AccountQuotaSummary],
        current_active_ref: Optional[str] = None,
        now: Optional[int] = None
    ) -> SelectionResult:
        t = now if now is not None else int(time.time())

        # Check maximum rotation limit first
        if self.rotation_attempts >= self.config.max_rotation_attempts:
            return SelectionResult(
                selected_account=None,
                terminal_state=TerminalState.FAILED_SAFE,
                decision_reason=f"Exceeded maximum rotation attempts ({self.rotation_attempts}/{self.config.max_rotation_attempts})",
                evaluated_candidates=[],
                rotation_count=self.rotation_attempts
            )

        if not accounts:
            return SelectionResult(
                selected_account=None,
                terminal_state=TerminalState.BLOCKED_NO_ACCOUNT,
                decision_reason="No stored accounts found in account store",
                evaluated_candidates=[],
                rotation_count=self.rotation_attempts
            )

        evaluated: List[Dict[str, any]] = []
        eligible_candidates: List[Tuple[AccountQuotaSummary, int, int]] = []  # (account, quota_score, failure_count)
        has_stale_or_unknown_account = False

        for acc in accounts:
            ref = acc.safe_account_ref
            penalty = self.penalties.get(ref, AccountPenaltyState(account_ref=ref))
            is_current = (current_active_ref and ref.lower() == current_active_ref.lower())
            is_in_cooldown = penalty.cooldown_until_epoch > t

            # Determine model quota score
            model_grp = self.config.target_model_group.lower()
            if "pro" in model_grp:
                score = acc.gemini_pro_pct
            elif "flash" in model_grp:
                score = acc.gemini_flash_pct
            elif "claude" in model_grp:
                score = acc.claude_pct
            else:
                score = acc.gemini_pro_pct

            eval_entry = {
                "account_ref": ref,
                "is_current": is_current,
                "is_expired": acc.is_token_expired,
                "in_cooldown": is_in_cooldown,
                "cooldown_remaining_sec": max(0, penalty.cooldown_until_epoch - t),
                "failure_count": penalty.failure_count,
                "quota_score": score,
                "freshness_state": acc.freshness_state.value,
                "status": "REJECTED"
            }

            # Filter rules
            if acc.is_token_expired:
                eval_entry["reject_reason"] = "Token is expired"
            elif is_current:
                eval_entry["reject_reason"] = "Currently active exhausted account"
            elif is_in_cooldown:
                eval_entry["reject_reason"] = f"In cooldown until {penalty.cooldown_until_epoch} ({penalty.last_failure_reason})"
            elif acc.freshness_state == FreshnessState.STALE_CACHED:
                has_stale_or_unknown_account = True
                eval_entry["reject_reason"] = "Cached quota lacks verified fresh provenance (run agm refresh)"
            elif acc.freshness_state == FreshnessState.REFRESH_FAILED:
                eval_entry["reject_reason"] = "Recent quota refresh failed"
            elif acc.freshness_state == FreshnessState.UNKNOWN_UNFETCHED or score is None:
                has_stale_or_unknown_account = True
                eval_entry["reject_reason"] = "Quota value is missing/unknown (needs refresh)"
            elif score < self.config.min_quota_pct:
                eval_entry["reject_reason"] = f"Quota score ({score}%) below threshold ({self.config.min_quota_pct}%)"
            elif acc.refresh_confirmed_at_epoch and (t - acc.refresh_confirmed_at_epoch) > self.config.max_quota_age_sec:
                has_stale_or_unknown_account = True
                eval_entry["reject_reason"] = f"Refresh provenance expired ({t - acc.refresh_confirmed_at_epoch}s > max {self.config.max_quota_age_sec}s)"
            else:
                eval_entry["status"] = "ELIGIBLE"
                eligible_candidates.append((acc, score, penalty.failure_count))

            evaluated.append(eval_entry)

        if not eligible_candidates:
            if has_stale_or_unknown_account:
                return SelectionResult(
                    selected_account=None,
                    terminal_state=TerminalState.BLOCKED_QUOTA_UNKNOWN,
                    decision_reason="All potential candidate accounts have stale or unknown quota; live refresh required",
                    evaluated_candidates=evaluated,
                    rotation_count=self.rotation_attempts
                )
            return SelectionResult(
                selected_account=None,
                terminal_state=TerminalState.BLOCKED_NO_ACCOUNT,
                decision_reason="No eligible accounts remaining with sufficient quota and valid tokens",
                evaluated_candidates=evaluated,
                rotation_count=self.rotation_attempts
            )

        # Deterministic sorting:
        # 1. Quota score descending (-score)
        # 2. Failure count ascending (failure_count)
        # 3. Account reference lexicographical ascending (ref)
        eligible_candidates.sort(key=lambda item: (-item[1], item[2], item[0].safe_account_ref))

        winner = eligible_candidates[0][0].safe_account_ref
        winner_score = eligible_candidates[0][1]

        return SelectionResult(
            selected_account=winner,
            terminal_state=TerminalState.NONE,
            decision_reason=f"Selected {winner} with {winner_score}% quota for model group '{self.config.target_model_group}'",
            evaluated_candidates=evaluated,
            rotation_count=self.rotation_attempts
        )


def test_selection_policy():
    print("=== Testing Selection Policy with Freshness Provenance ===")
    config = SelectionConfig(min_quota_pct=20, max_rotation_attempts=3)
    selector = AccountSelector(config)

    now = 1000000

    # 1. Test stale cached account (parsed now, but refreshed 5 hours ago) -> MUST BE REJECTED!
    stale_acc = AccountQuotaSummary(
        safe_account_ref="stale@test.com",
        status_tags=[],
        is_active_cli=False,
        is_active_ide=False,
        is_token_expired=False,
        gemini_pro_pct=100,  # 100% quota, but stale!
        gemini_flash_pct=100,
        claude_pct=100,
        models={},
        parsed_at_epoch=now,
        refresh_confirmed_at_epoch=now - 18000,  # 5 hours ago
        quota_reset_time=None,
        freshness_state=FreshnessState.STALE_CACHED,
        source="MOCK",
        parse_warnings=[],
        eligible=False
    )
    res_stale = selector.select_next_account([stale_acc], now=now)
    assert res_stale.selected_account is None
    assert res_stale.terminal_state == TerminalState.BLOCKED_QUOTA_UNKNOWN
    print("  [PASS] Stale cached quota (5 hours old) rejected -> BLOCKED_QUOTA_UNKNOWN")

    # 2. Test proven fresh accounts
    a1 = AccountQuotaSummary("acc1@test.com", ["cli"], True, False, False, 10, 50, 0, {}, now, now - 10, None, FreshnessState.PROVEN_FRESH, "MOCK", [], False)
    a2 = AccountQuotaSummary("acc2@test.com", [], False, False, False, 80, 80, 80, {}, now, now - 10, None, FreshnessState.PROVEN_FRESH, "MOCK", [], True)
    a3 = AccountQuotaSummary("acc3@test.com", [], False, False, False, 90, 90, 90, {}, now, now - 10, None, FreshnessState.PROVEN_FRESH, "MOCK", [], True)
    a4 = AccountQuotaSummary("acc4@test.com", ["token-exp"], False, False, True, 100, 100, 100, {}, now, now - 10, None, FreshnessState.PROVEN_FRESH, "MOCK", [], False)

    res = selector.select_next_account([a1, a2, a3, a4], current_active_ref="acc1@test.com", now=now)
    assert res.selected_account == "acc3@test.com"
    assert res.terminal_state == TerminalState.NONE
    print("  [PASS] Chosen highest proven fresh non-current account: acc3@test.com (90%)")

    # 3. Simulate failure on acc3
    selector.record_failure("acc3@test.com", "Verification failed", now=now)
    res2 = selector.select_next_account([a1, a2, a3, a4], current_active_ref="acc1@test.com", now=now)
    assert res2.selected_account == "acc2@test.com"
    print("  [PASS] After failure penalty, selected next eligible: acc2@test.com")

    # 4. Simulate failure on acc2
    selector.record_failure("acc2@test.com", "Verification failed", now=now)
    res3 = selector.select_next_account([a1, a2, a3, a4], current_active_ref="acc1@test.com", now=now)
    assert res3.selected_account is None
    assert res3.terminal_state == TerminalState.BLOCKED_NO_ACCOUNT
    print("  [PASS] No accounts remaining above threshold -> BLOCKED_NO_ACCOUNT")

    # 5. Simulate 3rd failure hitting max rotations
    selector.record_failure("acc1@test.com", "Fatal crash", now=now)
    res4 = selector.select_next_account([a1, a2, a3, a4], current_active_ref="acc1@test.com", now=now)
    assert res4.selected_account is None
    assert res4.terminal_state == TerminalState.FAILED_SAFE
    print("  [PASS] Max rotation limit reached -> FAILED_SAFE")

    print("\nAll selection policy tests passed!")


if __name__ == "__main__":
    test_selection_policy()
