#!/usr/bin/env python3
"""
switch_account_safe.py

Hardened, safety-first wrapper around AGM account switching for Switch-Antigravity.

Safety Constraints & Outcome Model:
1. Strict Scope: Target restricted exclusively to 'agy' (Credential Store only).
   'ide' and 'all' targets are strictly rejected.
2. No Desktop Termination: Does NOT force-kill or restart Antigravity.exe (managed by coordinator).
3. Canonical Email Enforcement: Rejects non-canonical aliases.
4. Exit Code Contract:
   - 0: CREDENTIAL_IDENTITY_VERIFIED (Vault written + Google userinfo identity confirmed)
   - 1: FAILURE (Command failed, verify mismatch, invalid input, wildcard rejected)
   - 2: SWITCH_WRITTEN_UNVERIFIED (Vault written + identity unverified/offline)
   - 3: DRY_RUN (Simulation mode; no OS changes)
5. Log Redaction: Clean, safe default output; raw stderr/stdout isolated to diagnostic mode.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from enum import Enum
from typing import Callable, Optional

# Add current dir to path to import verify_active_account and refresh_quota_safe
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from refresh_quota_safe import is_canonical_email
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


def find_agm_executable() -> Optional[str]:
    agm_exe = shutil.which("agm")
    if agm_exe:
        return agm_exe

    candidates = [
        os.path.join(os.environ.get("TEMP", ""), "agm.exe"),
        os.path.expanduser(r"~\.local\bin\agm.exe"),
        os.path.expanduser(r"~\go\bin\agm.exe"),
        os.path.expanduser(r"~\bin\agm.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def execute_safe_switch(
    account: str,
    target: str = "agy",
    confirm: bool = False,
    introspect_network: bool = False,
    diagnostic_mode: bool = False,
    agm_runner: Optional[Callable[[List[str], int], Tuple[int, str, str]]] = None,
    verifier: Optional[Callable[[Optional[str], bool], VerificationResult]] = None,
    executable_resolver: Optional[Callable[[], Optional[str]]] = None
) -> dict:
    """
    Safely executes an AGM account switch for the 'agy' target.
    Uses dependency injection for agm_runner, verifier, and executable_resolver for 100% test isolation.
    """
    verify_func = verifier or (lambda exp, net: verify_active_account(expected_account=exp, introspect_network=net))

    if not account or not account.strip():
        return {
            "status": SwitchOutcome.INVALID_ARGUMENT.value,
            "error_code": "EMPTY_ACCOUNT",
            "message": "Account argument cannot be empty",
            "exit_code": 1,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }

    account = account.strip()
    if account in ("*", "all", "any", "%"):
        return {
            "status": SwitchOutcome.WILDCARD_REJECTED.value,
            "error_code": "WILDCARD_REJECTED",
            "message": f"Wildcard target '{account}' is strictly forbidden in safe switch",
            "exit_code": 1,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }

    # Item 6: Enforce canonical email only
    if not is_canonical_email(account):
        return {
            "status": SwitchOutcome.INVALID_ARGUMENT.value,
            "error_code": "NON_CANONICAL_EMAIL",
            "message": f"Account '{account}' is not a valid canonical email. Aliases must be resolved prior to safe switch.",
            "exit_code": 1,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }

    # Item 11: Narrow target scope strictly to 'agy'
    if target != "agy":
        return {
            "status": SwitchOutcome.INVALID_ARGUMENT.value,
            "error_code": "UNSUPPORTED_TARGET_SCOPE",
            "message": f"Target '{target}' is not supported in T02 scope. Target is restricted exclusively to 'agy'.",
            "exit_code": 1,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }

    if not confirm:
        # Item 14: Use injected verifier or probe safely
        pre_verification = verify_func(None, False)
        out = {
            "status": SwitchOutcome.DRY_RUN.value,
            "target_account": account,
            "target_product": target,
            "message": "Dry-run mode: no changes applied. Pass --confirm to execute switch.",
            "exit_code": 3,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }
        if diagnostic_mode:
            out["pre_switch_state"] = pre_verification.__dict__
        return out

    # Locate AGM binary
    get_bin = executable_resolver or find_agm_executable
    agm_bin = get_bin()
    if not agm_bin:
        return {
            "status": SwitchOutcome.AGM_NOT_FOUND.value,
            "error_code": "AGM_NOT_FOUND",
            "message": "AGM executable not found on PATH or search locations",
            "exit_code": 1,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }

    # Execute AGM switch via injected runner or subprocess
    cmd = [agm_bin, "switch", account, "--target", target]
    if agm_runner is not None:
        try:
            exit_code, stdout, stderr = agm_runner(cmd, 15)
        except Exception as e:
            return {
                "status": SwitchOutcome.SWITCH_COMMAND_FAILED.value,
                "error_code": "RUNNER_EXEC_FAILED",
                "message": f"Injected runner error: {e}",
                "exit_code": 1,
                "scope": "CREDENTIAL_STORE_ONLY",
                "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
            }
    else:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            stdout = proc.stdout
            stderr = proc.stderr
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            return {
                "status": SwitchOutcome.SWITCH_COMMAND_FAILED.value,
                "error_code": "SWITCH_TIMEOUT",
                "message": "AGM switch command timed out after 15 seconds",
                "exit_code": 1,
                "scope": "CREDENTIAL_STORE_ONLY",
                "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
            }
        except Exception as e:
            return {
                "status": SwitchOutcome.SWITCH_COMMAND_FAILED.value,
                "error_code": "SWITCH_EXEC_FAILED",
                "message": f"Failed to execute AGM switch: {e}",
                "exit_code": 1,
                "scope": "CREDENTIAL_STORE_ONLY",
                "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
            }

    if exit_code != 0:
        out = {
            "status": SwitchOutcome.SWITCH_COMMAND_FAILED.value,
            "target_account": account,
            "agm_exit_code": exit_code,
            "message": f"AGM switch exited with code {exit_code}",
            "exit_code": 1,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }
        if diagnostic_mode:
            out["agm_stdout"] = stdout.strip()
            out["agm_stderr"] = stderr.strip()
        return out

    # Post-switch independent verification
    post_verification = verify_func(account, introspect_network)

    # Determine explicit outcome & exit code contract (Item 5)
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
        "target_account": account,
        "target_product": target,
        "agm_command_succeeded": (exit_code == 0),
        "credential_store_written": post_verification.credential_present,
        "credential_identity_verified": (outcome == SwitchOutcome.CREDENTIAL_IDENTITY_VERIFIED),
        "desktop_adoption_verified": False,
        "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN",
        "scope": "CREDENTIAL_STORE_ONLY",
        "exit_code": overall_exit
    }
    if diagnostic_mode:
        out["agm_exit_code"] = exit_code
        out["agm_stdout"] = stdout.strip()
        out["agm_stderr"] = stderr.strip()
        out["post_switch_state"] = post_verification.__dict__
    return out


def main():
    parser = argparse.ArgumentParser(description="Safely switch Antigravity account using AGM.")
    parser.add_argument("account", help="Exact canonical email address to switch to")
    parser.add_argument("--target", "-t", default="agy", choices=["agy"], help="Target product surface (restricted to agy)")
    parser.add_argument("--confirm", action="store_true", help="Confirm execution (without this, dry-run only)")
    parser.add_argument("--network", "-n", action="store_true", help="Perform live Google userinfo introspection")
    parser.add_argument("--diagnostic-mode", action="store_true", help="Include raw process diagnostics in output")
    args = parser.parse_args()

    res = execute_safe_switch(
        account=args.account,
        target=args.target,
        confirm=args.confirm,
        introspect_network=args.network,
        diagnostic_mode=args.diagnostic_mode
    )
    print(json.dumps(res, indent=2))
    sys.exit(res.get("exit_code", 1))


if __name__ == "__main__":
    main()
