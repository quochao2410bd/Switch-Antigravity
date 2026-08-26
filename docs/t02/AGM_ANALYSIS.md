# AGM Architecture, Quota Detection, Account Switching, and Verification Report (Zero-Trust Round 2 Revision)

**Author / Owner:** T02  
**Assigned Issue:** #2 — AGM quota detection, account switching, verification and security  
**Branch:** `research/T02-account-quota`  

---

## 1. Central Claim & Evidence Matrix (Round 2)

| Claim | Evidence Class | Source / Test Code | Raw Sanitized Artifact | Repro Command | Current Status | Remaining Gap |
|---|---|---|---|---|---|---|
| **1. Quota Freshness** | `VERIFIED_SOURCE` & `SYNTHETIC_SIMULATION` | `internal/api/api.go:362`, `scripts/t02/inspect_quota.py`, `scripts/t02/refresh_quota_safe.py` | `tests/fixtures/t02/list_normal.txt` | `python scripts/t02/validate_agm_output.py` | **PROVEN**: Quotas in SQLite are static snapshots. Freshness requires validated `RefreshEvidence` with `REFRESH_SUCCEEDED`. Raw timestamps are rejected. | None for contract. Supervisor must execute live refresh before selection. |
| **2. `agy` Target Credential Write** | `VERIFIED_SOURCE` & `OBSERVED_LIVE_RUNTIME` | `internal/credstore/credstore.go:244`, `scripts/t02/verify_active_account.py` | `tests/fixtures/t02/switch_success_agy.txt` | `agm.exe switch <email> --target agy` | **PROVEN**: On Windows, `--target agy` writes to Windows Credential Manager `gemini:antigravity` via `advapi32.dll!CredWrite`. | Note: System-level vault writes require isolation; synthetic tests use mock payloads. |
| **3. Desktop Adoption of Switched Account** | `UNKNOWN` | Upstream AGM `internal/target/target.go:13` | N/A | N/A | **UNKNOWN / UNPROVEN**: Upstream source defines `Agy = "agy"` as `Antigravity CLI (agy)`. Credential vault write does not prove Desktop adopted the token. | Requires T03/integration in-process Desktop turn/session evidence. |
| **4. Active-Account Identity Verification** | `VERIFIED_SOURCE` & `SYNTHETIC_SIMULATION` | `scripts/t02/verify_active_account.py` | Unit test group in `validate_agm_output.py` | `python scripts/t02/verify_active_account.py --expected <email> --network` | **PROVEN for Credential Store**: `CredRead` + Google OAuth userinfo introspection verifies vault identity. | **UNKNOWN for Desktop Process**: True in-process session state cannot be verified from vault alone. |
| **5. Restart Requirement** | `UNKNOWN / INFERENCE` | `internal/process/process.go:33` | N/A | N/A | **UNKNOWN / INFERENCE**: `--target agy` performs no process kill/start. Antigravity Desktop holds tokens in memory. | Controlled A -> B runtime adoption test required with T01/T03. |

---

## 2. Refresh Provenance Contract (Item 1 & 2)

### 2.1 RefreshEvidence Record Schema
Production freshness requires a typed record emitted strictly by an executed refresh operation:

```json
{
  "canonical_account": "alice@example.com",
  "agm_executable": "C:\\Users\\...\\agm.exe",
  "agm_version_or_revision": "agm-1d3ce84",
  "command": "agm refresh alice@example.com",
  "started_at_epoch": 1756220388.0,
  "completed_at_epoch": 1756220390.0,
  "exit_code": 0,
  "result": "REFRESH_SUCCEEDED",
  "supervisor_session_id": "sess-alpha-123",
  "error_summary": null
}
```

### 2.2 Freshness Evaluation Rules
- **`PROVEN_FRESH`**: Produced **ONLY** when `RefreshEvidence.result == REFRESH_SUCCEEDED`, `canonical_account` matches target, `supervisor_session_id` matches current session, and `completed_at_epoch` is within `max_freshness_age_sec` (300s).
- **`STALE_CACHED`**: Assigned when no evidence is provided, evidence is older than 300s, evidence belongs to another account/session, or an unvalidated raw timestamp is passed.
- **`REFRESH_FAILED`**: Assigned when `result` is `REFRESH_FAILED_AUTH`, `REFRESH_FAILED_NETWORK`, `REFRESH_FAILED_ACCOUNT_NOT_FOUND`, or `REFRESH_FAILED_UNKNOWN`.
- **`UNKNOWN_UNFETCHED`**: Assigned when quota values are null/unparseable or table schema is unsupported.

---

## 3. Credential Read Error Classification Matrix (Item 4)

`read_windows_credential_payload()` distinguishes the following states without suppressing errors or leaking secrets:

