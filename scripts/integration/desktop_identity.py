#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

USER_STATUS_PATH = "/exa.language_server_pb.LanguageServerService/GetUserStatus"


def pseudonymize_email(email: Optional[str]) -> str:
    if not email:
        return "acc_none"
    return "acc_" + hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:12]


def _extract_csrf(command_line: str) -> Optional[str]:
    if not command_line:
        return None
    m = re.search(r"--csrf_token(?:=|\s+)([^\s\"']+|\"[^\"]+\"|'[^']+')", command_line)
    if not m:
        return None
    return m.group(1).strip("\"'")


@dataclass
class DesktopProcessSnapshot:
    main_pid: int
    main_executable: str
    language_server_pid: int
    csrf_token: str
    listening_ports: List[int]


class DesktopRuntimeProbe:
    """Read-only identity probe for the running Antigravity Desktop language server.

    A positive adoption result comes from GetUserStatus.email returned by the exact
    running language_server process, not from Credential Manager contents.
    """

    def __init__(
        self,
        process_snapshot_provider: Optional[Callable[[Optional[int]], Tuple[Optional[DesktopProcessSnapshot], str]]] = None,
        user_status_fetcher: Optional[Callable[[DesktopProcessSnapshot], List[Dict[str, Any]]]] = None,
        restart_executor: Optional[Callable[[DesktopProcessSnapshot, float], Tuple[bool, str]]] = None,
        sleep_func: Callable[[float], None] = time.sleep,
    ):
        self._snapshot_provider = process_snapshot_provider or self._powershell_snapshot
        self._user_status_fetcher = user_status_fetcher or self._fetch_user_status_responses
        self._restart_executor = restart_executor or self._restart_windows_desktop
        self._sleep = sleep_func

    def inspect(self, pid_hint: Optional[int] = None) -> Tuple[Optional[DesktopProcessSnapshot], str]:
        return self._snapshot_provider(pid_hint)

    def capability_status(self, pid_hint: Optional[int] = None) -> Dict[str, Any]:
        if os.name != "nt" and self._snapshot_provider == self._powershell_snapshot:
            return {"available": False, "status": "DESKTOP_IDENTITY_WINDOWS_ONLY"}
        snapshot, status = self.inspect(pid_hint)
        return {
            "available": snapshot is not None,
            "status": "DESKTOP_IDENTITY_PROBE_AVAILABLE" if snapshot else status,
            "language_server_pid": snapshot.language_server_pid if snapshot else None,
        }

    def probe_identity(self, expected_account_ref: str, pid_hint: Optional[int] = None) -> Dict[str, Any]:
        snapshot, status = self.inspect(pid_hint)
        if snapshot is None:
            return {"verified": False, "status": status, "detected_account_ref": None, "language_server_pid": None}
        try:
            responses = self._user_status_fetcher(snapshot)
        except Exception:
            return {
                "verified": False,
                "status": "DESKTOP_GET_USER_STATUS_FAILED",
                "detected_account_ref": None,
                "language_server_pid": snapshot.language_server_pid,
            }
        refs = set()
        valid_response_count = 0
        for response in responses:
            if not isinstance(response, dict):
                continue
            user_status = response.get("userStatus")
            if not isinstance(user_status, dict):
                continue
            email = user_status.get("email")
            if not isinstance(email, str) or "@" not in email:
                continue
            valid_response_count += 1
            refs.add(pseudonymize_email(email))
        if valid_response_count == 0:
            return {
                "verified": False,
                "status": "DESKTOP_IDENTITY_EMAIL_MISSING",
                "detected_account_ref": None,
                "language_server_pid": snapshot.language_server_pid,
            }
        if len(refs) != 1:
            return {
                "verified": False,
                "status": "DESKTOP_IDENTITY_AMBIGUOUS",
                "detected_account_ref": None,
                "language_server_pid": snapshot.language_server_pid,
            }
        detected = next(iter(refs))
        return {
            "verified": detected == expected_account_ref,
            "status": "DESKTOP_ADOPTION_VERIFIED" if detected == expected_account_ref else "DESKTOP_IDENTITY_MISMATCH",
            "detected_account_ref": detected,
            "language_server_pid": snapshot.language_server_pid,
            "source": "LANGUAGE_SERVER_GET_USER_STATUS",
        }

    def restart_and_verify(
        self,
        expected_account_ref: str,
        pid_hint: Optional[int] = None,
        restart_timeout_sec: float = 25.0,
        ready_timeout_sec: float = 35.0,
    ) -> Dict[str, Any]:
        before, status = self.inspect(pid_hint)
        if before is None:
            return {"verified": False, "status": status, "restart_performed": False}
        old_ls_pid = before.language_server_pid
        ok, restart_status = self._restart_executor(before, restart_timeout_sec)
        if not ok:
            return {"verified": False, "status": restart_status, "restart_performed": True, "old_language_server_pid": old_ls_pid}
        deadline = time.time() + ready_timeout_sec
        last = {"verified": False, "status": "DESKTOP_RESTART_WAIT_TIMEOUT", "restart_performed": True}
        while time.time() < deadline:
            snapshot, _ = self.inspect(None)
            if snapshot is None or snapshot.language_server_pid == old_ls_pid:
                self._sleep(0.4)
                continue
            probe = self.probe_identity(expected_account_ref, snapshot.language_server_pid)
            probe["restart_performed"] = True
            probe["old_language_server_pid"] = old_ls_pid
            if probe.get("verified"):
                return probe
            last = probe
            if probe.get("status") == "DESKTOP_IDENTITY_MISMATCH":
                return probe
            self._sleep(0.4)
        return last

    def _powershell_snapshot(self, pid_hint: Optional[int]) -> Tuple[Optional[DesktopProcessSnapshot], str]:
        if os.name != "nt":
            return None, "DESKTOP_IDENTITY_WINDOWS_ONLY"
        hint = int(pid_hint or 0)
        ps = rf'''
$ErrorActionPreference='Stop'
$hint={hint}
$all = @(Get-CimInstance Win32_Process)
$ls = @($all | Where-Object {{
    $_.Name -ieq 'language_server.exe' -and
    $_.CommandLine -match '--app_data_dir(?:=|\s+)antigravity' -and
    $_.CommandLine -match '--override_ide_name(?:=|\s+)antigravity' -and
    $_.CommandLine -match '--subclient_type(?:=|\s+)hub'
}})
if ($hint -gt 0) {{
    $hinted = @($ls | Where-Object {{ $_.ProcessId -eq $hint }})
    if ($hinted.Count -eq 1) {{ $ls = $hinted }}
}}
$main = @($all | Where-Object {{
    $_.Name -ieq 'Antigravity.exe' -and
    $_.ExecutablePath -and
    ($_.CommandLine -notmatch '--type=')
}})
$out = @{{
    language_servers = @($ls | ForEach-Object {{
        $ports = @()
        try {{ $ports = @(Get-NetTCPConnection -State Listen -OwningProcess $_.ProcessId -ErrorAction Stop | ForEach-Object {{ [int]$_.LocalPort }} | Sort-Object -Unique) }} catch {{}}
        @{{ pid=[int]$_.ProcessId; command_line=$_.CommandLine; ports=$ports }}
    }})
    mains = @($main | ForEach-Object {{ @{{ pid=[int]$_.ProcessId; executable=$_.ExecutablePath }} }})
}}
$out | ConvertTo-Json -Depth 6 -Compress
'''
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=8
            )
        except Exception:
            return None, "DESKTOP_PROCESS_DISCOVERY_FAILED"
        if proc.returncode != 0 or not proc.stdout.strip():
            return None, "DESKTOP_PROCESS_DISCOVERY_FAILED"
        try:
            data = json.loads(proc.stdout)
        except Exception:
            return None, "DESKTOP_PROCESS_DISCOVERY_INVALID_JSON"
        ls_items = data.get("language_servers") or []
        mains = data.get("mains") or []
        if isinstance(ls_items, dict):
            ls_items = [ls_items]
        if isinstance(mains, dict):
            mains = [mains]
        if len(ls_items) != 1:
            return None, "DESKTOP_LANGUAGE_SERVER_NOT_FOUND" if not ls_items else "DESKTOP_LANGUAGE_SERVER_AMBIGUOUS"
        if len(mains) != 1:
            return None, "DESKTOP_MAIN_PROCESS_NOT_FOUND" if not mains else "DESKTOP_MAIN_PROCESS_AMBIGUOUS"
        ls = ls_items[0]
        main = mains[0]
        token = _extract_csrf(str(ls.get("command_line") or ""))
        ports_raw = ls.get("ports") or []
        if isinstance(ports_raw, int):
            ports_raw = [ports_raw]
        ports = sorted({int(p) for p in ports_raw if isinstance(p, (int, float, str)) and str(p).isdigit() and 0 < int(p) < 65536})
        if not token:
            return None, "DESKTOP_CSRF_TOKEN_NOT_FOUND"
        if not ports:
            return None, "DESKTOP_LANGUAGE_SERVER_PORT_NOT_FOUND"
        exe = str(main.get("executable") or "")
        if not exe or not os.path.isabs(exe):
            return None, "DESKTOP_EXECUTABLE_PATH_UNVERIFIED"
        return DesktopProcessSnapshot(
            main_pid=int(main["pid"]),
            main_executable=exe,
            language_server_pid=int(ls["pid"]),
            csrf_token=token,
            listening_ports=ports,
        ), "OK"

    def _fetch_user_status_responses(self, snapshot: DesktopProcessSnapshot) -> List[Dict[str, Any]]:
        body = json.dumps({
            "metadata": {
                "ideName": "antigravity",
                "extensionName": "antigravity",
                "ideVersion": "unknown",
                "locale": "en",
            }
        }).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "X-Codeium-Csrf-Token": snapshot.csrf_token,
        }
        out: List[Dict[str, Any]] = []
        for port in snapshot.listening_ports:
            for scheme in ("https", "http"):
                url = f"{scheme}://127.0.0.1:{port}{USER_STATUS_PATH}"
                req = urllib.request.Request(url, data=body, headers=headers, method="POST")
                try:
                    if scheme == "https":
                        ctx = ssl._create_unverified_context()
                        resp = urllib.request.urlopen(req, timeout=2.5, context=ctx)
                    else:
                        resp = urllib.request.urlopen(req, timeout=2.5)
                    with resp:
                        if getattr(resp, "status", 200) != 200:
                            continue
                        parsed = json.loads(resp.read().decode("utf-8", "replace"))
                        if isinstance(parsed, dict):
                            out.append(parsed)
                            break
                except Exception:
                    continue
        return out

    def _restart_windows_desktop(self, snapshot: DesktopProcessSnapshot, timeout_sec: float) -> Tuple[bool, str]:
        if os.name != "nt":
            return False, "DESKTOP_RESTART_WINDOWS_ONLY"
        if not os.path.isfile(snapshot.main_executable):
            return False, "DESKTOP_EXECUTABLE_NOT_FOUND"
        try:
            subprocess.run(
                ["taskkill", "/PID", str(snapshot.main_pid), "/T"],
                capture_output=True, text=True, timeout=8
            )
            deadline = time.time() + max(2.0, timeout_sec / 2)
            while time.time() < deadline:
                probe = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"if (Get-Process -Id {snapshot.main_pid} -ErrorAction SilentlyContinue) {{ exit 1 }} else {{ exit 0 }}"],
                    capture_output=True, text=True, timeout=3
                )
                if probe.returncode == 0:
                    break
                self._sleep(0.3)
            else:
                return False, "DESKTOP_GRACEFUL_CLOSE_TIMEOUT"
            subprocess.Popen(
                [snapshot.main_executable],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            return True, "DESKTOP_RESTART_LAUNCHED"
        except Exception:
            return False, "DESKTOP_RESTART_FAILED"
