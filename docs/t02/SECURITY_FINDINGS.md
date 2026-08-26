# T02 Security Audit and Findings: AGM Integration (Zero-Trust Round 3 Revision)

## 1. Threat Model & Provenance Trust Model

### 1.1 RefreshEvidence Trust Model
- **Process-Local Typed Origin:** Supervisor accepts `RefreshEvidence` instances directly returned from `execute_safe_refresh()` with `origin == EvidenceTrustOrigin.LIVE_REFRESH_EXECUTION`.
- **HMAC Session Signing:** Serialized evidence across process boundaries requires HMAC-SHA256 signature verification with an ephemeral session secret. Unsigned or mismatched records are classified `UNTRUSTED_DESERIALIZED` / `RESEARCH_ONLY` and rejected.
- **Synthetic Isolation:** `SYNTHETIC_TEST_EVIDENCE` records are rejected by default in supervisor mode, preventing test artifacts from leaking into production decision paths.

### 1.2 Credential Store Scope & Isolation
- **Target Restriction:** T02 safe wrapper strictly restricts target to `"agy"`, operating solely against Windows Credential Manager `gemini:antigravity`.
- **Non-Interference:** T02 does not touch IDE SQLite databases or kill Desktop processes.
- **Host Test Isolation:** All unit and synthetic test suites utilize dependency-injected mock runners and verifiers, ensuring 100% host vault isolation (zero reads, zero writes).

---

## 2. Production Log Exposure & Output Redaction (Item 16)

- **Default Output:** Emits only sanitized metadata: `account_ref`, `status`, `target_product`, `scope`, `desktop_adoption_status`, `exit_code`.
- **Diagnostic Mode:** Raw subprocess stderr/stdout and token fingerprint prefixes are sequestered behind explicit `--diagnostic-mode` flags.
- **Zero Raw Secret Exposure:** Raw OAuth tokens (`access_token`, `refresh_token`) are NEVER logged, printed, or persisted.

---

## 3. Incident Post-Mortem Status

- **Status:** `HOST_CREDENTIAL_RESTORATION = UNKNOWN`.
- Active credential target is restored; mock test decoupling via dependency injection permanently prevents test-runner mutation of host OS credentials.
