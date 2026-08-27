# AGM Architecture, Quota Detection, Account Switching, and Verification Report (Zero-Trust Round 6 Final Closure)

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
|  - Trusted Binary Identity Gate     - Freshness Provenance Verification |
|  - Scope Enforcer (--target agy)    - Fail-Closed Output Parsing        |
|  - Privacy Sanitization (acc_ref)   - Thin Supervisor Decision Policy   |
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
- **T02 OWNS ONLY:** Safe command invocation, trusted binary identity validation, freshness provenance verification, minimal selection safety policy when AGM primitive is insufficient, switch result normalization, independent Windows credential store verification, and fail-closed error handling.
- **T02 MUST NOT CREATE:** Another account database, another OAuth flow, another token encryption store, another alias registry, another account import/export layer, or a replacement multi-account daemon.

---

## 2. Source Analysis of Built-In `agm auto-switch` (Item 9 & 10)

At the inspected upstream revision (`1d3ce8497e36ffa60c3b4e369168315a7ae4d469`), AGM provides a built-in `agm auto-switch` command. We performed source-level analysis of its implementation:

| AGM `auto-switch` Characteristic | Observed Implementation Behavior | Why Autonomous Watchdog Cannot Directly Rely on It | T02 Thin Safety Adapter Role |
|---|---|---|---|
| **1. Target Scope** | Calls `switchAllToAccount()` switching all expanded targets (CLI, IDE, Config). | Watchdog requires strict `agy`-only (Credential Store) isolation to prevent unintended IDE disruptions. | `switch_account_safe.py` enforces `--target agy`. |
| **2. Quota Freshness** | Evaluates cached quota snapshot from local `accounts.json`. | Snapshot may be stale or unverified; no cryptographic freshness provenance. | `inspect_quota.py` enforces freshness age ceiling and provenance binding. |
| **3. Model Granularity** | Broad model matching across available models. | Watchdog requires explicit per-model routing (e.g. Gemini 1.5 Pro vs Gemini 1.5 Flash). | `selection_policy.py` validates strict `ModelGroup` enums. |
| **4. Failure & Cooldown Gates** | No per-account failure penalties or backoff cooldowns. | Rapid failure loops on revoked/rate-limited tokens would exhaust all accounts. | `selection_policy.py` applies exponential cooldowns per failure. |
| **5. Desktop Coordination** | No integration with Desktop Electron process lifecycle. | Token vault write does not guarantee Electron renderer adoption without turn recovery. | Gated for T01/T03 coordination in supervisor integration. |

**Justification for `selection_policy.py`:** It is NOT an account manager; it is a thin 150-line evaluation policy bridging these specific autonomous watchdog requirements over already-inspected AGM accounts.

---

## 3. Central Claim & Evidence Matrix (Round 6)

