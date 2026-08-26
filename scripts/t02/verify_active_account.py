#!/usr/bin/env python3
"""
verify_active_account.py

Windows Credential Store verification probe for Antigravity on Windows.

Explicit State Model & Error Classification:
- CREDENTIAL_STORE_IDENTITY_VERIFIED: Windows Credential Manager ('gemini:antigravity')
  contains a valid token whose Google OAuth userinfo matches the expected canonical email.
- CREDENTIAL_STORE_WRITTEN_UNVERIFIED: Token present in vault, but network userinfo
  introspection was not performed or offline.
- CREDENTIAL_STORE_EMPTY: No 'gemini:antigravity' target found (Win32 1168).
- CREDENTIAL_STORE_ACCESS_DENIED: Access denied reading OS vault (Win32 5).
- CREDENTIAL_STORE_READ_ERROR: OS-level read error from CredRead.
- CREDENTIAL_STORE_UNAVAILABLE: PowerShell or Credential Manager API unavailable.
- CREDENTIAL_PAYLOAD_INVALID: Malformed JSON or corrupted blob structure.
- IDENTITY_MISMATCH: Introspected OAuth identity does not match expected account.
- TOKEN_REJECTED: Google OAuth returned HTTP 401 / expired token.
- NETWORK_UNAVAILABLE: Introspection failed due to timeout or network error.
- USERINFO_INVALID_RESPONSE: Malformed JSON or missing email in userinfo response.
- DESKTOP_ACTIVE_IDENTITY_VERIFIED: Strictly UNKNOWN_DESKTOP_UNPROVEN in T02 scope.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Callable, Optional, Tuple


class CredentialVerificationStatus(str, Enum):
    CREDENTIAL_STORE_IDENTITY_VERIFIED = "CREDENTIAL_STORE_IDENTITY_VERIFIED"
    CREDENTIAL_STORE_WRITTEN_UNVERIFIED = "CREDENTIAL_STORE_WRITTEN_UNVERIFIED"
    CREDENTIAL_STORE_EMPTY = "CREDENTIAL_STORE_EMPTY"
    CREDENTIAL_STORE_ACCESS_DENIED = "CREDENTIAL_STORE_ACCESS_DENIED"
    CREDENTIAL_STORE_READ_ERROR = "CREDENTIAL_STORE_READ_ERROR"
    CREDENTIAL_STORE_UNAVAILABLE = "CREDENTIAL_STORE_UNAVAILABLE"
    CREDENTIAL_PAYLOAD_INVALID = "CREDENTIAL_PAYLOAD_INVALID"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    TOKEN_REJECTED = "TOKEN_REJECTED"
    NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
    USERINFO_INVALID_RESPONSE = "USERINFO_INVALID_RESPONSE"
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
    scope: str  # "CREDENTIAL_STORE_ONLY"
    desktop_adoption_status: str  # Always "UNKNOWN" in T02 scope
    verification_source: str
    details: str


def read_windows_credential_payload() -> Tuple[Optional[dict], Optional[CredentialVerificationStatus], Optional[str]]:
    """
    Safely reads 'gemini:antigravity' from Windows Credential Manager via PowerShell.
    Returns (parsed_dict, error_status, error_details).
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
  public static int Read(string target, out string blob) {
    IntPtr p;
    blob = null;
    if (!CredRead(target, 1, 0, out p)) return Marshal.GetLastWin32Error();
    try {
      CREDENTIAL c = (CREDENTIAL)Marshal.PtrToStructure(p, typeof(CREDENTIAL));
      byte[] b = new byte[c.CredentialBlobSize];
      Marshal.Copy(c.CredentialBlob, b, 0, (int)c.CredentialBlobSize);
      blob = Encoding.UTF8.GetString(b);
      return 0;
    } finally { CredFree(p); }
  }
}
'@
Add-Type -TypeDefinition $code -Language CSharp
$blob = ""
$code = [CredR]::Read('gemini:antigravity', [ref]$blob)
if ($code -eq 1168) { Write-Output "ERR_NOT_FOUND"; exit 0 }
if ($code -eq 5) { Write-Output "ERR_ACCESS_DENIED"; exit 0 }
if ($code -ne 0) { Write-Output "ERR_WIN32_$code"; exit 0 }
Write-Output $blob
'''
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=10
        )
        raw = proc.stdout.strip()
        if not raw:
            return None, CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY, "Empty output from credential reader"
        if raw == "ERR_NOT_FOUND":
            return None, CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY, "Target 'gemini:antigravity' not found in vault (Win32 1168)"
        if raw == "ERR_ACCESS_DENIED":
            return None, CredentialVerificationStatus.CREDENTIAL_STORE_ACCESS_DENIED, "Access denied reading 'gemini:antigravity' (Win32 5)"
        if raw.startswith("ERR_WIN32_"):
            return None, CredentialVerificationStatus.CREDENTIAL_STORE_READ_ERROR, f"Win32 error reading vault: {raw}"

        data = json.loads(raw)
        return data, None, None
    except subprocess.TimeoutExpired:
        return None, CredentialVerificationStatus.CREDENTIAL_STORE_UNAVAILABLE, "PowerShell credential query timed out"
    except json.JSONDecodeError as e:
        return None, CredentialVerificationStatus.CREDENTIAL_PAYLOAD_INVALID, f"Corrupted JSON in credential blob: {e}"
    except Exception as e:
        return None, CredentialVerificationStatus.CREDENTIAL_STORE_UNAVAILABLE, f"Subprocess / PowerShell unavailable: {e}"


def default_google_userinfo_fetcher(access_token: str, timeout_sec: int = 10) -> Tuple[Optional[dict], Optional[CredentialVerificationStatus], Optional[str]]:
    """Default live HTTP userinfo lookup against Google OAuth endpoint."""
    try:
        req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data, None, None
            return None, CredentialVerificationStatus.USERINFO_INVALID_RESPONSE, f"Unexpected HTTP status {resp.status}"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, CredentialVerificationStatus.TOKEN_REJECTED, f"OAuth token rejected by Google API (HTTP {e.code})"
        return None, CredentialVerificationStatus.USERINFO_INVALID_RESPONSE, f"Google API HTTP error: {e.code}"
    except urllib.error.URLError as e:
        return None, CredentialVerificationStatus.NETWORK_UNAVAILABLE, f"Network error during userinfo lookup: {e.reason}"
    except json.JSONDecodeError:
        return None, CredentialVerificationStatus.USERINFO_INVALID_RESPONSE, "Malformed JSON received from userinfo endpoint"
    except Exception as e:
        return None, CredentialVerificationStatus.NETWORK_UNAVAILABLE, f"Userinfo network lookup error: {e}"


def verify_active_account(
    expected_account: Optional[str] = None,
    introspect_network: bool = False,
    mock_payload: Optional[dict] = None,
    mock_error_status: Optional[CredentialVerificationStatus] = None,
    userinfo_fetcher: Optional[Callable[[str], Tuple[Optional[dict], Optional[CredentialVerificationStatus], Optional[str]]]] = None
) -> VerificationResult:
    """
    Verifies the active token in the Windows credential store.
    Dependency injection for mock_payload and userinfo_fetcher enables thorough unit testing without OS/network side-effects.
    """
    if mock_error_status is not None:
        return VerificationResult(
            expected_account=expected_account,
            detected_active_email=None,
            token_fingerprint=None,
            credential_present=False,
            status=mock_error_status,
            evidence_rank="UNKNOWN",
            matches_expected=False if expected_account else None,
            scope="CREDENTIAL_STORE_ONLY",
            desktop_adoption_status="UNKNOWN",
            verification_source="WINDOWS_CREDENTIAL_MANAGER",
            details=f"Credential store error: {mock_error_status.value}"
        )

    if mock_payload is not None:
        payload = mock_payload if mock_payload else None
        read_err_status = CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY if not mock_payload else None
        read_err_details = "Mock empty vault" if not mock_payload else None
    else:
        payload, read_err_status, read_err_details = read_windows_credential_payload()

    if read_err_status:
        return VerificationResult(
            expected_account=expected_account,
            detected_active_email=None,
            token_fingerprint=None,
            credential_present=False,
            status=read_err_status,
            evidence_rank="UNKNOWN",
            matches_expected=False if expected_account else None,
            scope="CREDENTIAL_STORE_ONLY",
            desktop_adoption_status="UNKNOWN",
            verification_source="WINDOWS_CREDENTIAL_MANAGER",
            details=read_err_details or "Error reading Windows Credential Manager"
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
            scope="CREDENTIAL_STORE_ONLY",
            desktop_adoption_status="UNKNOWN",
            verification_source="WINDOWS_CREDENTIAL_MANAGER",
            details="Credential payload exists but contains empty access/refresh tokens"
        )

    fingerprint = hashlib.sha256(access_token.encode("utf-8")).hexdigest()[:16] if access_token else None

    if not introspect_network:
        return VerificationResult(
            expected_account=expected_account,
            detected_active_email=None,
            token_fingerprint=fingerprint,
            credential_present=True,
            status=CredentialVerificationStatus.CREDENTIAL_STORE_WRITTEN_UNVERIFIED,
            evidence_rank="MEDIUM",
            matches_expected=None,
            scope="CREDENTIAL_STORE_ONLY",
            desktop_adoption_status="UNKNOWN",
            verification_source="WINDOWS_CREDENTIAL_MANAGER",
            details=f"Credential present in store with token fingerprint {fingerprint}. Identity unverified (network introspection disabled)."
        )

    fetcher = userinfo_fetcher or default_google_userinfo_fetcher
    userinfo_data, net_err_status, net_err_details = fetcher(access_token)

    if net_err_status:
        return VerificationResult(
            expected_account=expected_account,
            detected_active_email=None,
            token_fingerprint=fingerprint,
            credential_present=True,
            status=net_err_status,
            evidence_rank="WEAK",
            matches_expected=None,
            scope="CREDENTIAL_STORE_ONLY",
            desktop_adoption_status="UNKNOWN",
            verification_source="GOOGLE_USERINFO_ENDPOINT",
            details=net_err_details or f"Network userinfo introspection failed ({net_err_status.value})"
        )

    detected_email = userinfo_data.get("email") if userinfo_data else None
    if not detected_email:
        return VerificationResult(
            expected_account=expected_account,
            detected_active_email=None,
            token_fingerprint=fingerprint,
            credential_present=True,
            status=CredentialVerificationStatus.USERINFO_INVALID_RESPONSE,
            evidence_rank="WEAK",
            matches_expected=None,
            scope="CREDENTIAL_STORE_ONLY",
            desktop_adoption_status="UNKNOWN",
            verification_source="GOOGLE_USERINFO_ENDPOINT",
            details="Userinfo response omitted 'email' field"
        )

    if expected_account:
        matches = (detected_email.lower() == expected_account.lower())
        status = (
            CredentialVerificationStatus.CREDENTIAL_STORE_IDENTITY_VERIFIED
            if matches
            else CredentialVerificationStatus.IDENTITY_MISMATCH
        )
    else:
        matches = True
        status = CredentialVerificationStatus.CREDENTIAL_STORE_IDENTITY_VERIFIED

    return VerificationResult(
        expected_account=expected_account,
        detected_active_email=detected_email,
        token_fingerprint=fingerprint,
        credential_present=True,
        status=status,
        evidence_rank="STRONG",
        matches_expected=matches,
        scope="CREDENTIAL_STORE_ONLY",
        desktop_adoption_status="UNKNOWN",
        verification_source="GOOGLE_USERINFO_ENDPOINT",
        details=f"Introspected active OAuth identity: {detected_email} (Fingerprint: {fingerprint})"
    )


def main():
    parser = argparse.ArgumentParser(description="Independently verify Windows Credential Manager identity.")
    parser.add_argument("--expected", "-e", help="Expected canonical email address to verify against")
    parser.add_argument("--network", "-n", action="store_true", help="Perform live Google userinfo introspection")
    args = parser.parse_args()

    res = verify_active_account(args.expected, introspect_network=args.network)
    print(json.dumps(asdict(res), indent=2))
    if res.status == CredentialVerificationStatus.CREDENTIAL_STORE_IDENTITY_VERIFIED:
        sys.exit(0)
    elif res.status == CredentialVerificationStatus.CREDENTIAL_STORE_WRITTEN_UNVERIFIED:
        sys.exit(2)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
