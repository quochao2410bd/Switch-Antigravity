#!/usr/bin/env python3
"""
verify_active_account.py

Windows Credential Store verification probe for Antigravity on Windows.

Explicit State Terminology:
- CREDENTIAL_STORE_IDENTITY_VERIFIED: Windows Credential Manager ('gemini:antigravity')
  contains a valid token whose Google OAuth userinfo matches the expected email.
- CREDENTIAL_STORE_WRITTEN_UNVERIFIED: Token present in vault, but network userinfo
  introspection was not performed or failed.
- CREDENTIAL_STORE_EMPTY: No 'gemini:antigravity' target found.
- DESKTOP_ACTIVE_IDENTITY_VERIFIED: UNKNOWN / UNPROVEN in T02 scope (requires
  in-process Desktop turn/session evidence from T03/integration).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Optional


class CredentialVerificationStatus(str, Enum):
    CREDENTIAL_STORE_IDENTITY_VERIFIED = "CREDENTIAL_STORE_IDENTITY_VERIFIED"
    CREDENTIAL_STORE_WRITTEN_UNVERIFIED = "CREDENTIAL_STORE_WRITTEN_UNVERIFIED"
    CREDENTIAL_STORE_EMPTY = "CREDENTIAL_STORE_EMPTY"
    VERIFICATION_FAILED_MISMATCH = "VERIFICATION_FAILED_MISMATCH"
    DESKTOP_ACTIVE_IDENTITY_VERIFIED = "UNKNOWN_DESKTOP_UNPROVEN"


@dataclass
class VerificationResult:
    expected_account: Optional[str]
    detected_active_email: Optional[str]
    token_fingerprint: Optional[str]  # Non-secret SHA-256 prefix
    credential_present: bool
    status: CredentialVerificationStatus
    evidence_rank: str  # "STRONG", "MEDIUM", "WEAK", "UNKNOWN"
    matches_expected: Optional[bool]  # True, False, or None if unverified
    desktop_adoption_status: str  # Always "UNKNOWN" in T02 scope
    verification_source: str
    details: str


def read_windows_credential_payload() -> Optional[dict]:
    """
    Safely reads 'gemini:antigravity' from Windows Credential Manager via PowerShell.
    Returns parsed JSON dict without printing sensitive secrets to stdout.
    """
    ps_script = r'''
$ErrorActionPreference = 'Stop'
$code = @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public class CredR {
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  public struct CREDENTIAL {
    public uint Flags; public uint Type; public string TargetName; public string Comment;
    public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
    public uint CredentialBlobSize; public IntPtr CredentialBlob; public uint Persist;
    public uint AttributeCount; public IntPtr Attributes; public string TargetAlias; public string UserName;
  }
  [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  public static extern bool CredRead(string target, uint type, uint reservedFlag, out IntPtr credentialPtr);
  [DllImport("advapi32.dll", SetLastError = true)]
  public static extern void CredFree(IntPtr buffer);
  public static string Read(string target) {
    IntPtr p;
    if (!CredRead(target, 1, 0, out p)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    try {
      CREDENTIAL c = (CREDENTIAL)Marshal.PtrToStructure(p, typeof(CREDENTIAL));
      byte[] b = new byte[c.CredentialBlobSize];
      Marshal.Copy(c.CredentialBlob, b, 0, (int)c.CredentialBlobSize);
      return Encoding.UTF8.GetString(b);
    } finally { CredFree(p); }
  }
}
'@
Add-Type -TypeDefinition $code -Language CSharp
[CredR]::Read('gemini:antigravity')
'''
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            check=True
        )
        raw = proc.stdout.strip()
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        return None


def verify_active_account(
    expected_account: Optional[str] = None,
    introspect_network: bool = False,
    mock_payload: Optional[dict] = None
) -> VerificationResult:
    """
    Verifies the active token in the Windows credential store.
    If mock_payload is provided, uses that instead of reading host OS vault (safe for testing).
    """
    payload = mock_payload if mock_payload is not None else read_windows_credential_payload()
    if not payload:
        return VerificationResult(
            expected_account=expected_account,
            detected_active_email=None,
            token_fingerprint=None,
            credential_present=False,
            status=CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY,
            evidence_rank="UNKNOWN",
            matches_expected=False if expected_account else None,
            desktop_adoption_status="UNKNOWN",
            verification_source="WINDOWS_CREDENTIAL_MANAGER",
            details="No 'gemini:antigravity' target found in Windows Credential Manager"
        )

    token_dict = payload.get("token", {})
    access_token = token_dict.get("access_token", "")
    refresh_token = token_dict.get("refresh_token", "")

    if not access_token and not refresh_token:
        return VerificationResult(
            expected_account=expected_account,
            detected_active_email=None,
            token_fingerprint=None,
            credential_present=True,
            status=CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY,
            evidence_rank="UNKNOWN",
            matches_expected=False if expected_account else None,
            desktop_adoption_status="UNKNOWN",
            verification_source="WINDOWS_CREDENTIAL_MANAGER",
            details="Credential payload exists but contains empty access/refresh tokens"
        )

    fingerprint = hashlib.sha256(access_token.encode("utf-8")).hexdigest()[:16] if access_token else None

    detected_email = None
    if introspect_network and access_token:
        try:
            req = urllib.request.Request(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    info = json.loads(resp.read().decode("utf-8"))
                    detected_email = info.get("email")
        except Exception:
            detected_email = None

    if detected_email:
        if expected_account:
            matches = (detected_email.lower() == expected_account.lower())
            status = (
                CredentialVerificationStatus.CREDENTIAL_STORE_IDENTITY_VERIFIED
                if matches
                else CredentialVerificationStatus.VERIFICATION_FAILED_MISMATCH
            )
        else:
            matches = True
            status = CredentialVerificationStatus.CREDENTIAL_STORE_IDENTITY_VERIFIED
        evidence_rank = "STRONG"
        details = f"Introspected active OAuth identity: {detected_email} (Fingerprint: {fingerprint})"
    else:
        matches = None  # Explicitly UNVERIFIED
        status = CredentialVerificationStatus.CREDENTIAL_STORE_WRITTEN_UNVERIFIED
        evidence_rank = "MEDIUM"
        details = (
            f"Credential present in store with token fingerprint {fingerprint}. "
            f"Identity unverified (network introspection disabled or offline)."
        )

    return VerificationResult(
        expected_account=expected_account,
        detected_active_email=detected_email,
        token_fingerprint=fingerprint,
        credential_present=True,
        status=status,
        evidence_rank=evidence_rank,
        matches_expected=matches,
        desktop_adoption_status="UNKNOWN",
        verification_source="WINDOWS_CREDENTIAL_MANAGER",
        details=details
    )


def main():
    parser = argparse.ArgumentParser(description="Independently verify Windows Credential Manager identity.")
    parser.add_argument("--expected", "-e", help="Expected email address to verify against")
    parser.add_argument("--network", "-n", action="store_true", help="Perform live Google userinfo introspection")
    args = parser.parse_args()

    res = verify_active_account(args.expected, introspect_network=args.network)
    print(json.dumps(asdict(res), indent=2))
    if args.expected and res.matches_expected is False:
        sys.exit(1)


if __name__ == "__main__":
    main()