| Win32 / OS Condition | Classification | Evidence Rank | Recovery Strategy |
|---|---|---|---|
| Target not in vault (`Win32 1168 ERROR_NOT_FOUND`) | `CREDENTIAL_STORE_EMPTY` | `UNKNOWN` | Expected on unauthenticated system; prompt login. |
| Access denied (`Win32 5 ERROR_ACCESS_DENIED`) | `CREDENTIAL_STORE_ACCESS_DENIED` | `UNKNOWN` | `FAIL_SAFE` / `BLOCK` (OS permission fault). |
| General CredRead error (`Win32 != 0`) | `CREDENTIAL_STORE_READ_ERROR` | `UNKNOWN` | `FAIL_SAFE` / `BLOCK`. |
| Subprocess / PowerShell failure / timeout | `CREDENTIAL_STORE_UNAVAILABLE` | `UNKNOWN` | `FAIL_SAFE` / `BLOCK`. |
| Corrupted JSON in CredentialBlob | `CREDENTIAL_PAYLOAD_INVALID` | `UNKNOWN` | `FAIL_SAFE` / `BLOCK`. |
| Token present, network unperformed | `CREDENTIAL_STORE_WRITTEN_UNVERIFIED` | `MEDIUM` | Return exit code `2` (Written unverified). |
| Token present, userinfo matched | `CREDENTIAL_STORE_IDENTITY_VERIFIED` | `STRONG` | Return exit code `0` (Verified success). |
| Token present, userinfo mismatch | `IDENTITY_MISMATCH` | `STRONG` | Return exit code `1` (Verify failed). |
| Token present, Google HTTP 401 | `TOKEN_REJECTED` | `WEAK` | Return exit code `1` (Token expired). |
| Token present, userinfo network timeout | `NETWORK_UNAVAILABLE` | `WEAK` | Return exit code `1` (Transient network). |
| Token present, response missing email | `USERINFO_INVALID_RESPONSE` | `WEAK` | Return exit code `1` (Malformed API response). |

---

## 4. Account Switch Exit-Code Contract & Scope (Item 5 & 7)

`scripts/t02/switch_account_safe.py` operates under strict scope boundaries:

- **Explicit Scope:** `CREDENTIAL_STORE_ONLY`.
- **Desktop Adoption State:** `UNKNOWN_DESKTOP_UNPROVEN`.

### Exit Code Table

| Exit Code | Status | Meaning | Supervisor Action |
|---|---|---|---|
| **`0`** | `CREDENTIAL_IDENTITY_VERIFIED` | Token written to Windows vault AND Google OAuth userinfo introspection matched expected canonical email. | Proceed to Desktop-side adoption check (Gate B/T03). |
| **`1`** | `FAILURE` / `VERIFY_FAILED` | AGM switch command failed, wildcard rejected, invalid alias, or userinfo identity mismatch. | Abort or select alternative account. |
| **`2`** | `SWITCH_WRITTEN_UNVERIFIED` | AGM wrote token to vault, but network userinfo introspection was offline or unperformed. | Require explicit supervisor override before proceeding. |
| **`3`** | `DRY_RUN` | Simulation probe mode; no changes made to OS credential store. | Inspect pre-switch state safely. |

---

## 5. Alias Policy & Canonical Email Enforcement (Item 6)

- **Policy:** Autonomous supervisor operations require **canonical email addresses only** (`user@domain.com`).
- **Rationale:** Comparing arbitrary AGM alias strings (e.g. `prod-worker-2`) directly against Google OAuth userinfo email responses causes false identity mismatches.
- **Enforcement:** `switch_account_safe.py` and `selection_policy.py` immediately reject non-canonical inputs with `SwitchOutcome.INVALID_ARGUMENT` (`error_code="NON_CANONICAL_EMAIL"`, exit code `1`).

---

## 6. Model-Specific Routing & Quota Rules (Item 11)

- **Rule 1:** `AccountQuotaSummary.eligible` is a general baseline sanity flag; the selector's requested `target_model_group` is **authoritative**.
- **Rule 2 (Independent Quotas):**
  - An account with `gemini_pro_pct = 0` and `gemini_flash_pct = 90` is **REJECTED** for `gemini-pro`, but **ELIGIBLE** for `gemini-flash`.
  - An account with `gemini_pro_pct = None` and `gemini_flash_pct = 100` produces **`BLOCKED_QUOTA_UNKNOWN`** for `gemini-pro` (never infer Pro quota from Flash quota).
  - An account with `claude_pct = 80`, `gemini_pro_pct = 0` is **REJECTED** for `gemini-pro`.

---

## 7. Fail-Closed Table Schema Validation (Item 10)

- **Default Mode:** Parses header strictly (`EMAIL`, `STATUS`, `GEM-PRO`, `GEM-FLASH`, `CLAUDE`). Any deviation (missing column, renamed column, reordered columns, corrupted header) fails closed:
  - `format_support = FORMAT_UNSUPPORTED`
  - `freshness_state = UNKNOWN_UNFETCHED`
  - `eligible = False`
- **Lenient Mode:** Only enabled with explicit `--research-lenient-parser` flag; strictly prohibited in automated supervisor flows.

---

## 8. Host Credential Incident Post-Mortem (Item 8)

1. **Was the host vault overwritten?** Yes. Invoking `agm.exe switch mock.worker1 --target agy` during Phase 0 testing wrote mock tokens into the real Windows Credential Manager target `gemini:antigravity`.
2. **What safe identity existed before?** A valid token structure was present in `gemini:antigravity` prior to synthetic testing.
3. **What mock state was written?** Synthetic access token with fingerprint prefix `0d5a3c...` for account `mock.worker1`.
4. **Was original credential restored?** An interactive manual re-authentication / vault update occurred.
5. **How was restoration performed?** Re-authenticating via standard Antigravity login flow.
6. **How was restoration verified?** `verify_active_account.py` confirms active credential structure present with valid token length.
7. **Did Antigravity lose authentication?** The running Electron process retained its active in-memory session; cold-started CLI commands read the mutated target.
8. **Cryptographic Proof Status:** `HOST_CREDENTIAL_RESTORATION = UNKNOWN` (cannot mathematically prove identical byte-for-byte token payload without exposing secrets).
9. **Permanent Remediation:** All unit tests in `validate_agm_output.py` strictly use `mock_payload` and `userinfo_fetcher` dependency injection. Invoking `agm switch` on the host OS during test suites is permanently prohibited.

---

## 9. Final Questions Answered

### IS LIVE_DESKTOP_A_TO_B_ADOPTION VERIFIED?
**`NO`** (Status remains `UNKNOWN` / `UNPROVEN` in T02 scope).
