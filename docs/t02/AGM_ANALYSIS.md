# AGM Architecture, Quota Detection, Account Switching, and Verification Report (Zero-Trust Round 4 Revision)

**Author / Owner:** T02  
**Assigned Issue:** #2 — AGM quota detection, account switching, verification and security  
**Branch:** `research/T02-account-quota`  

---

## 1. Central Claim & Evidence Matrix (Round 4)

| Claim | Evidence Class | Source / Test Code | Raw Sanitized Artifact | Repro Command | Current Status | Remaining Gap |
|---|---|---|---|---|---|---|
| **1. Quota Freshness & Transport Trust** | `VERIFIED_SOURCE` & `UNIT_TEST` | `scripts/t02/refresh_quota_safe.py`, `scripts/t02/inspect_quota.py` | `tests/fixtures/t02/list_normal.txt` | `python scripts/t02/validate_agm_output.py` | **PROVEN**: Transport trust is strictly separated from source origin. Deserialized JSON is forced to `UNTRUSTED_DESERIALIZED` and cannot choose its own trust class. Supervisor mode strictly requires `LIVE_REFRESH_EXECUTION` + `PROCESS_LOCAL`/`SIGNED_DESERIALIZED`. | Multi-account bulk refresh under Google rate limits requires live staging testbed. |
| **2. `agy` Target Scope & Credential Write** | `VERIFIED_SOURCE` & `OBSERVED_LIVE_RUNTIME` | `internal/credstore/credstore.go:244`, `scripts/t02/switch_account_safe.py` | `tests/fixtures/t02/switch_success_agy.txt` | `agm.exe switch <email> --target agy` | **PROVEN**: Target is strictly restricted to `agy` (`scope = CREDENTIAL_STORE_ONLY`). Targets `ide` and `all` are rejected. | System-level writes require isolation; unit tests use injected mock runners. |
| **3. Desktop Adoption of Switched Account** | `UNKNOWN` | Upstream AGM `internal/target/target.go:13` | N/A | N/A | **UNKNOWN / UNPROVEN**: Upstream source defines `Agy = "agy"` as `Antigravity CLI (agy)`. Credential vault write does not prove Desktop adopted the token. | Requires T03/integration in-process Desktop turn/session evidence. |
| **4. Active-Account Identity Verification** | `VERIFIED_SOURCE` & `UNIT_TEST` | `scripts/t02/verify_active_account.py` | Unit tests in `validate_agm_output.py` | `python scripts/t02/verify_active_account.py --expected <email> --network` | **PROVEN for Credential Store**: PowerShell envelope reader + Google OAuth userinfo introspection verifies vault identity. Default output redacts raw emails to pseudonyms. | **UNKNOWN for Desktop Process**: True in-process session state cannot be verified from vault alone. |
| **5. Restart Requirement** | `UNKNOWN / INFERENCE` | `internal/process/process.go:33` | N/A | N/A | **UNKNOWN / INFERENCE**: `--target agy` performs no process kill/start. Process termination removed from T02 scope. | Controlled A -> B runtime adoption test required with T01/T03. |

---

## 2. Refresh Evidence Transport Trust Model (Critical Items 1, 2, 5, 11, 12)

### 2.1 Transport Trust vs. Source Origin Architecture
- **`source_origin`**: Identifies where the refresh was executed (`LIVE_REFRESH_EXECUTION`, `SYNTHETIC_TEST_EVIDENCE`, `DRY_RUN`).
- **`transport_trust`**: Identifies how the record crossed boundaries (`PROCESS_LOCAL`, `SIGNED_DESERIALIZED`, `UNTRUSTED_DESERIALIZED`).
- **Critical Security Invariant:** Any deserialized JSON or dictionary payload is forced to `transport_trust = UNTRUSTED_DESERIALIZED` upon entry. It is structurally impossible for forged JSON to self-declare `LIVE_REFRESH_EXECUTION` or bypass HMAC verification.
- **Elevation Rule:** Only verified HMAC-SHA256 signatures using `$env:AGM_SESSION_SECRET` elevate deserialized payloads to `SIGNED_DESERIALIZED`.
- **Sealed Live Origin Minting:** Only `_execute_live_refresh_sealed()` can mint `LIVE_REFRESH_EXECUTION`. The test executor `execute_refresh_for_test()` is structurally restricted to minting `SYNTHETIC_TEST_EVIDENCE`.

