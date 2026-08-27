# AGM Architecture, Quota Detection, Account Switching, and Verification Report (Zero-Trust Round 7 Final Closure)

**Author / Owner:** T02  
**Assigned Issue:** #2 — AGM quota detection, account switching, verification and security  
**Branch:** `research/T02-account-quota`  

---

## 1. Architectural Role: Thin Safety Adapter, NOT a Multi-Account Manager (Item 8)

T02 is strictly a **thin safety adapter** around Antigravity Manager (AGM). AGM is already the complete multi-account manager.

```
+-------------------------------------------------------------------------+
|                  ANTIGRAVITY MANAGER (AGM CLI / Upstream)               |
|  - Account DB (accounts.json)       - OAuth Login & Token Flow          |
|  - AES-256-GCM Credential Storage   - Alias Management                  |
|  - Import / Export / Backup         - Token Refresh & Quota Retrieval   |
|  - Switch Primitive & Vault Write   - Target Management                 |
+-------------------------------------------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
|                      T02 THIN SAFETY ADAPTER                            |
|  - PRE-EXECUTION Binary Trust Gate  - TrustedAgmRunner Shared Invoker   |
|  - Freshness Provenance Binding     - Scope Enforcer (--target agy)     |
|  - Privacy Sanitization (acc_ref)   - Normalized Error Codes (No Leaks) |
|  - Independent Credential Probe     - Structured Result Normalization   |
+-------------------------------------------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
|                      MAIN SUPERVISOR WATCHDOG                           |
|                       /                    \                            |
|           T01 Desktop Engine          T03 Turn & Resume Recovery        |
+-------------------------------------------------------------------------+
```

### 1.1 Existing Manager Reuse Contract
- **AGM OWNS:** Account storage, OAuth login, encrypted credential persistence, alias resolution, account import/export, quota retrieval from Google API, token refresh, and switch execution.
- **T02 OWNS ONLY:** Safe command invocation via `TrustedAgmRunner`, pre-execution binary identity verification, freshness provenance verification, minimal selection safety policy when AGM primitive is insufficient, switch result normalization, independent Windows credential store verification, and fail-closed error handling.
- **T02 MUST NOT CREATE:** Another account database, another OAuth flow, another token encryption store, another alias registry, another account import/export layer, or a replacement multi-account daemon.

---

## 2. Pre-Execution Binary Trust Architecture (`TrustedAgmRunner`) (Critical Items 1, 2, 3, 4)

In Round 7, T02 eliminates the execute-before-verify defect by introducing `TrustedAgmRunner`.

### 2.1 Execution Trust Pipeline
1. **Resolve Canonical Executable:** Absolute path resolution. Missing -> `AGM_NOT_FOUND`, `SUBPROCESS_CALL_COUNT = 0`.
2. **Validate Trusted Identity Config:** `TrustedAgmIdentity` must be non-empty and 64-hex format. Missing -> `BINARY_IDENTITY_UNCONFIGURED`, Malformed -> `BINARY_IDENTITY_CONFIG_INVALID`, `SUBPROCESS_CALL_COUNT = 0`.
3. **Compute Pre-Execution SHA-256:** Pre-execution hash of target file. Unverified -> `BINARY_IDENTITY_UNVERIFIED`, `SUBPROCESS_CALL_COUNT = 0`.
4. **Compare Observed vs Expected SHA:** If `observed_sha != expected_sha` -> `BINARY_IDENTITY_MISMATCH`, **`SUBPROCESS_CALL_COUNT = 0` (DO NOT EXECUTE)**.
5. **Execute Subprocess:** Subprocess is invoked **ONLY IF ALL PRE-EXECUTION CHECKS PASS**.
6. **Post-Execution TOCTOU Check:** Executable is re-hashed immediately after completion. If hash changed -> `BINARY_CHANGED_DURING_EXECUTION`.

All AGM execution surfaces (`refresh`, `switch`, `list`, `info`) strictly route through `TrustedAgmRunner`.

---

## 3. Source Analysis of Built-In `agm auto-switch` (Item 9 & 10)

At inspected upstream revision `1d3ce8497e36ffa60c3b4e369168315a7ae4d469`:

