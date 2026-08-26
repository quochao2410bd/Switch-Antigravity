# T02 Adversarial Self-Review (Zero-Trust Round 2 Revision)

## 1. Adversarial Scrutiny of Findings

### 1.1 Refresh Provenance Contract
- **Round 1 Defect:** Synthetic tests passed raw timestamps in Python dictionaries, proving only arithmetic without validating real refresh execution.
- **Round 2 Correction:** Designed typed `RefreshEvidence` generated strictly by refresh executor. Only `REFRESH_SUCCEEDED` for the matching canonical account and active session ID can produce `PROVEN_FRESH`.

### 1.2 Info Mode Provenance Support
- **Round 1 Defect:** `--provenance-json` was parsed by CLI but ignored when in `info` mode.
- **Round 2 Correction:** `parse_agm_info` now accepts `refresh_evidence` and CLI passes it in info mode.

### 1.3 Credential Read Error Distinctions
- **Round 1 Defect:** All exceptions in `read_windows_credential_payload()` were collapsed into `CREDENTIAL_STORE_EMPTY`.
- **Round 2 Correction:** Separated `CREDENTIAL_STORE_EMPTY`, `CREDENTIAL_STORE_ACCESS_DENIED`, `CREDENTIAL_STORE_READ_ERROR`, `CREDENTIAL_STORE_UNAVAILABLE`, and `CREDENTIAL_PAYLOAD_INVALID`.

### 1.4 Unverified Switch Exit Code
- **Round 1 Defect:** `SWITCH_WRITTEN_UNVERIFIED` exited `0`, making unverified writes indistinguishable from verified successes.
- **Round 2 Correction:** Assigned distinct exit code `2` for `SWITCH_WRITTEN_UNVERIFIED` (exit code `0` reserved exclusively for `CREDENTIAL_IDENTITY_VERIFIED`).

### 1.5 Alias Resolution Policy
- **Round 1 Defect:** Allowing aliases resulted in comparing alias strings to Google userinfo emails.
- **Round 2 Correction:** Enforced canonical email format (`is_canonical_email()`) across safe switch and selection policy.

### 1.6 Network Verifier Test Coverage
- **Round 1 Defect:** Network userinfo verification branches were not tested synthetically.
- **Round 2 Correction:** Built dependency-injectable `userinfo_fetcher` test harness covering match, mismatch, 401 token rejection, network timeout, and malformed payload.

### 1.7 Table Schema Fail-Closed Behavior
- **Round 1 Defect:** Parser fell back to opportunistic token parsing on unexpected column layouts.
- **Round 2 Correction:** Parser enforces strict header schema checking and fails closed (`FORMAT_UNSUPPORTED`, `eligible=False`) unless explicit research flag is provided.

### 1.8 Model-Specific Routing Gating
- **Round 1 Defect:** Selection policy did not test independent quota exhaustion across Pro, Flash, and Claude models.
- **Round 2 Correction:** Target model group is authoritative; Pro=0 / Flash=90 is rejected for Pro and eligible for Flash. Missing target quota triggers `BLOCKED_QUOTA_UNKNOWN`.

---

## 2. Unresolved Risks and Integration Scope

1. **Desktop Adoption Gate:** Status remains **`LIVE_DESKTOP_A_TO_B_ADOPTION = UNKNOWN`**. Supervisor must integrate with T03 in-process session validation before assuming active model turn switch.
2. **Workspace Checkpoint before Process Kill:** Supervisor must enforce git status check prior to any experimental desktop process termination.
