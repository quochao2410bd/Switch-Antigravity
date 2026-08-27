# T02 Security Audit and Findings: AGM Integration (Zero-Trust Round 6 Final Closure)

## 1. Threat Model & TCB Trust Boundaries

### 1.1 Process-Local TCB Model (Items 3 & 4)
- **Same-Process Supervisor Modules:** The entire supervisor Python process is within the Trusted Computing Base (TCB). Same-process modules are trusted.
- **Accidental Misuse Defense:** `LiveExecutionAttestation` prevents accidental caller construction of `LIVE_REFRESH_EXECUTION` within the supervisor without passing through the sealed executor.
- **Cross-Process Serialized Boundary:** Any JSON/dict payload arriving from outside the supervisor process is treated as `UNTRUSTED_DESERIALIZED`. It can only establish `SIGNED_DESERIALIZED` via HMAC-SHA256 verification using `$env:AGM_SESSION_SECRET`.

### 1.2 Binary Identity Binding & TOCTOU Defense (Items 1, 2, 5)
- **Mandatory Expected Binary SHA-256:** Independent verification against `expected_binary_sha256`. Missing, malformed, or mismatched hashes fail closed (`STALE_CACHED`).
- **TOCTOU Mutation Check:** Hashes executable immediately before and after execution; mutation fails closed (`BINARY_CHANGED_DURING_EXECUTION`).
- **Clean Production Live Path:** No test hooks or mock execution injected into production live refresh path.

---

## 2. Privacy & Data Minimization Contract (Items 6, 7, 11)

- **Sanitized Default DTOs:** All supervisor-facing DTOs (`SanitizedRefreshEvidenceDTO`, `SanitizedAccountQuotaDTO`, `SanitizedVerificationOutput`) emit ONLY stable pseudonymous `account_ref` (`acc_<hash>`).
- **Internal Orchestration:** Canonical email addresses exist strictly within in-memory structures to invoke `agm switch <email> --target agy`.
- **Zero Raw Secret Exposure:** Raw OAuth tokens (`access_token`, `refresh_token`), capability tokens, and HMAC secrets are NEVER emitted or logged by default.

---

## 3. Host Credential Incident Status & Closure Boundaries (Item 12)

- **Status:** `HOST_CREDENTIAL_RESTORATION = UNKNOWN`.
- Historical byte-for-byte token payload equality prior to synthetic testing cannot be proven without historical secret storage.
- All unit tests are globally trapped with verified tripwires (`OS_CRED_READ_CALLS = 0`, `OS_CRED_WRITE_CALLS = 0`, `LIVE_AGM_CALLS = 0`, `LIVE_GOOGLE_HTTP_CALLS = 0`).
- Cross-agent boundaries remain explicitly `UNKNOWN`:
  - `BINARY_SOURCE_EQUIVALENCE = UNKNOWN` (unless explicit `expected_binary_sha256` configured).
  - `LIVE_DESKTOP_A_TO_B_ADOPTION = UNKNOWN`.
  - `HOST_CREDENTIAL_RESTORATION = UNKNOWN`.
