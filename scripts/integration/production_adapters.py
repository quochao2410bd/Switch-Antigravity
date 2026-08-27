#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_ROOT = os.path.abspath(os.path.join(HERE, ".."))
for rel in ("t01", "t02", "t03"):
    p = os.path.join(SCRIPTS_ROOT, rel)
    if p not in sys.path:
        sys.path.insert(0, p)

from desktop_identity import DesktopRuntimeProbe, pseudonymize_email
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
    """Concrete fail-closed bridge across accepted T01/T02/T03 lanes.

    Runtime identity is sourced from the exact running Antigravity language_server
    through local GetUserStatus.email. Credential Manager is used only for the T02
    credential switch/verification boundary, never as proof that Desktop adopted it.
    """

    def __init__(self, config: ProductionAdapterConfig):
        self.config = config
        self.identity = TrustedAgmIdentity(
            expected_binary_sha256=config.expected_agm_sha256,
            canonical_executable_path=config.expected_agm_path,
        )
        self.desktop_probe = DesktopRuntimeProbe()
        self._last_language_server_pid = int(config.language_server_pid)

    def _cycle_ref(self, event_id: str) -> str:
        return hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:12]

    def _cycle_journal_path(self, event_id: str) -> str:
        root, ext = os.path.splitext(self.config.t03_journal_path)
        return f"{root}.cycle_{self._cycle_ref(event_id)}{ext or '.json'}"

    def _cycle_prompt(self, event_id: str) -> str:
        return f"{self.config.resume_prompt.rstrip()}\nRecovery cycle: {self._cycle_ref(event_id)}."

    def _observe_desktop_account_private(self) -> Dict[str, Any]:
        snapshot, status = self.desktop_probe.inspect(self._last_language_server_pid)
        if snapshot is None:
            snapshot, status = self.desktop_probe.inspect(None)
        if snapshot is None:
            return {"verified": False, "status": status, "account": None, "account_ref": None}
        try:
            responses = self.desktop_probe._user_status_fetcher(snapshot)
        except Exception:
            return {
                "verified": False,
                "status": "DESKTOP_GET_USER_STATUS_FAILED",
                "account": None,
                "account_ref": None,
                "language_server_pid": snapshot.language_server_pid,
            }
        emails = set()
        for response in responses:
            if not isinstance(response, dict):
                continue
            user_status = response.get("userStatus")
            email = user_status.get("email") if isinstance(user_status, dict) else None
            if isinstance(email, str) and is_canonical_email(email.strip().lower()):
                emails.add(email.strip().lower())
        if len(emails) != 1:
            return {
                "verified": False,
                "status": "DESKTOP_IDENTITY_EMAIL_MISSING" if not emails else "DESKTOP_IDENTITY_AMBIGUOUS",
                "account": None,
                "account_ref": None,
                "language_server_pid": snapshot.language_server_pid,
            }
        email = next(iter(emails))
        self._last_language_server_pid = snapshot.language_server_pid
        return {
            "verified": True,
            "status": "DESKTOP_IDENTITY_OBSERVED",
            "account": email,
            "account_ref": pseudonymize_email(email),
            "language_server_pid": snapshot.language_server_pid,
            "source": "LANGUAGE_SERVER_GET_USER_STATUS",
        }

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
        snapshot, _ = self.desktop_probe.inspect(self._last_language_server_pid)
        if snapshot is None:
            snapshot, _ = self.desktop_probe.inspect(None)
        if snapshot is None:
            return 0
        self._last_language_server_pid = snapshot.language_server_pid
        return snapshot.language_server_pid

    def get_current_account(self) -> Dict[str, Any]:
        return self._observe_desktop_account_private()

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

    def _t03_args(self, event_id: str, send: bool) -> argparse.Namespace:
        return argparse.Namespace(
            conversation_id=self.config.conversation_uuid,
            title=None,
            prompt=self._cycle_prompt(event_id),
            send=send,
            probe_composer_write=False,
            cdp_endpoint=None,
            journal_path=self._cycle_journal_path(event_id),
            timeout=self.config.t03_timeout_sec,
            json=False,
            verbose_private_data=False,
        )

    def _t03_preflight(self, event_id: str) -> Dict[str, Any]:
        result = asyncio.run(execute_resume_pipeline(self._t03_args(event_id, send=False)))
        summary = result.get("dry_run_summary") or {}
        decision = (result.get("recovery_decision") or {}).get("code")
        target = result.get("target_conversation") or {}
        exact_uuid = target.get("uuid") == self.config.conversation_uuid and summary.get("target_uuid_verified") == self.config.conversation_uuid
        ready = (
            result.get("status") == "DRY_RUN_READ_ONLY_SUCCESS"
            and exact_uuid
            and summary.get("draft_present") is False
            and decision == "NEW_ATTEMPT_ALLOWED"
        )
        return {
            "ready": ready,
            "status": "TRANSITION_PREFLIGHT_READY" if ready else "TRANSITION_PREFLIGHT_BLOCKED",
            "t03_status": result.get("status"),
            "decision": decision,
            "exact_uuid": exact_uuid,
            "draft_present": summary.get("draft_present"),
        }

    def prepare_account_transition(self, event_id: str) -> Dict[str, Any]:
        return self._t03_preflight(event_id)

    def switch_account(self, account: str) -> Dict[str, Any]:
        if not self.config.execute_switch:
            return {"verified": False, "safe_to_retry": False, "status": "EXECUTION_DISABLED", "error_code": "EXECUTION_DISABLED"}
        result = execute_safe_switch(
            account,
            target="agy",
            confirm=True,
            introspect_network=True,
            trusted_identity=self.identity,
            private_diagnostic_mode=False,
        )
        verified = result.get("status") == SwitchOutcome.CREDENTIAL_IDENTITY_VERIFIED.value
        return {
            "verified": verified,
            "safe_to_retry": False,
            "account_ref": result.get("account_ref"),
            "status": result.get("status"),
            "error_code": result.get("error_code") or result.get("status"),
        }

    def verify_credential_adoption(self, expected_account_ref: str) -> Dict[str, Any]:
        result = verify_active_account(expected_account=None, introspect_network=True)
        detected = pseudonymize_account(result.raw_detected_email) if result.raw_detected_email else None
        verified = (
            result.status == CredentialVerificationStatus.CREDENTIAL_STORE_IDENTITY_VERIFIED
            and detected == expected_account_ref
        )
        return {
            "verified": verified,
            "status": "CREDENTIAL_TARGET_VERIFIED" if verified else result.status.value,
            "detected_account_ref": detected,
            "error_code": result.error_code,
        }

    def desktop_adoption_verifier_available(self) -> bool:
        capability = self.desktop_probe.capability_status(self._last_language_server_pid)
        return bool(capability.get("available"))

    def probe_desktop_adoption(self, expected_account_ref: str) -> Dict[str, Any]:
        result = self.desktop_probe.probe_identity(expected_account_ref, self._last_language_server_pid)
        pid = result.get("language_server_pid")
        if isinstance(pid, int) and pid > 0:
            self._last_language_server_pid = pid
        return result

    def restart_desktop_for_adoption(self, expected_account_ref: str, old_ls_pid: int) -> Dict[str, Any]:
        result = self.desktop_probe.restart_and_verify(expected_account_ref, old_ls_pid)
        pid = result.get("language_server_pid")
        if result.get("verified") and isinstance(pid, int) and pid > 0:
            self._last_language_server_pid = pid
        return result

    def reconcile_desktop_after_restart(self, expected_account_ref: str, old_ls_pid: int) -> Dict[str, Any]:
        deadline = time.time() + 20.0
        last_status = "DESKTOP_RESTART_RECONCILIATION_TIMEOUT"
        while time.time() < deadline:
            snapshot, status = self.desktop_probe.inspect(None)
            if snapshot is None:
                last_status = status
                time.sleep(0.4)
                continue
            if snapshot.language_server_pid == old_ls_pid:
                last_status = "DESKTOP_OLD_LANGUAGE_SERVER_STILL_ACTIVE"
                time.sleep(0.4)
                continue
            result = self.desktop_probe.probe_identity(expected_account_ref, snapshot.language_server_pid)
            if result.get("verified"):
                self._last_language_server_pid = snapshot.language_server_pid
                return result
            last_status = result.get("status") or last_status
            if last_status == "DESKTOP_IDENTITY_MISMATCH":
                return result
            time.sleep(0.4)
        return {"verified": False, "status": last_status}

    def resume_conversation(self, event_id: str) -> Dict[str, Any]:
        preflight = self._t03_preflight(event_id)
        if not preflight.get("ready"):
            return {"status": "POST_TRANSITION_PREFLIGHT_FAILED", "preflight": preflight}
        return asyncio.run(execute_resume_pipeline(self._t03_args(event_id, send=True)))

    def probe_resume_progress(self, event_id: str) -> Dict[str, Any]:
        result = asyncio.run(execute_resume_pipeline(self._t03_args(event_id, send=False)))
        decision = (result.get("recovery_decision") or {}).get("code")
        verified = decision in ("TURN_ALREADY_ACTIVE", "RESUME_ALREADY_OBSERVED")
        return {
            "verified": verified,
            "status": "TURN_PROGRESS_VERIFIED" if verified else "TURN_PROGRESS_NOT_YET_VERIFIED",
            "t03_status": result.get("status"),
            "decision": decision,
        }