| Claim | Evidence Class | Source / Test Code | Raw Sanitized Artifact | Repro Command | Current Status | Remaining Gap |
|---|---|---|---|---|---|---|
| **1. Quota Freshness & Provenance** | `VERIFIED_SOURCE` & `UNIT_TEST` | `scripts/t02/refresh_quota_safe.py`, `scripts/t02/inspect_quota.py` | `tests/fixtures/t02/list_normal.txt` | `python scripts/t02/validate_agm_output.py` | **PROVEN**: Mandatory `expected_binary_sha256` binding enforced. Missing/malformed/mismatched hash fails closed. Deserialized JSON forced to `UNTRUSTED_DESERIALIZED`. | Live multi-account Google rate-limit staging verification. |
| **2. `agy` Target Scope & Credential Write** | `VERIFIED_SOURCE` & `OBSERVED_LIVE_RUNTIME` | `internal/credstore/credstore.go:244`, `scripts/t02/switch_account_safe.py` | `tests/fixtures/t02/switch_success_agy.txt` | `agm.exe switch <email> --target agy` | **PROVEN**: Target is strictly restricted to `agy` (`scope = CREDENTIAL_STORE_ONLY`). Targets `ide` and `all` are rejected. | System-level writes require isolation; unit tests use injected mock runners. |
| **3. Desktop Adoption of Switched Account** | `UNKNOWN` | Upstream AGM `internal/target/target.go:13` | N/A | N/A | **UNKNOWN / UNPROVEN**: Upstream source defines `Agy = "agy"` as `Antigravity CLI (agy)`. Credential vault write does not prove Desktop adopted the token. | Requires T03/integration in-process Desktop turn/session evidence. |
| **4. Active-Account Identity Verification** | `VERIFIED_SOURCE` & `UNIT_TEST` | `scripts/t02/verify_active_account.py` | Unit tests in `validate_agm_output.py` | `python scripts/t02/verify_active_account.py --expected <email> --network` | **PROVEN for Credential Store**: PowerShell envelope reader + Google OAuth userinfo introspection verifies vault identity. Default output redacts raw emails to pseudonyms. | **UNKNOWN for Desktop Process**: True in-process session state cannot be verified from vault alone. |
| **5. Restart Requirement** | `UNKNOWN / INFERENCE` | `internal/process/process.go:33` | N/A | N/A | **UNKNOWN / INFERENCE**: `--target agy` performs no process kill/start. Process termination removed from T02 scope. | Controlled A -> B runtime adoption test required with T01/T03. |
| **6. Binary Source Equivalence** | `UNKNOWN / ADMIN_CONFIGURED` | `scripts/t02/inspect_quota.py` | Unit tests in `validate_agm_output.py` | `python scripts/t02/validate_agm_output.py` | **UNKNOWN / ADMIN_CONFIGURED**: Without independent expected binary hash binding (`expected_binary_sha256`), source equivalence is UNKNOWN and supervisor fails closed. | Administrator / build system must supply trusted expected SHA-256. |

---

## 4. Process-Local TCB Model & Binary Identity Binding (Items 1, 2, 3, 4)

### 4.1 Process-Local TCB Model (Item 3 & 4)
- **The entire main supervisor Python process is the Trusted Computing Base (TCB).**
- Process-local attestation (`LiveExecutionAttestation`) is an **accidental-call / misuse guard**, not cryptographic security isolation against hostile same-process Python code.
- Mutually hostile cross-process boundaries (e.g. serialized RPC) are secured via HMAC-SHA256 signatures derived from `$env:AGM_SESSION_SECRET`.

### 4.2 Mandatory Independent Expected Binary Identity Binding (Items 1 & 2)
- `TrustedAgmIdentity` configures the administrator-approved AGM binary identity.
- Production validation (`validate_refresh_evidence_supervisor`) REQUIRES a non-empty, valid 64-hex `expected_binary_sha256`.
- **Fail-Closed Behavior:**
  - Missing hash -> `BINARY_IDENTITY_UNCONFIGURED` -> `STALE_CACHED` (account not eligible).
  - Malformed hash (not 64 hex) -> `BINARY_IDENTITY_CONFIG_INVALID` -> `STALE_CACHED`.
  - Mismatched hash -> `BINARY_IDENTITY_MISMATCH` -> `STALE_CACHED`.

---

## 5. Output Privacy & Sanitization Contract (Items 6, 7, 11)

- **Sanitized DTOs:** All default CLI and supervisor outputs (`SanitizedRefreshEvidenceDTO`, `SanitizedAccountQuotaDTO`, `SanitizedVerificationOutput`) emit ONLY pseudonymous `account_ref` (`acc_<sha256_prefix>`).
- **Internal Orchestration:** Canonical email addresses (`canonical_account`) are retained strictly in-memory within trusted supervisor structures solely to pass to `agm switch <email> --target agy`.
- **Zero Token Leakage:** Capability tokens, HMAC session secrets, and OAuth token fingerprints are completely excluded from default logging and serialization.
