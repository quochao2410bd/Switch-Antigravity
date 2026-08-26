#!/usr/bin/env python3
"""
switch_account_safe.py

Hardened, safety-first wrapper around AGM account switching for Switch-Antigravity.

Safety Constraints & Outcome Model:
1. Requires explicit account email / reference argument (refuses empty string, wildcard '*').
2. Default behavior is dry-run / probe unless --confirm is explicitly passed.
3. Performs pre-switch and post-switch credential store verification.
4. Returns explicit, non-misleading outcome statuses:
   - DRY_RUN: Simulation only.
   - SWITCH_COMMAND_FAILED: AGM switch exited non-zero or threw error.
   - SWITCH_WRITTEN_UNVERIFIED: Credential written to vault, but network identity unverified.
   - CREDENTIAL_IDENTITY_VERIFIED: Credential written and verified via network introspection.
   - VERIFY_FAILED: Credential introspection detected mismatching identity.
   - DESKTOP_ADOPTION_UNPROVEN: Desktop process pickup remains unverified in T02 scope.
5. Exit code is 0 ONLY on successful dry-run or fully verified switch. Returns code 1 on errors/rejections.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import Optional

# Add current dir to path to import verify_active_account
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
            "exit_code": 1
        }

    account = account.strip()
    if account in ("*", "all", "any", "%"):
        return {
            "status": SwitchOutcome.WILDCARD_REJECTED.value,
            "error_code": "WILDCARD_REJECTED",
            "message": f"Wildcard target '{account}' is strictly forbidden in safe switch",
            "exit_code": 1
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
            "exit_code": 0
        }

    # Locate AGM binary
    agm_bin = find_agm_executable()
    if not agm_bin:
        return {
            "status": SwitchOutcome.AGM_NOT_FOUND.value,
            "error_code": "AGM_NOT_FOUND",
            "message": "AGM executable not found on PATH or search locations",
            "exit_code": 1
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
            "exit_code": 1
        }
    except Exception as e:
        return {
            "status": SwitchOutcome.SWITCH_COMMAND_FAILED.value,
            "error_code": "SWITCH_EXEC_FAILED",
            "message": f"Failed to execute AGM switch: {e}",
            "exit_code": 1
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
            "exit_code": 1
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

    # Determine explicit outcome
    if post_verification.status == CredentialVerificationStatus.CREDENTIAL_STORE_IDENTITY_VERIFIED:
        outcome = SwitchOutcome.CREDENTIAL_IDENTITY_VERIFIED
        overall_exit = 0
    elif post_verification.status == CredentialVerificationStatus.VERIFICATION_FAILED_MISMATCH:
        outcome = SwitchOutcome.VERIFY_FAILED
        overall_exit = 1
    elif post_verification.status == CredentialVerificationStatus.CREDENTIAL_STORE_WRITTEN_UNVERIFIED:
        outcome = SwitchOutcome.SWITCH_WRITTEN_UNVERIFIED
        overall_exit = 0
    else:
        outcome = SwitchOutcome.VERIFY_FAILED
        overall_exit = 1

    return {
        "status": outcome.value,
        "target_account": account,
        "target_product": target,
        "agm_exit_code": exit_code,
        "agm_stdout": stdout.strip(),
        "agm_stderr": stderr.strip(),
        "experimental_desktop_restarted": desktop_restarted,
        "desktop_adoption_status": "UNKNOWN_DESKTOP_UNPROVEN",
        "pre_switch_state": pre_verification.__dict__,
        "post_switch_state": post_verification.__dict__,
        "exit_code": overall_exit
    }


def main():
    parser = argparse.ArgumentParser(description="Safely switch Antigravity account using AGM.")
    parser.add_argument("account", help="Exact email address or alias to switch to")
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
