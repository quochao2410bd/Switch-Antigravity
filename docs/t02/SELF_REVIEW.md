# T02 Adversarial Self-Review (Zero-Trust Round 4 Revision)

## 1. Adversarial Audit of Round 4 Remediations

1. **Deserialized Trust Enforcement:** Fixed trust bypass where dicts chose their own origin. Forced `UNTRUSTED_DESERIALIZED` on all deserialized payloads.
2. **Sealed Live Origin Minting:** Separated `_execute_live_refresh_sealed()` from `execute_refresh_for_test()`. Fake runners cannot mint live evidence.
3. **Exact Argv Equality:** Enforced element-by-element argv checking with zero suffix matching.
4. **Binary SHA-256 Binding:** Added binary hashing and bound to inspected revision `1d3ce8497e36ffa60c3b4e369168315a7ae4d469`.
5. **Credential Envelope:** PowerShell returns structured envelope separating `1168` (not found) from zero-length blobs (missing token fields).
6. **Default Output Pseudonymization:** Default CLI outputs emit pseudonymous references (`acc_<hash>`).
7. **Global Test Isolation Trap:** Globally trapped subprocess and urllib handlers, verifying `OS_CRED_READ_CALLS = 0`, `OS_CRED_WRITE_CALLS = 0`, `LIVE_AGM_CALLS = 0`, `LIVE_GOOGLE_HTTP_CALLS = 0`.
8. **Clean Supervisor API:** Removed test-weakening flags from supervisor production interfaces.

---

## 2. Unresolved Boundaries

1. **Desktop Adoption Gate:** Status remains **`LIVE_DESKTOP_A_TO_B_ADOPTION = UNKNOWN`**.
2. **Host Credential Restoration:** Status remains **`HOST_CREDENTIAL_RESTORATION = UNKNOWN`**.
