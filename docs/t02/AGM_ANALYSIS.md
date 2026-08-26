# AGM Architecture, Quota Detection, Account Switching, and Verification Report (Zero-Trust Round 5 Final Closure)

**Author / Owner:** T02  
**Assigned Issue:** #2 — AGM quota detection, account switching, verification and security  
**Branch:** `research/T02-account-quota`  

---

## 1. Central Claim & Evidence Matrix (Round 5)

| Claim | Evidence Class | Source / Test Code | Raw Sanitized Artifact | Repro Command | Current Status | Remaining Gap |
|---|---|---|---|---|---|---|
| **1. Quota Freshness & Provenance** | `VERIFIED_SOURCE` & `UNIT_TEST` | `scripts/t02/refresh_quota_safe.py`, `scripts/t02/inspect_quota.py` | `tests/fixtures/t02/list_normal.txt` | `python scripts/t02/validate_agm_output.py` | **PROVEN**: Process-local live evidence requires sealed `LiveExecutionAttestation`. Deserialized JSON is forced to `UNTRUSTED_DESERIALIZED`. Independent binary hash binding strictly enforced. | Live multi-account Google rate-limit staging verification. |
| **2. `agy` Target Scope & Credential Write** | `VERIFIED_SOURCE` & `OBSERVED_LIVE_RUNTIME` | `internal/credstore/credstore.go:244`, `scripts/t02/switch_account_safe.py` | `tests/fixtures/t02/switch_success_agy.txt` | `agm.exe switch <email> --target agy` | **PROVEN**: Target is strictly restricted to `agy` (`scope = CREDENTIAL_STORE_ONLY`). Targets `ide` and `all` are rejected. | System-level writes require isolation; unit tests use injected mock runners. |
| **3. Desktop Adoption of Switched Account** | `UNKNOWN` | Upstream AGM `internal/target/target.go:13` | N/A | N/A | **UNKNOWN / UNPROVEN**: Upstream source defines `Agy = "agy"` as `Antigravity CLI (agy)`. Credential vault write does not prove Desktop adopted the token. | Requires T03/integration in-process Desktop turn/session evidence. |
| **4. Active-Account Identity Verification** | `VERIFIED_SOURCE` & `UNIT_TEST` | `scripts/t02/verify_active_account.py` | Unit tests in `validate_agm_output.py` | `python scripts/t02/verify_active_account.py --expected <email> --network` | **PROVEN for Credential Store**: PowerShell envelope reader + Google OAuth userinfo introspection verifies vault identity. Default output redacts raw emails to pseudonyms. | **UNKNOWN for Desktop Process**: True in-process session state cannot be verified from vault alone. |
| **5. Restart Requirement** | `UNKNOWN / INFERENCE` | `internal/process/process.go:33` | N/A | N/A | **UNKNOWN / INFERENCE**: `--target agy` performs no process kill/start. Process termination removed from T02 scope. | Controlled A -> B runtime adoption test required with T01/T03. |
| **6. Binary Source Equivalence** | `UNKNOWN / ADMIN_CONFIGURED` | `scripts/t02/inspect_quota.py` | Unit tests in `validate_agm_output.py` | `python scripts/t02/validate_agm_output.py` | **UNKNOWN / ADMIN_CONFIGURED**: Without independent expected binary hash binding (`expected_binary_sha256`), source equivalence is UNKNOWN and supervisor fails closed. | Administrator / build system must supply trusted expected SHA-256. |

---

## 2. Process-Local Attestation & Transport Trust Model (Critical Items 1, 2, 3, 4, 7)

### 2.1 Process-Local Capability Attestation
- **`LiveExecutionAttestation`**: An in-memory capability object containing:
  - `session_nonce`: Ephemeral nonce generated on supervisor startup
  - `execution_nonce`: UUID generated per live refresh execution
  - `account`: Canonical account email
  - `binary_sha256`: SHA-256 of the executed binary
  - `issued_at`: Monotonic timestamp
  - `capability_token`: HMAC-SHA256 signature minted with module-private key `_EXECUTOR_ATTESTATION_SECRET`.
- **Enforcement:** `validate_refresh_evidence_supervisor()` requires a valid `LiveExecutionAttestation` for any `PROCESS_LOCAL` live evidence. Manual construction of `RefreshEvidence` dataclasses without this capability fails closed to `STALE_CACHED`.

### 2.2 Independent Expected Binary Identity Binding (Critical Items 1 & 2)
- Supervisor validation compares `observed_binary_sha256` against `expected_binary_sha256`.
- Syntactically valid 64-hex SHA-256 hashes that do not match the expected binary hash fail closed with `BINARY_IDENTITY_MISMATCH` (`STALE_CACHED`).
- If no expected binary hash is configured, `BINARY_SOURCE_EQUIVALENCE = UNKNOWN` and operations fail closed.

### 2.3 Pre/Post Execution TOCTOU Mitigation (Item 9)
- `_execute_live_refresh_sealed()` computes `sha_pre` before subprocess execution and `sha_post` after subprocess execution.
- If `sha_pre != sha_post`: Stamped `RefreshResult.BINARY_IDENTITY_UNVERIFIED` / `BINARY_CHANGED_DURING_EXECUTION` and rejected.

---

## 3. Signed Serialized Transport Contract (Items 5 & 6)

- **HMAC Role:** Proves `TRANSPORT_AUTHENTICATED` (transport integrity across process boundaries). It does NOT independently prove source-code equivalence.
- **Secret Lifecycle:**
  - Loaded strictly from `$env:AGM_SESSION_SECRET` (256-bit entropy).
  - Ephemeral per supervisor session; never committed to git or passed in command-line arguments.
  - Serialized evidence from prior supervisor sessions is rejected (`supervisor_session_id` bound into HMAC payload).

---

## 4. Credential Reader Envelope Contract (Item 7)

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
| `0` | `found: true`, valid tokens, offline | `CREDENTIAL_STORE_WRITTEN_UNVERIFIED` | `True` | `MEDIUM` |
| `0` | `found: true`, userinfo match | `CREDENTIAL_STORE_IDENTITY_VERIFIED` | `True` | `STRONG` |
| `0` | `found: true`, userinfo mismatch | `IDENTITY_MISMATCH` | `True` | `STRONG` |

---

## 5. Sanitized Supervisor Output DTO Contract (Item 10)

- `SanitizedVerificationOutput`: Guaranteed free of raw emails or token fingerprints.
- Supervisor logging consumes only sanitized DTOs. Private diagnostic mode is explicit.
- Raw OAuth tokens are NEVER emitted under any circumstance.
