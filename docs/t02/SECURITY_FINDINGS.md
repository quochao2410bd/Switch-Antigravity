# T02 Security Audit and Findings: AGM Integration (Zero-Trust Round 7 Final Closure)

## 1. Threat Model & TCB Trust Boundaries

### 1.1 Process-Local TCB Model
- **Same-Process Supervisor Modules:** The entire supervisor Python process is within the Trusted Computing Base (TCB). Same-process modules are trusted.
- **Accidental Misuse Defense:** `LiveExecutionAttestation` prevents accidental caller construction of `LIVE_REFRESH_EXECUTION` within the supervisor without passing through the sealed executor.
- **Cross-Process Serialized Boundary:** Any JSON/dict payload arriving from outside the supervisor process is treated as `UNTRUSTED_DESERIALIZED`. It can only establish `SIGNED_DESERIALIZED` via HMAC-SHA256 verification using `$env:AGM_SESSION_SECRET`.

### 1.2 Pre-Execution Binary Trust Gate (Critical Items 1 & 3)
- **Pre-Execution Check:** `TrustedAgmRunner` validates `expected_binary_sha256` BEFORE any subprocess call.
- **Execution Invariant:** If the binary is missing, identity unconfigured, format invalid, or hash mismatched, `SUBPROCESS_CALL_COUNT == 0` (DO NOT EXECUTE).
- **Post-Execution TOCTOU Mutation Check:** Re-hashes binary immediately after subprocess termination.

---

## 2. Privacy & Data Minimization Contract (Critical Items 6, 7, 8, 9, 10)

- **Sanitized Default DTOs:** All supervisor-facing DTOs (`SanitizedRefreshEvidenceDTO`, `SanitizedAccountQuotaDTO`, `SanitizedVerificationOutput`, `switch_account_safe` default output) emit ONLY stable pseudonymous `account_ref` (`acc_<hash>`) and normalized error/warning codes.
- **Zero Free-Text Leaks:** `error_code` and `sanitized_warnings` use static predefined enums/codes. No raw emails, paths, bearer headers, tokens, command lines, or stderr traces exist in default output.
- **Invalid Account Input Protection:** Invalid account strings are NEVER echoed raw outside private diagnostic mode.
- **Adversarial Test Verification:** The adversarial privacy test matrix proves 100% absence of sensitive tokens/markers in all default JSON outputs.

---

## 3. Closure Boundaries & Honest Unknowns (Item 12)

- `BINARY_SOURCE_EQUIVALENCE = UNKNOWN / ADMIN_CONFIGURED` (A configured SHA proves match against an approved binary, but does NOT by itself prove reproducible build from upstream commit `1d3ce8497e36ffa60c3b4e369168315a7ae4d469`).
- `LIVE_DESKTOP_A_TO_B_ADOPTION = UNKNOWN` (Requires T01/T03 runtime coordination).
- `HOST_CREDENTIAL_RESTORATION = UNKNOWN` (Zero historical secret storage; zero unmocked host side-effects).
