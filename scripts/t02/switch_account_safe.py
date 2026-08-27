#!/usr/bin/env python3
"""
switch_account_safe.py

Hardened, safety-first wrapper around AGM account switching for Switch-Antigravity (Round 7).

Safety Constraints & Outcome Model:
1. Strict Scope: Target restricted exclusively to 'agy' (Credential Store only).
2. Pre-Execution Binary Trust Gate (Critical Item 3):
   - Switch is a credential-mutating operation.
   - Requires TrustedAgmIdentity BEFORE any subprocess execution.
   - Missing / malformed / mismatched hash fails closed with ZERO subprocess calls.
3. Sanitized Error Messages (Item 9 & 10):
   - Default outputs NEVER echo raw invalid inputs, email addresses, command lines, or stdout/stderr.
   - Raw account emails and process diagnostics are strictly restricted to --private-diagnostic-mode.
4. Exit Code Contract:
   - 0: CREDENTIAL_IDENTITY_VERIFIED (Vault written + Google userinfo identity confirmed)
   - 1: FAILURE (Command failed, verify mismatch, invalid input, wildcard rejected, generic failure)
   - 2: SWITCH_WRITTEN_UNVERIFIED (Vault written + identity unverified/offline)
   - 3: DRY_RUN (Simulation mode; no OS changes)
   - 4: TRUST_IDENTITY_FAILURE (Missing/malformed/mismatched/unverified trusted binary SHA)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from enum import Enum
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from refresh_quota_safe import is_canonical_email, pseudonymize_account
from trusted_agm_runner import (
    RunnerErrorCode,
    TrustedAgmIdentity,
    TrustedExecutionResult,
    execute_trusted_agm,
    find_canonical_agm_executable,
)
from verify_active_account import (
    CredentialVerificationStatus,
    VerificationResult,
    verify_active_account,
)


class SwitchOutcome(str, Enum):
    DRY_RUN = "DRY_RUN"
    SWITCH_COMMAND_FAILED = "SWITCH_COMMAND_FAILED"
    SWITCH_WRITTEN_UNVERIFIED = "SWITCH_WRITTEN_UNVERIFIED"
    CREDENTIAL_IDENTITY_VERIFIED = "CREDENTIAL_IDENTITY_VERIFIED"
    VERIFY_FAILED = "VERIFY_FAILED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    WILDCARD_REJECTED = "WILDCARD_REJECTED"
    AGM_NOT_FOUND = "AGM_NOT_FOUND"
    BINARY_IDENTITY_UNCONFIGURED = "BINARY_IDENTITY_UNCONFIGURED"
    BINARY_IDENTITY_CONFIG_INVALID = "BINARY_IDENTITY_CONFIG_INVALID"
    BINARY_IDENTITY_MISMATCH = "BINARY_IDENTITY_MISMATCH"
    BINARY_IDENTITY_UNVERIFIED = "BINARY_IDENTITY_UNVERIFIED"
    BINARY_CHANGED_DURING_EXECUTION = "BINARY_CHANGED_DURING_EXECUTION"


def execute_safe_switch(
    account: str,
    target: str = "agy",
    confirm: bool = False,
    introspect_network: bool = False,
    trusted_identity: Optional[TrustedAgmIdentity] = None,
    private_diagnostic_mode: bool = False,
    agm_runner: Optional[Callable[[list, int], tuple]] = None,
    verifier: Optional[Callable[[Optional[str], bool], VerificationResult]] = None,
    executable_resolver: Optional[Callable[[], Optional[str]]] = None,
    sha_computer: Optional[Callable[[str], Optional[str]]] = None
) -> dict:
    """
    Safely executes an AGM account switch for the 'agy' target.
    Requires TrustedAgmIdentity before executing any switch binary.
    """
    verify_func = verifier or (lambda exp, net: verify_active_account(expected_account=exp, introspect_network=net))
    pseudonymous_ref = pseudonymize_account(account) if is_canonical_email(account) else "acc_invalid"

    # Input sanitization (Item 9: never echo raw invalid inputs)
    if not account or not account.strip():
        return {
            "status": SwitchOutcome.INVALID_ARGUMENT.value,
            "error_code": "EMPTY_ACCOUNT",
            "message": "Account argument cannot be empty.",
            "exit_code": 1,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }

    account_clean = account.strip()
    if account_clean in ("*", "all", "any", "%"):
        return {
            "status": SwitchOutcome.WILDCARD_REJECTED.value,
            "error_code": "WILDCARD_REJECTED",
            "message": "Wildcard target is strictly forbidden in safe switch.",
            "exit_code": 1,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }

    if not is_canonical_email(account_clean):
        return {
            "status": SwitchOutcome.INVALID_ARGUMENT.value,
            "error_code": "NON_CANONICAL_EMAIL",
            "message": "Account input is not a valid canonical email.",
            "exit_code": 1,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }

    if target != "agy":
        return {
            "status": SwitchOutcome.INVALID_ARGUMENT.value,
            "error_code": "UNSUPPORTED_TARGET_SCOPE",
            "message": "Target scope is restricted exclusively to 'agy'.",
            "exit_code": 1,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }

    if not confirm:
        pre_verification = verify_func(None, False)
        out = {
            "status": SwitchOutcome.DRY_RUN.value,
            "account_ref": pseudonymous_ref,
            "target_product": target,
            "message": "Dry-run mode: no changes applied. Pass --confirm to execute switch.",
            "exit_code": 3,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }
        if private_diagnostic_mode:
            out["raw_target_account"] = account_clean
            out["pre_switch_state"] = pre_verification.to_private_diagnostic_dict()
        return out

    # Critical Item 3: Execute switch through TrustedAgmRunner
    exec_res = execute_trusted_agm(
        subcommand_args=["switch", account_clean, "--target", target],
        trusted_identity=trusted_identity,
        timeout_sec=15,
        injected_runner=agm_runner,
        injected_resolver=executable_resolver,
        injected_sha_computer=sha_computer
    )

    if not exec_res.command_executed:
        # Pre-execution gate rejected binary before execution
        if exec_res.error_code == RunnerErrorCode.BINARY_IDENTITY_UNCONFIGURED:
            out_status = SwitchOutcome.BINARY_IDENTITY_UNCONFIGURED
            msg = "Trusted AGM binary identity is unconfigured."
        elif exec_res.error_code == RunnerErrorCode.BINARY_IDENTITY_CONFIG_INVALID:
            out_status = SwitchOutcome.BINARY_IDENTITY_CONFIG_INVALID
            msg = "Trusted AGM binary identity format is invalid."
        elif exec_res.error_code == RunnerErrorCode.BINARY_IDENTITY_MISMATCH:
            out_status = SwitchOutcome.BINARY_IDENTITY_MISMATCH
            msg = "Observed AGM binary hash mismatches expected identity."
        elif exec_res.error_code == RunnerErrorCode.BINARY_IDENTITY_UNVERIFIED:
            out_status = SwitchOutcome.BINARY_IDENTITY_UNVERIFIED
            msg = "Could not verify AGM binary hash."
        elif exec_res.error_code == RunnerErrorCode.AGM_NOT_FOUND:
            out_status = SwitchOutcome.AGM_NOT_FOUND
            msg = "AGM executable was not found."
        else:
            out_status = SwitchOutcome.SWITCH_COMMAND_FAILED
            msg = "AGM pre-execution trust check failed."

        out = {
            "status": out_status.value,
            "error_code": exec_res.error_code.value,
            "account_ref": pseudonymous_ref,
            "message": msg,
            "exit_code": 4 if out_status in (
                SwitchOutcome.BINARY_IDENTITY_UNCONFIGURED,
                SwitchOutcome.BINARY_IDENTITY_CONFIG_INVALID,
                SwitchOutcome.BINARY_IDENTITY_MISMATCH,
                SwitchOutcome.BINARY_IDENTITY_UNVERIFIED
            ) else 1,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }
        if private_diagnostic_mode:
            out["raw_target_account"] = account_clean
            out["details_private"] = exec_res.stderr
        return out

    if exec_res.error_code == RunnerErrorCode.BINARY_CHANGED_DURING_EXECUTION:
        out = {
            "status": SwitchOutcome.BINARY_CHANGED_DURING_EXECUTION.value,
            "error_code": "BINARY_CHANGED_DURING_EXECUTION",
            "account_ref": pseudonymous_ref,
            "message": "AGM binary mutated during execution.",
            "exit_code": 4,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }
        if private_diagnostic_mode:
            out["raw_target_account"] = account_clean
            out["details_private"] = exec_res.stderr
        return out

    if not exec_res.success:
        out = {
            "status": SwitchOutcome.SWITCH_COMMAND_FAILED.value,
            "error_code": "SWITCH_PROCESS_ERROR",
            "account_ref": pseudonymous_ref,
            "agm_exit_code": exec_res.exit_code,
            "message": "AGM switch process returned non-zero exit code.",
            "exit_code": 1,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }
        if private_diagnostic_mode:
            out["raw_target_account"] = account_clean
            out["agm_stdout"] = exec_res.stdout.strip()
            out["agm_stderr"] = exec_res.stderr.strip()
        return out

    post_verification = verify_func(account_clean, introspect_network)

    if post_verification.status == CredentialVerificationStatus.CREDENTIAL_STORE_IDENTITY_VERIFIED:
        outcome = SwitchOutcome.CREDENTIAL_IDENTITY_VERIFIED
        overall_exit = 0
    elif post_verification.status == CredentialVerificationStatus.CREDENTIAL_STORE_WRITTEN_UNVERIFIED:
        outcome = SwitchOutcome.SWITCH_WRITTEN_UNVERIFIED
        overall_exit = 2
    else:
        outcome = SwitchOutcome.VERIFY_FAILED
        overall_exit = 1

    out = {
        "status": outcome.value,
        "account_ref": pseudonymous_ref,
        "target_product": target,
        "agm_command_succeeded": exec_res.success,
        "credential_store_written": post_verification.credential_present,
        "credential_identity_verified": (outcome == SwitchOutcome.CREDENTIAL_IDENTITY_VERIFIED),
        "desktop_adoption_verified": False,
        "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN",
        "scope": "CREDENTIAL_STORE_ONLY",
        "exit_code": overall_exit
    }
    if private_diagnostic_mode:
        out["raw_target_account"] = account_clean
        out["agm_exit_code"] = exec_res.exit_code
        out["agm_stdout"] = exec_res.stdout.strip()
        out["agm_stderr"] = exec_res.stderr.strip()
        out["post_switch_state"] = post_verification.to_private_diagnostic_dict()
    return out


def main():
    parser = argparse.ArgumentParser(description="Safely switch Antigravity account using AGM.")
    parser.add_argument("account", help="Exact canonical email address to switch to")
    parser.add_argument("--target", "-t", default="agy", choices=["agy"], help="Target product surface (restricted to agy)")
    parser.add_argument("--confirm", action="store_true", help="Confirm execution (without this, dry-run only)")
    parser.add_argument("--expected-binary-sha256", help="Mandatory expected AGM binary SHA-256 for switch")
    parser.add_argument("--network", "-n", action="store_true", help="Perform live Google userinfo introspection")
    parser.add_argument("--private-diagnostic-mode", action="store_true", help="Include raw account email and process diagnostics")
    args = parser.parse_args()

    trusted_id = None
    if args.expected_binary_sha256:
        trusted_id = TrustedAgmIdentity(expected_binary_sha256=args.expected_binary_sha256)

    res = execute_safe_switch(
        account=args.account,
        target=args.target,
        confirm=args.confirm,
        trusted_identity=trusted_id,
        introspect_network=args.network,
        private_diagnostic_mode=args.private_diagnostic_mode
    )
    print(json.dumps(res, indent=2))
    sys.exit(res.get("exit_code", 1))


if __name__ == "__main__":
    main()
