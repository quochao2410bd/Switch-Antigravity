# T02 Security Audit and Findings: AGM Integration (Zero-Trust Round 5 Final Closure)

## 1. Threat Model & Provenance Trust Model

### 1.1 Process-Local Capability Attestation Model
- **Process-Local Execution:** Trusted execution within the same supervisor process mints `source_origin = EvidenceSourceOrigin.LIVE_REFRESH_EXECUTION`, `transport_trust = TransportTrustClass.PROCESS_LOCAL`, and attaches an unforgeable in-memory `LiveExecutionAttestation`.
- **Manual Typed Forgery Defense:** Callers attempting to manually construct `RefreshEvidence` without valid `LiveExecutionAttestation` capabilities are rejected as `STALE_CACHED`.
- **Deserialized Payloads:** All deserialized JSON/dict inputs are initialized as `transport_trust = TransportTrustClass.UNTRUSTED_DESERIALIZED`. Forged claims of `LIVE_REFRESH_EXECUTION` fail closed.
- **HMAC Session Signing:** Cross-process serialized evidence requires HMAC-SHA256 signature verification loaded from `$env:AGM_SESSION_SECRET`. Unsigned payloads fail closed.

### 1.2 Binary Identity Binding & TOCTOU Defense
- **Expected Hash Binding:** Supervisor enforces `evidence.binary_sha256 == expected_binary_sha256`. Syntactically valid non-matching hashes fail closed.
- **TOCTOU Mutation Check:** Hashes binary immediately before and after execution; mutation fails closed (`BINARY_CHANGED_DURING_EXECUTION`).
- **Binary Source Equivalence:** `BINARY_SOURCE_EQUIVALENCE = UNKNOWN` unless independent expected binary hash is configured.

---

## 2. Production Log Exposure & Output Privacy Contract (Item 10)

- **Sanitized DTO:** Supervisor consumers receive `SanitizedVerificationOutput` containing only pseudonymous `account_ref` (`acc_<hash>`), status enums, exit codes, and sanitized messages.
- **Private Diagnostic Isolation:** Raw emails, subprocess streams, and token fingerprints are strictly isolated behind explicit private diagnostic methods.
- **Zero Raw Secret Exposure:** Raw OAuth tokens (`access_token`, `refresh_token`) are NEVER logged, printed, or persisted.

---

## 3. Host Credential Incident Status

- **Status:** `HOST_CREDENTIAL_RESTORATION = UNKNOWN`.
- Historical byte-for-byte token payload equality prior to synthetic testing cannot be proven without historical secret storage.
- All test suites are globally trapped with verified tripwires (`OS_CRED_READ_CALLS = 0`, `OS_CRED_WRITE_CALLS = 0`, `LIVE_AGM_CALLS = 0`, `LIVE_GOOGLE_HTTP_CALLS = 0`).
