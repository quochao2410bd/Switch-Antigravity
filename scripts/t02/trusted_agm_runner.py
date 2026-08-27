#!/usr/bin/env python3
"""
trusted_agm_runner.py

Shared, security-hardened executor for all Antigravity Manager (AGM) CLI surfaces (Round 7 Architecture).

Enforces strict PRE-EXECUTION binary verification:
1. Resolves canonical executable path.
2. Computes observed pre-execution SHA-256.
3. Validates TrustedAgmIdentity configuration exists and has valid 64-hex format.
4. Compares observed_sha == expected_sha BEFORE subprocess execution.
5. If pre-execution validation fails: FAILS CLOSED with ZERO subprocess calls.
6. If pre-execution passes: executes with bounded timeout and verifies post-execution TOCTOU hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Callable, List, Optional, Tuple

INSPECTED_AGM_SOURCE_REVISION = "1d3ce8497e36ffa60c3b4e369168315a7ae4d469"


class RunnerErrorCode(str, Enum):
    NONE = "NONE"
    AGM_NOT_FOUND = "AGM_NOT_FOUND"
    BINARY_IDENTITY_UNCONFIGURED = "BINARY_IDENTITY_UNCONFIGURED"
    BINARY_IDENTITY_CONFIG_INVALID = "BINARY_IDENTITY_CONFIG_INVALID"
    BINARY_IDENTITY_MISMATCH = "BINARY_IDENTITY_MISMATCH"
    BINARY_IDENTITY_UNVERIFIED = "BINARY_IDENTITY_UNVERIFIED"
    CANONICAL_PATH_MISMATCH = "CANONICAL_PATH_MISMATCH"
    BINARY_CHANGED_DURING_EXECUTION = "BINARY_CHANGED_DURING_EXECUTION"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    EXECUTION_FAILED = "EXECUTION_FAILED"


@dataclass
class TrustedAgmIdentity:
    """Trusted administrative identity configuration for AGM executable."""
    expected_binary_sha256: str
    canonical_executable_path: Optional[str] = None
    inspected_source_revision: str = INSPECTED_AGM_SOURCE_REVISION


@dataclass
class TrustedExecutionResult:
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    error_code: RunnerErrorCode
    canonical_executable_path: str
    observed_sha_pre: str
    observed_sha_post: Optional[str]
    started_at_epoch: float
    completed_at_epoch: float
    duration_sec: float
    command_executed: bool  # True ONLY if subprocess was actually invoked

    def to_dict(self) -> dict:
        d = asdict(self)
        d["error_code"] = self.error_code.value
        return d


def compute_file_sha256(filepath: str) -> Optional[str]:
    """Computes SHA-256 hash of a file safely."""
    if not filepath or not os.path.isfile(filepath):
        return None
    try:
        hasher = hashlib.sha256()
        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return None


def find_canonical_agm_executable() -> Optional[str]:
    """Resolves absolute canonical path to AGM binary."""
    agm_exe = shutil.which("agm")
    if agm_exe and os.path.isfile(agm_exe):
        return os.path.abspath(agm_exe)

    candidates = [
        os.path.join(os.environ.get("TEMP", ""), "agm.exe"),
        os.path.expanduser(r"~\.local\bin\agm.exe"),
        os.path.expanduser(r"~\go\bin\agm.exe"),
        os.path.expanduser(r"~\bin\agm.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c)
    return None


def execute_trusted_agm(
    subcommand_args: List[str],
    trusted_identity: Optional[TrustedAgmIdentity],
    timeout_sec: int = 15,
    injected_runner: Optional[Callable[[List[str], int], Tuple[int, str, str]]] = None,
    injected_sha_computer: Optional[Callable[[str], Optional[str]]] = None,
    injected_resolver: Optional[Callable[[], Optional[str]]] = None
) -> TrustedExecutionResult:
    """
    Executes an AGM command through the strict pre-execution trust gate.
    
    GUARANTEE: If trusted_identity is missing, malformed, or mismatches the observed binary,
    ZERO subprocess calls are made.
    """
    start_t = time.time()
    resolve_func = injected_resolver or find_canonical_agm_executable
    sha_func = injected_sha_computer or compute_file_sha256

    agm_bin = resolve_func()
    if not agm_bin:
        end_t = time.time()
        return TrustedExecutionResult(
            success=False,
            exit_code=1,
            stdout="",
            stderr="AGM binary not found on PATH or search locations",
            error_code=RunnerErrorCode.AGM_NOT_FOUND,
            canonical_executable_path="none",
            observed_sha_pre="none",
            observed_sha_post=None,
            started_at_epoch=start_t,
            completed_at_epoch=end_t,
            duration_sec=max(0.0, end_t - start_t),
            command_executed=False
        )

    # 1. Validate Trusted Identity Config is present
    if not trusted_identity or not trusted_identity.expected_binary_sha256 or not trusted_identity.expected_binary_sha256.strip():
        end_t = time.time()
        return TrustedExecutionResult(
            success=False,
            exit_code=1,
            stdout="",
            stderr="BINARY_IDENTITY_UNCONFIGURED: Missing mandatory expected AGM binary SHA-256",
            error_code=RunnerErrorCode.BINARY_IDENTITY_UNCONFIGURED,
            canonical_executable_path=agm_bin,
            observed_sha_pre="none",
            observed_sha_post=None,
            started_at_epoch=start_t,
            completed_at_epoch=end_t,
            duration_sec=max(0.0, end_t - start_t),
            command_executed=False
        )

    expected_sha_clean = trusted_identity.expected_binary_sha256.strip().lower()
    if not re.match(r"^[0-9a-f]{64}$", expected_sha_clean):
        end_t = time.time()
        return TrustedExecutionResult(
            success=False,
            exit_code=1,
            stdout="",
            stderr=f"BINARY_IDENTITY_CONFIG_INVALID: Expected SHA is not 64-hex string",
            error_code=RunnerErrorCode.BINARY_IDENTITY_CONFIG_INVALID,
            canonical_executable_path=agm_bin,
            observed_sha_pre="none",
            observed_sha_post=None,
            started_at_epoch=start_t,
            completed_at_epoch=end_t,
            duration_sec=max(0.0, end_t - start_t),
            command_executed=False
        )

    # 2. Compute Observed Pre-Execution SHA-256
    sha_pre = sha_func(agm_bin)
    if not sha_pre:
        end_t = time.time()
        return TrustedExecutionResult(
            success=False,
            exit_code=1,
            stdout="",
            stderr="Could not compute pre-execution SHA-256 of target binary",
            error_code=RunnerErrorCode.BINARY_IDENTITY_UNVERIFIED,
            canonical_executable_path=agm_bin,
            observed_sha_pre="UNKNOWN_SHA256",
            observed_sha_post=None,
            started_at_epoch=start_t,
            completed_at_epoch=end_t,
            duration_sec=max(0.0, end_t - start_t),
            command_executed=False
        )

    # 3. Compare Observed Pre-Execution SHA against Expected SHA BEFORE Invoking Subprocess
    if sha_pre.lower() != expected_sha_clean:
        end_t = time.time()
        return TrustedExecutionResult(
            success=False,
            exit_code=1,
            stdout="",
            stderr=f"BINARY_IDENTITY_MISMATCH: Observed binary SHA does not match expected identity",
            error_code=RunnerErrorCode.BINARY_IDENTITY_MISMATCH,
            canonical_executable_path=agm_bin,
            observed_sha_pre=sha_pre,
            observed_sha_post=None,
            started_at_epoch=start_t,
            completed_at_epoch=end_t,
            duration_sec=max(0.0, end_t - start_t),
            command_executed=False  # Subprocess NEVER executed!
        )

    # 4. Check Optional Canonical Path Policy Match
    if trusted_identity.canonical_executable_path:
        expected_path_norm = os.path.normcase(os.path.abspath(trusted_identity.canonical_executable_path))
        observed_path_norm = os.path.normcase(os.path.abspath(agm_bin))
        if expected_path_norm != observed_path_norm:
            end_t = time.time()
            return TrustedExecutionResult(
                success=False,
                exit_code=1,
                stdout="",
                stderr=f"CANONICAL_PATH_MISMATCH: Resolved path differs from configured policy",
                error_code=RunnerErrorCode.CANONICAL_PATH_MISMATCH,
                canonical_executable_path=agm_bin,
                observed_sha_pre=sha_pre,
                observed_sha_post=None,
                started_at_epoch=start_t,
                completed_at_epoch=end_t,
                duration_sec=max(0.0, end_t - start_t),
                command_executed=False
            )

    # 5. ALL PRE-EXECUTION CHECKS PASSED -> Invoke Command
    argv = [agm_bin] + subcommand_args
    if injected_runner is not None:
        try:
            exit_code, stdout, stderr = injected_runner(argv, timeout_sec)
            end_t = time.time()
        except Exception as e:
            end_t = time.time()
            return TrustedExecutionResult(
                success=False,
                exit_code=1,
                stdout="",
                stderr=f"Injected runner execution error: {e}",
                error_code=RunnerErrorCode.EXECUTION_FAILED,
                canonical_executable_path=agm_bin,
                observed_sha_pre=sha_pre,
                observed_sha_post=None,
                started_at_epoch=start_t,
                completed_at_epoch=end_t,
                duration_sec=max(0.0, end_t - start_t),
                command_executed=True
            )
    else:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_sec)
            exit_code = proc.returncode
            stdout = proc.stdout
            stderr = proc.stderr
            end_t = time.time()
        except subprocess.TimeoutExpired:
            end_t = time.time()
            return TrustedExecutionResult(
                success=False,
                exit_code=124,
                stdout="",
                stderr=f"AGM command timed out after {timeout_sec}s",
                error_code=RunnerErrorCode.EXECUTION_TIMEOUT,
                canonical_executable_path=agm_bin,
                observed_sha_pre=sha_pre,
                observed_sha_post=None,
                started_at_epoch=start_t,
                completed_at_epoch=end_t,
                duration_sec=max(0.0, end_t - start_t),
                command_executed=True
            )
        except Exception as e:
            end_t = time.time()
            return TrustedExecutionResult(
                success=False,
                exit_code=1,
                stdout="",
                stderr=f"Subprocess execution error: {e}",
                error_code=RunnerErrorCode.EXECUTION_FAILED,
                canonical_executable_path=agm_bin,
                observed_sha_pre=sha_pre,
                observed_sha_post=None,
                started_at_epoch=start_t,
                completed_at_epoch=end_t,
                duration_sec=max(0.0, end_t - start_t),
                command_executed=True
            )

    # 6. Post-Execution TOCTOU Hash Verification
    sha_post = sha_func(agm_bin)
    if not sha_post or sha_post.lower() != sha_pre.lower():
        return TrustedExecutionResult(
            success=False,
            exit_code=1,
            stdout="",
            stderr="BINARY_CHANGED_DURING_EXECUTION: Executable binary mutated during execution",
            error_code=RunnerErrorCode.BINARY_CHANGED_DURING_EXECUTION,
            canonical_executable_path=agm_bin,
            observed_sha_pre=sha_pre,
            observed_sha_post=sha_post or "UNKNOWN",
            started_at_epoch=start_t,
            completed_at_epoch=end_t,
            duration_sec=max(0.0, end_t - start_t),
            command_executed=True
        )

    return TrustedExecutionResult(
        success=(exit_code == 0),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        error_code=RunnerErrorCode.NONE if exit_code == 0 else RunnerErrorCode.EXECUTION_FAILED,
        canonical_executable_path=agm_bin,
        observed_sha_pre=sha_pre,
        observed_sha_post=sha_post,
        started_at_epoch=start_t,
        completed_at_epoch=end_t,
        duration_sec=max(0.0, end_t - start_t),
        command_executed=True
    )
