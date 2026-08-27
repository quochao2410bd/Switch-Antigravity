#!/usr/bin/env python3
"""
selection_policy.py

Thin supervisor decision policy for Switch-Antigravity watchdog (Round 6 Architecture).

Architectural Role & Scope (Items 8 & 10):
1. NOT A MULTI-ACCOUNT MANAGER:
   - AGM is the authoritative multi-account manager (storage, encrypted tokens, OAuth, aliases, switch).
   - selection_policy.py is ONLY a thin fail-closed decision evaluator for candidate accounts
     already managed by AGM and parsed by inspect_quota.py.
2. Purpose of Thin Policy (Item 9 & 10):
   - Bridges gaps where AGM built-in auto-switch cannot be directly used by the autonomous watchdog:
     a. Enforces target-specific scope ('agy' Credential Store only; never switches all surfaces).
     b. Enforces strict per-model quota thresholds (Gemini Pro vs Flash, without broad model matching).
     c. Enforces supervisor-level bounded rotation attempts and cooldown failure penalties.
     d. Requires cryptographic freshness provenance before selecting an account.
3. Privacy Contract (Items 7 & 11):
   - Decision logs and evaluated candidate tables expose ONLY pseudonymous account_ref (acc_<hash>).
   - selected_account (canonical email) is returned strictly for internal orchestration to invoke 'agm switch'.
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
from inspect_quota import AccountQuotaSummary, FormatSupportState, FreshnessState
from refresh_quota_safe import is_canonical_email, pseudonymize_account


class ModelGroup(str, Enum):
    GEMINI_PRO = "gemini-pro"
    GEMINI_FLASH = "gemini-flash"
    CLAUDE = "claude"


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
    last_attempt_epoch: float = 0.0
    cooldown_until_epoch: float = 0.0


@dataclass
class SelectionConfig:
    min_quota_pct: int = 20
    max_quota_age_sec: float = 300.0
    cooldown_sec: float = 600.0
    max_rotation_attempts: int = 3
    target_model_group: str = "gemini-pro"


@dataclass
class SelectionResult:
    selected_account: Optional[str]        # Internal canonical email for switch invocation
    selected_account_ref: Optional[str]    # Safe pseudonymous ref for supervisor logging
    terminal_state: TerminalState
    decision_reason: str
    evaluated_candidates: List[Dict[str, any]]
    rotation_count: int

    def to_sanitized_dto(self) -> dict:
        """Sanitized representation guaranteed free of raw emails in decision metadata."""
        return {
            "selected_account_ref": self.selected_account_ref,
            "terminal_state": self.terminal_state.value,
            "decision_reason": self.decision_reason,
            "evaluated_candidates": self.evaluated_candidates,
            "rotation_count": self.rotation_count
        }


class AccountSelector:
    def __init__(self, config: Optional[SelectionConfig] = None):
        self.config = config or SelectionConfig()
        self.penalties: Dict[str, AccountPenaltyState] = {}
        self.rotation_attempts: int = 0

    def record_failure(self, account_ref: str, reason: str, now: Optional[float] = None) -> None:
        """Record a switch or verification failure for an account and apply cooldown."""
        t = now if now is not None else time.time()
        state = self.penalties.get(account_ref, AccountPenaltyState(account_ref=account_ref))
        state.failure_count += 1
        state.last_failure_reason = reason
        state.last_attempt_epoch = t
        state.cooldown_until_epoch = t + (self.config.cooldown_sec * state.failure_count)
        self.penalties[account_ref] = state
        self.rotation_attempts += 1

    def record_success(self, account_ref: str, now: Optional[float] = None) -> None:
        """Reset failure penalties upon verified successful activation."""
        if account_ref in self.penalties:
            self.penalties[account_ref].failure_count = 0
            self.penalties[account_ref].cooldown_until_epoch = 0.0
        self.rotation_attempts = 0

    def select_next_account(
        self,
        accounts: List[AccountQuotaSummary],
        current_active_account: Optional[str] = None,
        now: Optional[float] = None
    ) -> SelectionResult:
        t = now if now is not None else time.time()

        try:
            validated_model = ModelGroup(self.config.target_model_group.lower().strip())
        except (ValueError, AttributeError):
            return SelectionResult(
                selected_account=None,
                selected_account_ref=None,
                terminal_state=TerminalState.FAILED_SAFE,
                decision_reason=f"INVALID_MODEL_GROUP: '{self.config.target_model_group}' is not a supported ModelGroup enum",
                evaluated_candidates=[],
                rotation_count=self.rotation_attempts
            )

        if self.rotation_attempts >= self.config.max_rotation_attempts:
            return SelectionResult(
                selected_account=None,
                selected_account_ref=None,
                terminal_state=TerminalState.FAILED_SAFE,
                decision_reason=f"Exceeded maximum rotation attempts ({self.rotation_attempts}/{self.config.max_rotation_attempts})",
                evaluated_candidates=[],
                rotation_count=self.rotation_attempts
            )

        if not accounts:
            return SelectionResult(
                selected_account=None,
                selected_account_ref=None,
                terminal_state=TerminalState.BLOCKED_NO_ACCOUNT,
                decision_reason="No stored accounts found in account store",
                evaluated_candidates=[],
                rotation_count=self.rotation_attempts
            )

        if any(acc.format_support == FormatSupportState.FORMAT_UNSUPPORTED for acc in accounts):
            return SelectionResult(
                selected_account=None,
                selected_account_ref=None,
                terminal_state=TerminalState.FAILED_SAFE,
                decision_reason="AGM output schema is unsupported. Failing closed.",
                evaluated_candidates=[],
                rotation_count=self.rotation_attempts
            )

        evaluated: List[Dict[str, any]] = []
        eligible_candidates: List[Tuple[AccountQuotaSummary, int, int]] = []
        has_stale_or_unknown_account = False

        for acc in accounts:
            c_acc = acc.canonical_account
            ref = acc.account_ref
            penalty = self.penalties.get(ref, AccountPenaltyState(account_ref=ref))
            is_current = bool(current_active_account and c_acc.lower() == current_active_account.lower())
            is_in_cooldown = bool(penalty.cooldown_until_epoch > t)

            if validated_model == ModelGroup.GEMINI_FLASH:
                score = acc.gemini_flash_pct
            elif validated_model == ModelGroup.CLAUDE:
                score = acc.claude_pct
            else:  # GEMINI_PRO
                score = acc.gemini_pro_pct

            eval_entry = {
                "account_ref": ref,
                "target_model_group": validated_model.value,
                "is_canonical_email": is_canonical_email(c_acc),
                "is_current": is_current,
                "is_expired": acc.is_token_expired,
                "in_cooldown": is_in_cooldown,
                "cooldown_remaining_sec": max(0.0, penalty.cooldown_until_epoch - t),
                "failure_count": penalty.failure_count,
                "quota_score": score,
                "freshness_state": acc.freshness_state.value,
                "status": "REJECTED"
            }

            if not is_canonical_email(c_acc):
                eval_entry["reject_reason"] = "Account reference is not a canonical email"
            elif acc.is_token_expired:
                eval_entry["reject_reason"] = "Token is expired"
            elif is_current:
                eval_entry["reject_reason"] = "Currently active exhausted account"
            elif is_in_cooldown:
                eval_entry["reject_reason"] = f"In cooldown until {penalty.cooldown_until_epoch:.0f} ({penalty.last_failure_reason})"
            elif acc.freshness_state == FreshnessState.STALE_CACHED:
                has_stale_or_unknown_account = True
                eval_entry["reject_reason"] = "Cached quota lacks verified fresh RefreshEvidence (execute agm refresh)"
            elif acc.freshness_state == FreshnessState.REFRESH_FAILED:
                eval_entry["reject_reason"] = "Recent quota refresh failed"
            elif acc.freshness_state == FreshnessState.UNKNOWN_UNFETCHED or score is None:
                has_stale_or_unknown_account = True
                eval_entry["reject_reason"] = f"Quota score for '{validated_model.value}' is unknown/missing (live refresh required)"
            elif score < self.config.min_quota_pct:
                eval_entry["reject_reason"] = f"Quota score ({score}%) for '{validated_model.value}' below threshold ({self.config.min_quota_pct}%)"
            elif acc.refresh_confirmed_at_epoch and (t - acc.refresh_confirmed_at_epoch) > self.config.max_quota_age_sec:
                has_stale_or_unknown_account = True
                eval_entry["reject_reason"] = f"Refresh evidence expired ({t - acc.refresh_confirmed_at_epoch:.0f}s > max {self.config.max_quota_age_sec}s)"
            else:
                eval_entry["status"] = "ELIGIBLE"
                eligible_candidates.append((acc, score, penalty.failure_count))

            evaluated.append(eval_entry)

        if not eligible_candidates:
            if has_stale_or_unknown_account:
                return SelectionResult(
                    selected_account=None,
                    selected_account_ref=None,
                    terminal_state=TerminalState.BLOCKED_QUOTA_UNKNOWN,
                    decision_reason=f"All candidate accounts have stale/unknown quota for model group '{validated_model.value}'; live refresh required",
                    evaluated_candidates=evaluated,
                    rotation_count=self.rotation_attempts
                )
            return SelectionResult(
                selected_account=None,
                selected_account_ref=None,
                terminal_state=TerminalState.BLOCKED_NO_ACCOUNT,
                decision_reason=f"No eligible accounts remaining with sufficient quota (>= {self.config.min_quota_pct}%) for '{validated_model.value}'",
                evaluated_candidates=evaluated,
                rotation_count=self.rotation_attempts
            )

        eligible_candidates.sort(key=lambda item: (-item[1], item[2], item[0].canonical_account))

        winner_acc = eligible_candidates[0][0]
        winner_score = eligible_candidates[0][1]

        return SelectionResult(
            selected_account=winner_acc.canonical_account,
            selected_account_ref=winner_acc.account_ref,
            terminal_state=TerminalState.NONE,
            decision_reason=f"Selected account {winner_acc.account_ref} with {winner_score}% quota for model group '{validated_model.value}'",
            evaluated_candidates=evaluated,
            rotation_count=self.rotation_attempts
        )


def main():
    print("Selection policy module loaded.")


if __name__ == "__main__":
    main()