| AGM `auto-switch` Characteristic | Observed Implementation Behavior | Why Autonomous Watchdog Cannot Directly Rely on It | T02 Thin Safety Adapter Role |
|---|---|---|---|
| **1. Target Scope** | Calls `switchAllToAccount()` switching all expanded targets (CLI, IDE, Config). | Watchdog requires strict `agy`-only (Credential Store) isolation to prevent unintended IDE disruptions. | `switch_account_safe.py` enforces `--target agy`. |
| **2. Quota Freshness** | Evaluates cached quota snapshot from local `accounts.json`. | Snapshot may be stale or unverified; no cryptographic freshness provenance. | `inspect_quota.py` enforces freshness age ceiling and provenance binding. |
| **3. Model Granularity** | Broad model matching across available models. | Watchdog requires explicit per-model routing (e.g. Gemini 1.5 Pro vs Gemini 1.5 Flash). | `selection_policy.py` validates strict `ModelGroup` enums. |
| **4. Failure & Cooldown Gates** | No per-account failure penalties or backoff cooldowns. | Rapid failure loops on revoked/rate-limited tokens would exhaust all accounts. | `selection_policy.py` applies exponential cooldowns per failure. |
| **5. Desktop Coordination** | No integration with Desktop Electron process lifecycle. | Token vault write does not guarantee Electron renderer adoption without turn recovery. | Gated for T01/T03 coordination in supervisor integration. |

---

## 4. Central Claim & Evidence Matrix (Round 7)

| Claim | Evidence Class | Source / Test Code | Raw Sanitized Artifact | Repro Command | Current Status | Remaining Gap |
|---|---|---|---|---|---|---|
| **1. Pre-Execution Binary Gate** | `VERIFIED_CODE_INSPECTION` & `UNIT_TEST` | `scripts/t02/trusted_agm_runner.py` | Unit tests in `validate_agm_output.py` | `python scripts/t02/validate_agm_output.py` | **PROVEN**: Missing/malformed/mismatched SHA rejects invocation with `SUBPROCESS_CALL_COUNT = 0`. | Admin/build system must supply `expected_binary_sha256`. |
| **2. Quota Freshness & Provenance** | `VERIFIED_SOURCE` & `UNIT_TEST` | `scripts/t02/refresh_quota_safe.py`, `scripts/t02/inspect_quota.py` | `tests/fixtures/t02/list_normal.txt` | `python scripts/t02/validate_agm_output.py` | **PROVEN**: Mandatory `TrustedAgmIdentity` binding enforced. Deserialized JSON forced to `UNTRUSTED_DESERIALIZED`. | Multi-account live staging verification. |
| **3. `agy` Target Scope & Credential Write** | `VERIFIED_SOURCE` & `OBSERVED_LIVE_RUNTIME` | `internal/credstore/credstore.go:244`, `scripts/t02/switch_account_safe.py` | `tests/fixtures/t02/switch_success_agy.txt` | `agm.exe switch <email> --target agy` | **PROVEN**: Target is strictly restricted to `agy` (`scope = CREDENTIAL_STORE_ONLY`). Targets `ide` and `all` are rejected. | System-level writes require isolation; unit tests use injected mock runners. |
| **4. Active-Account Identity Verification** | `VERIFIED_SOURCE` & `UNIT_TEST` | `scripts/t02/verify_active_account.py` | Unit tests in `validate_agm_output.py` | `python scripts/t02/verify_active_account.py --expected <email> --network` | **PROVEN for Credential Store**: PowerShell envelope reader + Google OAuth userinfo introspection verifies vault identity. Default output contains only pseudonyms. | **UNKNOWN for Desktop Process**: In-process Electron session state cannot be verified from vault alone. |
| **5. Desktop Adoption of Switched Account** | `UNKNOWN` | Upstream AGM `internal/target/target.go:13` | N/A | N/A | **UNKNOWN / UNPROVEN**: Upstream source defines `Agy = "agy"` as `Antigravity CLI (agy)`. Credential vault write does not prove Desktop adopted the token. | Requires T03/integration in-process Desktop turn/session evidence. |
| **6. Binary Source Equivalence** | `UNKNOWN / ADMIN_CONFIGURED` | `scripts/t02/trusted_agm_runner.py` | Unit tests in `validate_agm_output.py` | `python scripts/t02/validate_agm_output.py` | **UNKNOWN / ADMIN_CONFIGURED**: An expected SHA proves match against an approved binary, but does NOT by itself prove reproducible build from upstream commit `1d3ce8497e36ffa60c3b4e369168315a7ae4d469`. | Independent source->binary build provenance required. |