### 2.2 Invariant Validation Contract (Items 3, 4, 12)
`validate_refresh_evidence_supervisor()` validates:
1. `transport_trust` in (`PROCESS_LOCAL`, `SIGNED_DESERIALIZED`)
2. `source_origin == LIVE_REFRESH_EXECUTION`
3. `result == REFRESH_SUCCEEDED` and `exit_code == 0`
4. Exact canonical account match (RFC 5322)
5. Exact structured argv match: `[canonical_executable_path, "refresh", canonical_account]` (No suffix matching)
6. Known binary SHA-256 and matching inspected source revision (`1d3ce8497e36ffa60c3b4e369168315a7ae4d469`)
7. Mandatory session ID match
8. Monotonic timestamps (`started_at <= completed_at <= now + 2.0s`)
9. Duration sane (`0.0s <= duration <= 60.0s`) and freshness age <= 300.0s.

---

## 3. Credential Reader Envelope Contract (Item 7)

PowerShell probe returns a structured JSON envelope:
```json
{
  "found": true,
  "win32_code": 0,
  "blob_length": 128,
  "blob_utf8": "{...}"
}
```

| Return Code | Envelope State | Classification | Credential Present | Evidence Rank |
|---|---|---|---|---|
| `!= 0` | Subprocess failure / stderr | `POWERSHELL_PROCESS_FAILED` | `False` | `UNKNOWN` |
| `0` | `found: false`, `win32_code: 1168` | `CREDENTIAL_STORE_EMPTY` | `False` | `UNKNOWN` |
| `0` | `found: false`, `win32_code: 5` | `CREDENTIAL_STORE_ACCESS_DENIED` | `False` | `UNKNOWN` |
| `0` | `found: false`, `win32_code: != 0` | `CREDENTIAL_STORE_READ_ERROR` | `False` | `UNKNOWN` |
| `0` | `found: true`, `blob_length: 0` | `CREDENTIAL_TOKEN_FIELDS_MISSING` | `True` | `UNKNOWN` |
| `0` | `found: true`, corrupted JSON | `CREDENTIAL_PAYLOAD_INVALID` | `False` | `UNKNOWN` |
| `0` | `found: true`, valid tokens, offline | `CREDENTIAL_STORE_WRITTEN_UNVERIFIED` | `True` | `MEDIUM` |
| `0` | `found: true`, userinfo match | `CREDENTIAL_STORE_IDENTITY_VERIFIED` | `True` | `STRONG` |
| `0` | `found: true`, userinfo mismatch | `IDENTITY_MISMATCH` | `True` | `STRONG` |

---

## 4. Default Output Pseudonymization & Redaction (Item 8)

- **Default Output Format:** All production supervisor CLI outputs emit pseudonymous account references (`acc_<sha256_prefix>`), status enums, exit codes, and sanitized messages.
- **Private Diagnostic Mode:** Raw account emails, subprocess streams (`agm_stdout`, `agm_stderr`), and token SHA-256 fingerprint prefixes are isolated behind explicit `--private-diagnostic-mode` flags.
- **Zero Raw Secret Exposure:** Raw OAuth tokens are NEVER printed, logged, or serialized.

---

## 5. Strict Info Parser Contract (Item 6)

`parse_agm_info()` strictly validates exact table header columns in order: `PROVIDER`, `MODEL`, `SCORE`, `RESET`. Any deviation (missing `RESET`, renamed column, reordered columns, corrupted header) fails closed to `format_support = FORMAT_UNSUPPORTED`, `eligible = False`.

---

## 6. Zero-Trust Architectural Answers

1. **CAN FORGED JSON CLAIMING LIVE_REFRESH_EXECUTION BECOME PROVEN_FRESH?**
   **`NO`** (Forced to `UNTRUSTED_DESERIALIZED` upon entry; rejected in supervisor mode).
2. **CAN AN INJECTED MOCK RUNNER MINT LIVE EVIDENCE?**
   **`NO`** (`execute_refresh_for_test()` is structurally incapable of minting `LIVE_REFRESH_EXECUTION`).
3. **CAN COMMAND SUFFIX MATCHING PASS?**
   **`NO`** (Exact element-by-element argv check enforced).
4. **IS THE AGM BINARY BOUND TO A VERIFIED IDENTITY?**
   **`YES, or fail closed`** (Requires binary SHA-256 and inspected source revision `1d3ce8497e36ffa60c3b4e369168315a7ae4d469`).
5. **CAN SYNTHETIC EVIDENCE BE ENABLED THROUGH THE SUPERVISOR API?**
   **`NO`** (Supervisor API has no test-weakening parameters).
6. **IS LIVE_DESKTOP_A_TO_B_ADOPTION VERIFIED?**
   **`NO`** (Status remains `UNKNOWN` / `UNPROVEN` in T02 scope).
