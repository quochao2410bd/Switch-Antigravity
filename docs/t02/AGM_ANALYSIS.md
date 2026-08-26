# AGM Architecture, Quota Detection, Account Switching, and Verification Report (Zero-Trust Round 3 Revision)

**Author / Owner:** T02  
**Assigned Issue:** #2 — AGM quota detection, account switching, verification and security  
**Branch:** `research/T02-account-quota`  

---

## 1. Central Claim & Evidence Matrix (Round 3)

| Claim | Evidence Class | Source / Test Code | Raw Sanitized Artifact | Repro Command | Current Status | Remaining Gap |
|---|---|---|---|---|---|---|
| **1. Quota Freshness & Provenance** | `VERIFIED_SOURCE` & `SYNTHETIC_SIMULATION` | `internal/api/api.go:362`, `scripts/t02/refresh_quota_safe.py`, `scripts/t02/inspect_quota.py` | `tests/fixtures/t02/list_normal.txt` | `python scripts/t02/validate_agm_output.py` | **PROVEN**: Quotas in SQLite are static snapshots. Freshness requires validated `RefreshEvidence` with `origin == LIVE_REFRESH_EXECUTION`, `result == REFRESH_SUCCEEDED`, and `exit_code == 0`. Unvalidated JSON/synthetic records fail closed to `STALE_CACHED`. | None for provenance contract. Live multi-account refresh under Google rate limits requires live staging testbed. |
| **2. `agy` Target Credential Write** | `VERIFIED_SOURCE` & `OBSERVED_LIVE_RUNTIME` | `internal/credstore/credstore.go:244`, `scripts/t02/verify_active_account.py` | `tests/fixtures/t02/switch_success_agy.txt` | `agm.exe switch <email> --target agy` | **PROVEN**: On Windows, `--target agy` writes to Windows Credential Manager `gemini:antigravity` via `advapi32.dll!CredWrite`. Target is strictly restricted to `agy`. | Note: System-level vault writes require isolation; unit tests use injected mock runners. |
| **3. Desktop Adoption of Switched Account** | `UNKNOWN` | Upstream AGM `internal/target/target.go:13` | N/A | N/A | **UNKNOWN / UNPROVEN**: Upstream source defines `Agy = "agy"` as `Antigravity CLI (agy)`. Credential vault write does not prove Desktop adopted the token. | Requires T03/integration in-process Desktop turn/session evidence. |
| **4. Active-Account Identity Verification** | `VERIFIED_SOURCE` & `SYNTHETIC_SIMULATION` | `scripts/t02/verify_active_account.py` | Unit test group in `validate_agm_output.py` | `python scripts/t02/verify_active_account.py --expected <email> --network` | **PROVEN for Credential Store**: `CredRead` + Google OAuth userinfo introspection verifies vault identity. | **UNKNOWN for Desktop Process**: True in-process session state cannot be verified from vault alone. |
| **5. Restart Requirement** | `UNKNOWN / INFERENCE` | `internal/process/process.go:33` | N/A | N/A | **UNKNOWN / INFERENCE**: `--target agy` performs no process kill/start. Antigravity Desktop holds tokens in memory. | Controlled A -> B runtime adoption test required with T01/T03. |

---

## 2. Refresh Evidence Trust Model & Invariants (Items 1, 2, 3, 4, 5)

### 2.1 Trust Model: Process-Local Typed Evidence + HMAC Session Signing
- **Production Mode:** Accepts typed `RefreshEvidence` generated directly by `execute_safe_refresh()` within the same supervisor process, having `origin == EvidenceTrustOrigin.LIVE_REFRESH_EXECUTION`.
- **Cross-Process / Deserialized Mode:** Requires a per-session secret and HMAC-SHA256 signature verification (`verify_evidence_signature()`). Unsigned or deserialized JSON evidence without matching signature is marked `UNTRUSTED_DESERIALIZED` / `RESEARCH_ONLY` and rejected in supervisor mode.
- **Synthetic Test Isolation:** Mock/synthetic refresh evidence is tagged `EvidenceTrustOrigin.SYNTHETIC_TEST_EVIDENCE` and **can NEVER yield `PROVEN_FRESH` in supervisor production mode** (`allow_synthetic_test_origin=False`).

### 2.2 Complete Refresh Success Invariants Matrix

| Invariant | Requirement | Violation Outcome |
|---|---|---|
| 1. Evidence Origin | `origin == LIVE_REFRESH_EXECUTION` (or `allow_synthetic_test_origin=True` in unit test) | `STALE_CACHED` |
| 2. Result Status | `result == RefreshResult.REFRESH_SUCCEEDED` | `REFRESH_FAILED` |
| 3. Exit Code | `exit_code == 0` | `REFRESH_FAILED` |
| 4. Canonical Account Match | `canonical_account` matches target RFC 5322 email | `STALE_CACHED` |
| 5. Command Exact Binding | `command == "agm refresh <canonical_account>"` | `STALE_CACHED` |
| 6. Trusted Executable | `agm_executable` non-empty and valid executable path | `STALE_CACHED` |
| 7. Supported AGM Version | `agm_version_or_revision` in `SUPPORTED_AGM_VERSIONS` (never `"UNKNOWN_VERSION"`) | `STALE_CACHED` |
| 8. Mandatory Session ID | `supervisor_session_id == expected_session_id` | `STALE_CACHED` |
| 9. Monotonic Timestamps | `started_at_epoch <= completed_at_epoch` | `STALE_CACHED` |
| 10. Clock Skew Ceiling | `completed_at_epoch <= now + allowed_clock_skew_sec` (max 2.0s skew) | `STALE_CACHED` |
| 11. Execution Duration | `0.0 <= (completed_at_epoch - started_at_epoch) <= 60.0s` | `STALE_CACHED` |
| 12. Freshness Window | `(now - completed_at_epoch) <= max_freshness_age_sec` (max 300.0s) | `STALE_CACHED` |

