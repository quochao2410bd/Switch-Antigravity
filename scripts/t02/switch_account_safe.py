#!/usr/bin/env python3
"""
switch_account_safe.py

Hardened, safety-first wrapper around AGM account switching for Switch-Antigravity.

Safety Constraints:
1. Requires explicit account email / reference argument (refuses empty string, wildcard '*').
2. Default behavior is dry-run / probe unless --confirm is explicitly passed.
3. Performs pre-switch and post-switch independent active-account verification.
4. Handles target product selection (defaults to 'agy' on Windows where Antigravity uses Credential Manager).
5. Never loops across accounts automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Add current dir to path to import verify_active_account
sys.path.insert(0, str(Path(__file__).parent))
from verify_active_account import verify_active_account


def find_agm_executable() -> Optional[str]:
    # Check PATH first
    import shutil
    agm_exe = shutil.which("agm")
    if agm_exe:
        return agm_exe

    # Check temp build or typical paths
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


def execute_safe_switch(account: str, target: str = "agy", confirm: bool = False, restart_desktop: bool = False) -> dict:
    if not account or not account.strip():
        return {
            "status": "ERROR",
            "error_code": "INVALID_ARGUMENT",
            "message": "Account argument cannot be empty"
        }

    account = account.strip()
    if account in ("*", "all", "any", "%"):
        return {
            "status": "ERROR",
            "error_code": "WILDCARD_REJECTED",
            "message": f"Wildcard target '{account}' is strictly forbidden in safe switch"
        }

    # 1. Pre-switch verification
    pre_verification = verify_active_account()

    if not confirm:
        return {
            "status": "DRY_RUN",
            "target_account": account,
            "switch_target": target,
            "pre_switch_state": pre_verification.__dict__,
            "message": "Dry-run mode: no changes applied. Pass --confirm to execute switch."
        }

    # 2. Locate AGM binary
    agm_bin = find_agm_executable()
    if not agm_bin:
        return {
            "status": "ERROR",
            "error_code": "AGM_NOT_FOUND",
            "message": "AGM executable not found on PATH or search locations"
        }

    # 3. Execute AGM switch
    cmd = [agm_bin, "switch", account, "--target", target]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        stdout = proc.stdout
        stderr = proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        return {
            "status": "ERROR",
            "error_code": "SWITCH_TIMEOUT",
            "message": "AGM switch command timed out after 15 seconds"
        }
    except Exception as e:
        return {
            "status": "ERROR",
            "error_code": "SWITCH_EXEC_FAILED",
            "message": f"Failed to execute AGM switch: {e}"
        }

    # 4. Optional Antigravity Desktop restart on Windows
    desktop_restarted = False
    if restart_desktop and exit_code == 0:
        try:
            subprocess.run(["taskkill", "/F", "/IM", "Antigravity.exe"], capture_output=True)
            import time
            time.sleep(1)
            exe_path = os.path.expandvars(r"%LOCALAPPDATA%\Programs\antigravity\Antigravity.exe")
            if os.path.isfile(exe_path):
                subprocess.Popen([exe_path], close_fds=True)
                desktop_restarted = True
        except Exception:
            desktop_restarted = False

    # 5. Independent post-switch verification
    post_verification = verify_active_account(expected_account=account)

    success = (exit_code == 0) and post_verification.credential_present

    return {
        "status": "SUCCESS" if success else "FAILED",
        "target_account": account,
        "target_product": target,
        "agm_exit_code": exit_code,
        "agm_stdout": stdout.strip(),
        "agm_stderr": stderr.strip(),
        "desktop_restarted": desktop_restarted,
        "pre_switch_state": pre_verification.__dict__,
        "post_switch_state": post_verification.__dict__,
        "verified_active": post_verification.matches_expected
    }


def main():
    parser = argparse.ArgumentParser(description="Safely switch Antigravity account using AGM.")
    parser.add_argument("account", help="Exact email address or alias to switch to")
    parser.add_argument("--target", "-t", default="agy", choices=["agy", "ide", "all"], help="Target product surface")
    parser.add_argument("--confirm", action="store_true", help="Confirm execution (without this, dry-run only)")
    parser.add_argument("--restart-desktop", action="store_true", help="Restart Antigravity.exe after switch")
    args = parser.parse_args()

    res = execute_safe_switch(
        account=args.account,
        target=args.target,
        confirm=args.confirm,
        restart_desktop=args.restart_desktop
    )
    print(json.dumps(res, indent=2))
    if res.get("status") not in ("SUCCESS", "DRY_RUN"):
        sys.exit(1)


if __name__ == "__main__":
    main()
