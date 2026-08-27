#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_ROOT = os.path.abspath(os.path.join(HERE, ".."))
for rel in ("t01", "t02", "t03"):
    p = os.path.join(SCRIPTS_ROOT, rel)
    if p not in sys.path:
        sys.path.insert(0, p)

from quota_detector import create_baseline, poll_new_events
from trusted_agm_runner import TrustedAgmIdentity, execute_trusted_agm
from refresh_quota_safe import execute_safe_refresh, is_canonical_email, pseudonymize_account
from inspect_quota import parse_agm_list
from selection_policy import AccountSelector, SelectionConfig, TerminalState
from switch_account_safe import execute_safe_switch, SwitchOutcome
from verify_active_account import verify_active_account, CredentialVerificationStatus
from send_resume import execute_resume_pipeline


@dataclass
class ProductionAdapterConfig:
    log_path: str
    conversation_uuid: str
    expected_agm_sha256: str
    language_server_pid: int
    t03_journal_path: str
    resume_prompt: str
    model_group: str = "gemini-pro"
    min_quota_pct: int = 20
    max_rotation_attempts: int = 3
    expected_agm_path: Optional[str] = None
    t03_timeout_sec: int = 30
    execute_switch: bool = False


class ProductionAdapters:
    """
    Concrete bridge from the integration supervisor to accepted T01/T02/T03 modules.

    Important current integration boundary:
    - Credential-store identity can be independently verified by T02.
    - Desktop adoption of that credential after a switch remains UNKNOWN.
    - Therefore desktop_adoption_verifier_available() intentionally returns False.
      The supervisor will stop BEFORE any credential mutation unless this adapter is
      later extended with a verified Desktop adoption mechanism.
    """

    def __init__(self, config: ProductionAdapterConfig):
        self.config = config
        self.identity = TrustedAgmIdentity(
            expected_binary_sha256=config.expected_agm_sha256,
            canonical_executable_path=config.expected_agm_path,
        )

    def create_quota_baseline(self, session_id: str, ls_pid: int) -> Dict[str, Any]:
        baseline, _ = create_baseline(self.config.log_path, ls_pid=ls_pid, supervisor_session_id=session_id)
        return baseline

    def poll_quota(self, baseline: Dict[str, Any], session_id: str, ls_pid: int) -> Dict[str, Any]:
        result, _ = poll_new_events(
            baseline,
            log_path=self.config.log_path,
            current_ls_pid=ls_pid,
            current_session_id=session_id,
            include_raw_log=False,
        )
        return result

    def current_ls_pid(self) -> int:
        return int(self.config.language_server_pid)

    def get_current_account(self) -> Dict[str, Any]:
        result = verify_active_account(expected_account=None, introspect_network=True)
        verified = (
            result.status == CredentialVerificationStatus.CREDENTIAL_STORE_IDENTITY_VERIFIED
            and bool(result.raw_detected_email)
        )
        return {
            "verified": verified,
            "account": result.raw_detected_email if verified else None,
            "account_ref": pseudonymize_account(result.raw_detected_email) if verified else result.account_ref,
            "status": result.status.value,
            "error_code": result.error_code,
        }

    def _trusted_list(self) -> Optional[str]:
        res = execute_trusted_agm(["list"], trusted_identity=self.identity, timeout_sec=15)
        if not res.command_executed or not res.success:
            return None
        return res.stdout

    def discover_candidates(self, session_id: str, current_account: str) -> List[Dict[str, Any]]:
        first_output = self._trusted_list()
        if first_output is None:
            return []

        preliminary = parse_agm_list(
            first_output,
            supervisor_session_id=session_id,
            trusted_identity=self.identity,
        )

        evidence_map = {}
        for acc in preliminary:
            email = acc.canonical_account
            if not is_canonical_email(email):
                continue
            if current_account and email.lower() == current_account.lower():
                continue
            evidence_map[email] = execute_safe_refresh(
                email,
                supervisor_session_id=session_id,
                trusted_identity=self.identity,
                live_network=True,
            )

        second_output = self._trusted_list()
        if second_output is None:
            return []

        accounts = parse_agm_list(
            second_output,
            refresh_evidence_map=evidence_map,
            supervisor_session_id=session_id,
            trusted_identity=self.identity,
        )

        ordered: List[Dict[str, Any]] = []
        remaining = list(accounts)
        while remaining:
            selector = AccountSelector(SelectionConfig(
                min_quota_pct=self.config.min_quota_pct,
                max_rotation_attempts=max(self.config.max_rotation_attempts, len(remaining) + 1),
                target_model_group=self.config.model_group,
            ))
            selected = selector.select_next_account(remaining, current_active_account=current_account)
            if selected.terminal_state != TerminalState.NONE or not selected.selected_account:
                break
            chosen = next((a for a in remaining if a.canonical_account == selected.selected_account), None)
            if chosen is None:
                break
            ordered.append({
                "account": chosen.canonical_account,
                "account_ref": chosen.account_ref,
                "eligible": bool(chosen.eligible),
                "gemini_pro_pct": chosen.gemini_pro_pct,
                "gemini_flash_pct": chosen.gemini_flash_pct,
                "claude_pct": chosen.claude_pct,
                "freshness_state": chosen.freshness_state.value,
            })
            remaining = [a for a in remaining if a.canonical_account != chosen.canonical_account]

        return ordered

    def switch_account(self, account: str) -> Dict[str, Any]:
        if not self.config.execute_switch:
            return {"verified": False, "error_code": "EXECUTION_DISABLED"}
        result = execute_safe_switch(
            account,
            target="agy",
            confirm=True,
            introspect_network=True,
            trusted_identity=self.identity,
            private_diagnostic_mode=False,
        )
        return {
            "verified": result.get("status") == SwitchOutcome.CREDENTIAL_IDENTITY_VERIFIED.value,
            "account_ref": result.get("account_ref"),
            "status": result.get("status"),
            "error_code": result.get("error_code") or result.get("status"),
        }

    def desktop_adoption_verifier_available(self) -> bool:
        return False

    def verify_desktop_adoption(self, expected_account_ref: str) -> Dict[str, Any]:
        return {
            "verified": False,
            "status": "BLOCKED_DESKTOP_ADOPTION_UNVERIFIED",
            "account_ref": expected_account_ref,
        }

    def _t03_args(self, send: bool) -> argparse.Namespace:
        return argparse.Namespace(
            conversation_id=self.config.conversation_uuid,
            title=None,
            prompt=self.config.resume_prompt,
            send=send,
            probe_composer_write=False,
            cdp_endpoint=None,
            journal_path=self.config.t03_journal_path,
            timeout=self.config.t03_timeout_sec,
            json=False,
            verbose_private_data=False,
        )

    def resume_conversation(self) -> Dict[str, Any]:
        return asyncio.run(execute_resume_pipeline(self._t03_args(send=True)))

    def probe_resume_progress(self) -> Dict[str, Any]:
        result = asyncio.run(execute_resume_pipeline(self._t03_args(send=False)))
        decision = (result.get("recovery_decision") or {}).get("code")
        verified = decision == "TURN_ALREADY_ACTIVE"
        return {
            "verified": verified,
            "status": "TURN_PROGRESS_VERIFIED" if verified else "TURN_PROGRESS_NOT_YET_VERIFIED",
            "t03_status": result.get("status"),
            "decision": decision,
        }