---

## 3. Credential Reader Process Output Classifier Matrix (Items 7, 8, 9)

`parse_credential_process_output()` evaluates process exit code and stderr prior to parsing stdout:

| Exit Code | Output Pattern | Classification | Credential Present | Evidence Rank |
|---|---|---|---|---|
| `!= 0` | `Access is denied` in stderr | `CREDENTIAL_STORE_ACCESS_DENIED` | `False` | `UNKNOWN` |
| `!= 0` | Generic PowerShell failure | `POWERSHELL_PROCESS_FAILED` | `False` | `UNKNOWN` |
| `0` | `ERR_NOT_FOUND` (Win32 1168) | `CREDENTIAL_STORE_EMPTY` | `False` | `UNKNOWN` |
| `0` | `ERR_ACCESS_DENIED` (Win32 5) | `CREDENTIAL_STORE_ACCESS_DENIED` | `False` | `UNKNOWN` |
| `0` | `ERR_WIN32_*` (Win32 != 0) | `CREDENTIAL_STORE_READ_ERROR` | `False` | `UNKNOWN` |
| `0` | Malformed JSON blob | `CREDENTIAL_PAYLOAD_INVALID` | `False` | `UNKNOWN` |
| `0` | Valid JSON, but `access_token` & `refresh_token` empty | `CREDENTIAL_TOKEN_FIELDS_MISSING` | `True` | `UNKNOWN` |
| `0` | Valid JSON with token, offline | `CREDENTIAL_STORE_WRITTEN_UNVERIFIED` | `True` | `MEDIUM` |
| `0` | Valid JSON with token, userinfo match | `CREDENTIAL_STORE_IDENTITY_VERIFIED` | `True` | `STRONG` |
| `0` | Valid JSON with token, userinfo mismatch | `IDENTITY_MISMATCH` | `True` | `STRONG` |

---

## 4. Account Switch Scope & Exit-Code Contract (Items 5, 10, 11, 12, 16)

### 4.1 Target Scope Restriction (Item 11)
- Autonomous safe switch is **restricted exclusively to `--target agy`** (`scope = CREDENTIAL_STORE_ONLY`).
- Targets `ide` and `all` are strictly rejected with `SwitchOutcome.INVALID_ARGUMENT` (`error_code="UNSUPPORTED_TARGET_SCOPE"`).

### 4.2 Desktop Process Non-Interference (Item 12)
- T02 safe wrapper strictly performs credential store operations only. All force-kill and process restart parameters have been completely removed.

### 4.3 Switch Exit Code Table & Test Results (Item 10)

| Exit Code | Status | Verified Test Case | Supervisor Action |
|---|---|---|---|
| **`0`** | `CREDENTIAL_IDENTITY_VERIFIED` | AGM zero + post verifier identity match | Proceed to Desktop-side adoption check (Gate B/T03). |
| **`1`** | `FAILURE` / `VERIFY_FAILED` / `WILDCARD_REJECTED` / `UNSUPPORTED_TARGET_SCOPE` | AGM nonzero, identity mismatch, network timeout, target 'ide' | Abort or select alternative account. |
| **`2`** | `SWITCH_WRITTEN_UNVERIFIED` | AGM zero + post verifier written unverified | Require explicit supervisor override before proceeding. |
| **`3`** | `DRY_RUN` | Simulation probe mode (`confirm=False`) | Inspect pre-switch state safely without changes. |

---

## 5. ModelGroup Fail-Closed Validation (Item 15)

`selection_policy.py` validates requested model groups against the `ModelGroup` enum:
- Supported: `gemini-pro`, `gemini-flash`, `claude`.
- Typos or unsupported strings (e.g. `gemni-pro`, `foo`, `""`) fail closed immediately:
  - `terminal_state = TerminalState.FAILED_SAFE`
  - `decision_reason = "INVALID_MODEL_GROUP: '...' is not a supported ModelGroup enum"`

---

## 6. Zero-Trust Architectural Guarantees

1. **CAN ARBITRARY JSON REFRESHEVIDENCE MAKE AN ACCOUNT PROVEN_FRESH?**
   **`NO`** (Unsigned JSON evidence is marked `UNTRUSTED_DESERIALIZED` / `RESEARCH_ONLY` and fails closed in supervisor mode).
2. **CAN SYNTHETIC REFRESH_SUCCEEDED EVIDENCE MAKE A PRODUCTION ACCOUNT ELIGIBLE?**
   **`NO`** (`SYNTHETIC_TEST_EVIDENCE` origin is rejected in production supervisor mode).
3. **CAN T02 SAFE WRAPPER FORCE-RESTART ANTIGRAVITY DESKTOP?**
   **`NO`** (Process management removed from T02; restricted exclusively to credential store operations).
4. **IS LIVE_DESKTOP_A_TO_B_ADOPTION VERIFIED?**
   **`NO`** (Status remains `UNKNOWN` / `UNPROVEN` in T02 scope).
