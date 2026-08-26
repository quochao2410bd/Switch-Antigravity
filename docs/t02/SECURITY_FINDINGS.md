# T02 Security Audit and Findings: AGM Integration (Zero-Trust Round 4 Revision)

## 1. Threat Model & Provenance Trust Model

### 1.1 Transport Trust vs Source Origin Model
- **Process-Local Execution:** Trusted execution within the same supervisor process mints `source_origin = EvidenceSourceOrigin.LIVE_REFRESH_EXECUTION`, `transport_trust = TransportTrustClass.PROCESS_LOCAL`.
- **Deserialized Payloads:** All deserialized JSON/dict inputs are initialized as `transport_trust = TransportTrustClass.UNTRUSTED_DESERIALIZED`. Forged self-declarations of `LIVE_REFRESH_EXECUTION` cannot bypass verification.
- **HMAC Session Signing:** Cross-process serialized evidence requires HMAC-SHA256 signature verification loaded from `$env:AGM_SESSION_SECRET`. Unsigned payloads fail closed.
- **Test Isolation:** `execute_refresh_for_test()` is structurally sealed to only mint `SYNTHETIC_TEST_EVIDENCE`.

### 1.2 Target Scope Restriction
- T02 safe switch is restricted strictly to `--target agy`, writing exclusively to Windows Credential Manager `gemini:antigravity`. Targets `ide` and `all` are rejected.

---

## 2. Production Log Exposure & Output Redaction (Item 8)

- **Default Output:** Emits pseudonymous account references (`acc_<sha256_prefix>`), status enums, exit codes, and sanitized messages.
- **Private Diagnostic Mode:** Raw account emails, subprocess streams (`agm_stdout`, `agm_stderr`), and token SHA-256 fingerprint prefixes are isolated behind `--private-diagnostic-mode`.
- **Zero Raw Secret Exposure:** Raw OAuth tokens (`access_token`, `refresh_token`) are NEVER logged, printed, or persisted.

---

## 3. Host Credential Incident Status (Item 10)

- **Status:** `HOST_CREDENTIAL_RESTORATION = UNKNOWN`.
- Historical byte-for-byte token payload equality prior to synthetic testing cannot be proven without historical secret storage.
- All test suites are globally trapped with zero host vault reads/writes.
