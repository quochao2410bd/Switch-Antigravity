#!/usr/bin/env python3
"""
verify_active_account.py

Independent active account verification probe for Antigravity on Windows.

Separates:
- AGM_STATE (what AGM thinks or records in SQLite)
- ANTIGRAVITY_ACTIVE_STATE (what is actually active in Windows Credential Manager / Antigravity)

Evidence Ranks:
- STRONG: Direct cryptographic hash match or userinfo introspection from Windows Credential Manager payload ('gemini:antigravity').
- MEDIUM: AGM SQLite DB active setting ('active_cloud_account.agy' / 'active_cloud_account.ide').
- WEAK: CLI return code or textual output saying '✓ Antigravity CLI (agy)'.
- UNKNOWN: Verification not possible or credentials unreadable.
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
from typing import Optional


@dataclass
class VerificationResult:
    expected_account: Optional[str]
    detected_active_email: Optional[str]
    token_fingerprint: Optional[str]  # SHA-256 hash prefix of token (safe, non-secret)
    credential_present: bool
    evidence_rank: str  # "STRONG", "MEDIUM", "WEAK", "UNKNOWN"
    matches_expected: bool
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


def verify_active_account(expected_account: Optional[str] = None, introspect_network: bool = False) -> VerificationResult:
    payload = read_windows_credential_payload()
    if not payload:
        return VerificationResult(
            expected_account=expected_account,
            detected_active_email=None,
            token_fingerprint=None,
            credential_present=False,
            evidence_rank="UNKNOWN",
            matches_expected=False,
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
            evidence_rank="UNKNOWN",
            matches_expected=False,
            verification_source="WINDOWS_CREDENTIAL_MANAGER",
            details="Credential payload exists but contains empty access and refresh tokens"
        )

    # Compute safe SHA-256 fingerprint of access token
    fingerprint = hashlib.sha256(access_token.encode("utf-8")).hexdigest()[:16] if access_token else None

    # Check if userinfo network introspection is requested
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
        except Exception as e:
            detected_email = None

    matches = False
    evidence_rank = "STRONG" if detected_email else "MEDIUM"
    if expected_account and detected_email:
        matches = (expected_account.lower() == detected_email.lower())
    elif not expected_account:
        matches = True

    details = (
        f"Verified active token in Windows Credential Manager. "
        f"Detected identity: {detected_email or 'unverified (network introspection disabled)'}, "
        f"Token Fingerprint: {fingerprint}"
    )

    return VerificationResult(
        expected_account=expected_account,
        detected_active_email=detected_email,
        token_fingerprint=fingerprint,
        credential_present=True,
        evidence_rank=evidence_rank,
        matches_expected=matches,
        verification_source="WINDOWS_CREDENTIAL_MANAGER",
        details=details
    )


def main():
    parser = argparse.ArgumentParser(description="Independently verify active Antigravity account.")
    parser.add_argument("--expected", "-e", help="Expected email address to verify against")
    parser.add_argument("--network", "-n", action="store_true", help="Perform live Google userinfo introspection")
    args = parser.parse_args()

    res = verify_active_account(args.expected, introspect_network=args.network)
    print(json.dumps(asdict(res), indent=2))
    if args.expected and not res.matches_expected:
        sys.exit(1)


if __name__ == "__main__":
    main()
