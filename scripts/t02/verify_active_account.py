#!/usr/bin/env python3
"""
verify_active_account.py

Windows Credential Store verification probe for Antigravity on Windows (Round 7 Architecture).

Key Guarantees:
1. Structured Credential Envelope:
   - Win32 1168 -> CREDENTIAL_STORE_EMPTY (found=False, credential_present=False).
   - Found but empty/zero-length blob -> CREDENTIAL_TOKEN_FIELDS_MISSING (found=True, credential_present=True).
   - Win32 5 -> CREDENTIAL_STORE_ACCESS_DENIED.
   - Non-zero return code -> POWERSHELL_PROCESS_FAILED.
2. Sanitized Supervisor Output Contract (Item 8 & 11):
   - Default output DTO contains NO raw_expected_account, NO raw_detected_email, and NO token_fingerprint.
   - error_code and safe_summary contain only safe normalized enums (NO free-form stderr/exceptions).
   - Only pseudonymous account_ref is emitted by default.
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from refresh_quota_safe import pseudonymize_account


class CredentialVerificationStatus(str, Enum):
    CREDENTIAL_STORE_IDENTITY_VERIFIED = "CREDENTIAL_STORE_IDENTITY_VERIFIED"
    CREDENTIAL_STORE_WRITTEN_UNVERIFIED = "CREDENTIAL_STORE_WRITTEN_UNVERIFIED"
    CREDENTIAL_STORE_EMPTY = "CREDENTIAL_STORE_EMPTY"
    CREDENTIAL_TOKEN_FIELDS_MISSING = "CREDENTIAL_TOKEN_FIELDS_MISSING"
    CREDENTIAL_STORE_ACCESS_DENIED = "CREDENTIAL_STORE_ACCESS_DENIED"
    CREDENTIAL_STORE_READ_ERROR = "CREDENTIAL_STORE_READ_ERROR"
    POWERSHELL_PROCESS_FAILED = "POWERSHELL_PROCESS_FAILED"
    CREDENTIAL_STORE_UNAVAILABLE = "CREDENTIAL_STORE_UNAVAILABLE"
    CREDENTIAL_PAYLOAD_INVALID = "CREDENTIAL_PAYLOAD_INVALID"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    TOKEN_REJECTED = "TOKEN_REJECTED"
    NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
    USERINFO_INVALID_RESPONSE = "USERINFO_INVALID_RESPONSE"
    DESKTOP_ACTIVE_IDENTITY_VERIFIED = "UNKNOWN_DESKTOP_UNPROVEN"


@dataclass
class SanitizedVerificationOutput:
    """Safe supervisor DTO containing zero raw emails, fingerprints, or free-text stderr (Item 8 & 11)."""
    account_ref: Optional[str]
    status: str
    credential_present: bool
    evidence_rank: str
    matches_expected: Optional[bool]
    scope: str
    desktop_adoption_status: str
    verification_source: str
    error_code: Optional[str] = None
    safe_summary: str = "STATUS_NORMAL"


@dataclass
class VerificationResult:
    account_ref: Optional[str]  # Pseudonymous hash by default
    status: CredentialVerificationStatus
    credential_present: bool
    evidence_rank: str
    matches_expected: Optional[bool]
    scope: str
    desktop_adoption_status: str
    verification_source: str
    error_code: Optional[str] = None
    safe_summary: str = "STATUS_NORMAL"
    # Diagnostic private fields (omitted in default output)
    details_private: Optional[str] = None
    raw_expected_account: Optional[str] = None
    raw_detected_email: Optional[str] = None
    token_fingerprint: Optional[str] = None

    def to_sanitized_dto(self) -> SanitizedVerificationOutput:
        """Converts to safe supervisor DTO guaranteed free of raw emails/tokens."""
        return SanitizedVerificationOutput(
            account_ref=self.account_ref,
            status=self.status.value,
            credential_present=self.credential_present,
            evidence_rank=self.evidence_rank,
            matches_expected=self.matches_expected,
            scope=self.scope,
            desktop_adoption_status=self.desktop_adoption_status,
            verification_source=self.verification_source,
            error_code=self.error_code,
            safe_summary=self.safe_summary
        )

    def to_private_diagnostic_dict(self) -> dict:
        """Explicit diagnostic extraction including raw values."""
        d = asdict(self.to_sanitized_dto())
        d["details_private"] = self.details_private
        d["raw_expected_account"] = self.raw_expected_account
        d["raw_detected_email"] = self.raw_detected_email
        d["token_fingerprint"] = self.token_fingerprint
        return d


def parse_credential_envelope_output(
    returncode: int,
    stdout: str,
    stderr: str
) -> Tuple[Optional[dict], Optional[CredentialVerificationStatus], Optional[str], Optional[str]]:
    """
    Parses PowerShell output returning a structured envelope.
    Returns: (payload, status, error_code, private_diag)
    """
    if returncode != 0:
        err_msg = stderr.strip() or stdout.strip() or f"Process exited with code {returncode}"
        if "access is denied" in err_msg.lower() or "unauthorized" in err_msg.lower():
            return None, CredentialVerificationStatus.CREDENTIAL_STORE_ACCESS_DENIED, "ACCESS_DENIED", err_msg
        return None, CredentialVerificationStatus.POWERSHELL_PROCESS_FAILED, "PROCESS_ERROR", err_msg

    raw = stdout.strip()
    if not raw:
        return None, CredentialVerificationStatus.POWERSHELL_PROCESS_FAILED, "EMPTY_STDOUT", "Empty stdout from credential reader subprocess"

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, CredentialVerificationStatus.CREDENTIAL_PAYLOAD_INVALID, "JSON_DECODE_ERROR", f"Malformed envelope JSON: {e}"

    found = envelope.get("found", False)
    win32_code = envelope.get("win32_code", 0)
    blob_len = envelope.get("blob_length", 0)
    blob_utf8 = envelope.get("blob_utf8", "")

    if not found:
        if win32_code == 1168:
            return None, CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY, "WIN32_1168_NOT_FOUND", "Target 'gemini:antigravity' not found in vault (Win32 1168)"
        elif win32_code == 5:
            return None, CredentialVerificationStatus.CREDENTIAL_STORE_ACCESS_DENIED, "WIN32_5_ACCESS_DENIED", "Access denied reading 'gemini:antigravity' (Win32 5)"
        else:
            return None, CredentialVerificationStatus.CREDENTIAL_STORE_READ_ERROR, f"WIN32_{win32_code}", f"Win32 error reading vault: {win32_code}"

    if blob_len == 0 or not blob_utf8.strip():
        return {}, CredentialVerificationStatus.CREDENTIAL_TOKEN_FIELDS_MISSING, "ZERO_LENGTH_BLOB", "Vault target exists but contains zero-length blob"

    try:
        data = json.loads(blob_utf8)
        return data, None, None, None
    except json.JSONDecodeError as e:
        return None, CredentialVerificationStatus.CREDENTIAL_PAYLOAD_INVALID, "CORRUPT_PAYLOAD_JSON", f"Corrupted JSON in credential blob: {e}"


def read_windows_credential_payload(
    runner: Optional[Callable[[], Tuple[int, str, str]]] = None
) -> Tuple[Optional[dict], Optional[CredentialVerificationStatus], Optional[str], Optional[str]]:
    """
    Reads 'gemini:antigravity' via PowerShell emitting structured envelope.
    """
    if runner:
        try:
            ret, out, err = runner()
            return parse_credential_envelope_output(ret, out, err)
        except Exception as e:
            return None, CredentialVerificationStatus.CREDENTIAL_STORE_UNAVAILABLE, "RUNNER_EXCEPTION", f"Injected runner error: {e}"

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
  public static int Read(string target, out string blob, out int blobSize) {
    IntPtr p;
    blob = "";
    blobSize = 0;
    if (!CredRead(target, 1, 0, out p)) return Marshal.GetLastWin32Error();
    try {
      CREDENTIAL c = (CREDENTIAL)Marshal.PtrToStructure(p, typeof(CREDENTIAL));
      blobSize = (int)c.CredentialBlobSize;
      if (blobSize > 0) {
        byte[] b = new byte[blobSize];
        Marshal.Copy(c.CredentialBlob, b, 0, blobSize);
        blob = Encoding.UTF8.GetString(b);
      }
      return 0;
    } finally { CredFree(p); }
  }
}
'@
Add-Type -TypeDefinition $code -Language CSharp
$blob = ""
$size = 0
$code = [CredR]::Read('gemini:antigravity', [ref]$blob, [ref]$size)
$envelope = @{
  found = ($code -eq 0);
  win32_code = $code;
  blob_length = $size;
  blob_utf8 = $blob
}
ConvertTo-Json $envelope -Compress
'''
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=10
        )
        return parse_credential_envelope_output(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return None, CredentialVerificationStatus.CREDENTIAL_STORE_UNAVAILABLE, "QUERY_TIMEOUT", "PowerShell credential query timed out"
    except Exception as e:
        return None, CredentialVerificationStatus.CREDENTIAL_STORE_UNAVAILABLE, "SUBPROCESS_ERROR", f"Subprocess / PowerShell unavailable: {e}"


def default_google_userinfo_fetcher(access_token: str, timeout_sec: int = 10) -> Tuple[Optional[dict], Optional[CredentialVerificationStatus], Optional[str], Optional[str]]:
    """Default live HTTP userinfo lookup against Google OAuth endpoint."""
    try:
        req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data, None, None, None
            return None, CredentialVerificationStatus.USERINFO_INVALID_RESPONSE, f"HTTP_{resp.status}", f"Unexpected HTTP status {resp.status}"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, CredentialVerificationStatus.TOKEN_REJECTED, f"TOKEN_HTTP_{e.code}", f"OAuth token rejected by Google API (HTTP {e.code})"
        return None, CredentialVerificationStatus.USERINFO_INVALID_RESPONSE, f"HTTP_{e.code}", f"Google API HTTP error: {e.code}"
    except urllib.error.URLError as e:
        return None, CredentialVerificationStatus.NETWORK_UNAVAILABLE, "NETWORK_ERROR", f"Network error during userinfo lookup: {e.reason}"
    except json.JSONDecodeError:
        return None, CredentialVerificationStatus.USERINFO_INVALID_RESPONSE, "JSON_DECODE_ERROR", "Malformed JSON received from userinfo endpoint"
    except Exception as e:
        return None, CredentialVerificationStatus.NETWORK_UNAVAILABLE, "LOOKUP_EXCEPTION", f"Userinfo network lookup error: {e}"


def verify_active_account(
    expected_account: Optional[str] = None,
    introspect_network: bool = False,
    mock_payload: Optional[dict] = None,
    ps_runner: Optional[Callable[[], Tuple[int, str, str]]] = None,
    userinfo_fetcher: Optional[Callable[[str], Tuple[Optional[dict], Optional[CredentialVerificationStatus], Optional[str], Optional[str]]]] = None
) -> VerificationResult:
    """
    Verifies the active token in the Windows credential store.
    """
    pseudonymous_ref = pseudonymize_account(expected_account)

    if mock_payload is not None:
        payload = mock_payload if mock_payload else None
        read_err_status = CredentialVerificationStatus.CREDENTIAL_STORE_EMPTY if not mock_payload else None
        read_err_code = "MOCK_EMPTY" if not mock_payload else None
        read_err_details = "Mock empty vault" if not mock_payload else None
    else:
        payload, read_err_status, read_err_code, read_err_details = read_windows_credential_payload(runner=ps_runner)

    if read_err_status:
        return VerificationResult(
            account_ref=pseudonymous_ref,
            status=read_err_status,
            credential_present=(read_err_status == CredentialVerificationStatus.CREDENTIAL_TOKEN_FIELDS_MISSING),
            evidence_rank="UNKNOWN",
            matches_expected=False if expected_account else None,
            scope="CREDENTIAL_STORE_ONLY",
            desktop_adoption_status="UNKNOWN",
            verification_source="WINDOWS_CREDENTIAL_MANAGER",
            error_code=read_err_code or read_err_status.value,
            safe_summary="CREDENTIAL_STORE_QUERY_FAILED",
            details_private=read_err_details or "Error reading Windows Credential Manager",
            raw_expected_account=expected_account
        )

    token_dict = payload.get("token", {})
    access_token = token_dict.get("access_token", "")
    refresh_token = token_dict.get("refresh_token", "")

    if not access_token and not refresh_token:
        return VerificationResult(
            account_ref=pseudonymous_ref,
            status=CredentialVerificationStatus.CREDENTIAL_TOKEN_FIELDS_MISSING,
            credential_present=True,
            evidence_rank="UNKNOWN",
            matches_expected=False if expected_account else None,
            scope="CREDENTIAL_STORE_ONLY",
            desktop_adoption_status="UNKNOWN",
            verification_source="WINDOWS_CREDENTIAL_MANAGER",
            error_code="TOKEN_FIELDS_MISSING",
            safe_summary="PAYLOAD_MISSING_TOKEN_KEYS",
            details_private="Credential payload exists but token fields are missing or empty",
            raw_expected_account=expected_account
        )

    fingerprint = hashlib.sha256(access_token.encode("utf-8")).hexdigest()[:16] if access_token else None

    if not introspect_network:
        return VerificationResult(
            account_ref=pseudonymous_ref,
            status=CredentialVerificationStatus.CREDENTIAL_STORE_WRITTEN_UNVERIFIED,
            credential_present=True,
            evidence_rank="MEDIUM",
            matches_expected=None,
            scope="CREDENTIAL_STORE_ONLY",
            desktop_adoption_status="UNKNOWN",
            verification_source="WINDOWS_CREDENTIAL_MANAGER",
            error_code=None,
            safe_summary="CREDENTIAL_WRITTEN_OFFLINE_UNVERIFIED",
            details_private="Credential present in store. Identity unverified (network introspection disabled).",
            raw_expected_account=expected_account,
            token_fingerprint=fingerprint
        )

    fetcher = userinfo_fetcher or default_google_userinfo_fetcher
    userinfo_data, net_err_status, net_err_code, net_err_details = fetcher(access_token)

    if net_err_status:
        return VerificationResult(
            account_ref=pseudonymous_ref,
            status=net_err_status,
            credential_present=True,
            evidence_rank="WEAK",
            matches_expected=None,
            scope="CREDENTIAL_STORE_ONLY",
            desktop_adoption_status="UNKNOWN",
            verification_source="GOOGLE_USERINFO_ENDPOINT",
            error_code=net_err_code or net_err_status.value,
            safe_summary="USERINFO_LOOKUP_FAILED",
            details_private=net_err_details or f"Network userinfo introspection failed ({net_err_status.value})",
            raw_expected_account=expected_account,
            token_fingerprint=fingerprint
        )

    detected_email = userinfo_data.get("email") if userinfo_data else None
    if not detected_email:
        return VerificationResult(
            account_ref=pseudonymous_ref,
            status=CredentialVerificationStatus.USERINFO_INVALID_RESPONSE,
            credential_present=True,
            evidence_rank="WEAK",
            matches_expected=None,
            scope="CREDENTIAL_STORE_ONLY",
            desktop_adoption_status="UNKNOWN",
            verification_source="GOOGLE_USERINFO_ENDPOINT",
            error_code="USERINFO_EMAIL_MISSING",
            safe_summary="USERINFO_OMITTED_EMAIL",
            details_private="Userinfo response omitted 'email' field",
            raw_expected_account=expected_account,
            token_fingerprint=fingerprint
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
        account_ref=pseudonymous_ref,
        status=status,
        credential_present=True,
        evidence_rank="STRONG",
        matches_expected=matches,
        scope="CREDENTIAL_STORE_ONLY",
        desktop_adoption_status="UNKNOWN",
        verification_source="GOOGLE_USERINFO_ENDPOINT",
        error_code=None if matches else "IDENTITY_MISMATCH",
        safe_summary="IDENTITY_MATCHED_EXPECTED" if matches else "IDENTITY_MISMATCH",
        details_private="Introspected active OAuth identity matched expected account" if matches else "Identity mismatch",
        raw_expected_account=expected_account,
        raw_detected_email=detected_email,
        token_fingerprint=fingerprint
    )


def main():
    parser = argparse.ArgumentParser(description="Independently verify Windows Credential Manager identity.")
    parser.add_argument("--expected", "-e", help="Expected canonical email address to verify against")
    parser.add_argument("--network", "-n", action="store_true", help="Perform live Google userinfo introspection")
    parser.add_argument("--private-diagnostic-mode", action="store_true", help="Include raw email and token fingerprints")
    args = parser.parse_args()

    res = verify_active_account(args.expected, introspect_network=args.network)
    if args.private_diagnostic_mode:
        print(json.dumps(res.to_private_diagnostic_dict(), indent=2))
    else:
        print(json.dumps(asdict(res.to_sanitized_dto()), indent=2))

    if res.status == CredentialVerificationStatus.CREDENTIAL_STORE_IDENTITY_VERIFIED:
        sys.exit(0)
    elif res.status == CredentialVerificationStatus.CREDENTIAL_STORE_WRITTEN_UNVERIFIED:
        sys.exit(2)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
