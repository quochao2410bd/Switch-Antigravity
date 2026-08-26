#!/usr/bin/env python3
"""
switch_account_safe.py

Hardened, safety-first wrapper around AGM account switching for Switch-Antigravity.

Safety Constraints & Outcome Model:
1. Enforces canonical email only (refuses aliases without '@' domain, empty string, wildcards).
2. Default behavior is dry-run / probe unless --confirm is explicitly passed.
3. Performs pre-switch and post-switch credential store verification.
4. Exit Code Contract:
   - 0: CREDENTIAL_IDENTITY_VERIFIED (Token written and Google OAuth userinfo matched)
   - 1: FAILURE (Command failed, verify mismatch, invalid input, wildcard rejected)
   - 2: SWITCH_WRITTEN_UNVERIFIED (Token written to vault, but network userinfo unverified)
   - 3: DRY_RUN (Simulation mode; no changes applied)
5. Scope is explicitly CREDENTIAL_STORE_ONLY; Desktop adoption remains UNKNOWN in T02.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from enum import Enum
from typing import Optional

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
    experimental_restart_desktop: bool = False
) -> dict:
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

    # Item 6: Enforce canonical email only to prevent false identity mismatch during userinfo comparison
    if not is_canonical_email(account):
        return {
            "status": SwitchOutcome.INVALID_ARGUMENT.value,
            "error_code": "NON_CANONICAL_EMAIL",
            "message": f"Account '{account}' is not a valid canonical email. Aliases must be resolved prior to safe switch.",
            "exit_code": 1,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }

    # Pre-switch verification
    pre_verification = verify_active_account()

    if not confirm:
        return {
            "status": SwitchOutcome.DRY_RUN.value,
            "target_account": account,
            "switch_target": target,
            "pre_switch_state": pre_verification.__dict__,
            "message": "Dry-run mode: no changes applied. Pass --confirm to execute switch.",
            "exit_code": 3,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }

    # Locate AGM binary
    agm_bin = find_agm_executable()
    if not agm_bin:
        return {
            "status": SwitchOutcome.AGM_NOT_FOUND.value,
            "error_code": "AGM_NOT_FOUND",
            "message": "AGM executable not found on PATH or search locations",
            "exit_code": 1,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }

    # Execute AGM switch
    cmd = [agm_bin, "switch", account, "--target", target]
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
        return {
            "status": SwitchOutcome.SWITCH_COMMAND_FAILED.value,
            "target_account": account,
            "agm_exit_code": exit_code,
            "agm_stdout": stdout.strip(),
            "agm_stderr": stderr.strip(),
            "pre_switch_state": pre_verification.__dict__,
            "message": f"AGM switch exited with code {exit_code}",
            "exit_code": 1,
            "scope": "CREDENTIAL_STORE_ONLY",
            "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN"
        }

    # Optional experimental restart (flagged clearly as unsafe / experimental)
    desktop_restarted = False
    if experimental_restart_desktop and exit_code == 0:
        try:
            # WARNING: Experimental force-kill; supervisor must checkpoint first!
            subprocess.run(["taskkill", "/F", "/IM", "Antigravity.exe"], capture_output=True)
            import time
            time.sleep(1)
            exe_path = os.path.expandvars(r"%LOCALAPPDATA%\Programs\antigravity\Antigravity.exe")
            if os.path.isfile(exe_path):
                subprocess.Popen([exe_path], close_fds=True)
                desktop_restarted = True
        except Exception:
            desktop_restarted = False

    # Post-switch independent verification
    post_verification = verify_active_account(
        expected_account=account,
        introspect_network=introspect_network
    )

    # Determine explicit outcome & exit code contract (Item 5)
    if post_verification.status == CredentialVerificationStatus.CREDENTIAL_STORE_IDENTITY_VERIFIED:
        outcome = SwitchOutcome.CREDENTIAL_IDENTITY_VERIFIED
        overall_exit = 0
    elif post_verification.status == CredentialVerificationStatus.CREDENTIAL_STORE_WRITTEN_UNVERIFIED:
        outcome = SwitchOutcome.SWITCH_WRITTEN_UNVERIFIED
        overall_exit = 2  # Explicitly distinct from 0!
    else:
        outcome = SwitchOutcome.VERIFY_FAILED
        overall_exit = 1

    return {
        "status": outcome.value,
        "target_account": account,
        "target_product": target,
        "agm_command_succeeded": (exit_code == 0),
        "credential_store_written": post_verification.credential_present,
        "credential_identity_verified": (outcome == SwitchOutcome.CREDENTIAL_IDENTITY_VERIFIED),
        "desktop_adoption_verified": False,
        "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN",
        "scope": "CREDENTIAL_STORE_ONLY",
        "agm_exit_code": exit_code,
        "agm_stdout": stdout.strip(),
        "agm_stderr": stderr.strip(),
        "experimental_desktop_restarted": desktop_restarted,
        "pre_switch_state": pre_verification.__dict__,
        "post_switch_state": post_verification.__dict__,
        "exit_code": overall_exit
    }


def main():
    parser = argparse.ArgumentParser(description="Safely switch Antigravity account using AGM.")
    parser.add_argument("account", help="Exact canonical email address to switch to")
    parser.add_argument("--target", "-t", default="agy", choices=["agy", "ide", "all"], help="Target product surface")
    parser.add_argument("--confirm", action="store_true", help="Confirm execution (without this, dry-run only)")
    parser.add_argument("--network", "-n", action="store_true", help="Perform live Google userinfo introspection")
    parser.add_argument("--experimental-restart-desktop", action="store_true", help="EXPERIMENTAL: Force restart Antigravity.exe after switch")
    args = parser.parse_args()

    res = execute_safe_switch(
        account=args.account,
        target=args.target,
        confirm=args.confirm,
        introspect_network=args.network,
        experimental_restart_desktop=args.experimental_restart_desktop
    )
    print(json.dumps(res, indent=2))
    sys.exit(res.get("exit_code", 1))


if __name__ == "__main__":
    main()
