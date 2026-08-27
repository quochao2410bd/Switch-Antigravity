# T02 Adversarial Self-Review (Zero-Trust Round 6 Final Closure)

## 1. Adversarial Audit of Round 6 Remediations

1. **Mandatory Expected Binary SHA-256 Binding:** Verified missing (`BINARY_IDENTITY_UNCONFIGURED`), malformed (`BINARY_IDENTITY_CONFIG_INVALID`), and mismatched (`BINARY_IDENTITY_MISMATCH`) expected hashes all fail closed.
2. **Production Parser Threading:** Verified `parse_agm_list` and `parse_agm_info` fail closed to `STALE_CACHED` and `eligible = False` when `TrustedAgmIdentity` is omitted or mismatched.
3. **Honest Process-Local TCB Model:** Stated honestly that the entire supervisor Python process is the TCB; attestation is a misuse/accidental-call guard.
4. **Clean Production Live Origin:** Removed test injection hooks from `_execute_live_refresh_sealed()`; tests use `execute_refresh_for_test()` or `_validate_refresh_evidence_for_test()`.
5. **Output Sanitization & Privacy:** Verified `SanitizedRefreshEvidenceDTO`, `SanitizedAccountQuotaDTO`, and `SanitizedVerificationOutput` emit only pseudonymous `account_ref` and zero raw emails or capability tokens.
6. **AGM Reuse & Auto-Switch Analysis:** Documented why AGM owns all multi-account management and why `selection_policy.py` exists only as a thin 150-line adapter for watchdog-specific gaps.

---

## 2. Unresolved Boundaries (Honest Closure Contract)

1. **Binary Source Equivalence:** `BINARY_SOURCE_EQUIVALENCE = UNKNOWN` (unless explicit `expected_binary_sha256` configured).
2. **Desktop Adoption Gate:** `LIVE_DESKTOP_A_TO_B_ADOPTION = UNKNOWN`.
3. **Host Credential Restoration:** `HOST_CREDENTIAL_RESTORATION = UNKNOWN`.
