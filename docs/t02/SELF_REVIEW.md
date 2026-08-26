# T02 Adversarial Self-Review (Zero-Trust Revision)

## 1. Adversarial Scrutiny of Findings

### 1.1 Quota Freshness Provenance
- **Initial Flaw:** Assigning `observed_at_epoch = time.time()` when parsing cached table output falsely refreshed old data.
- **Correction:** Quotas now strictly require runtime binding to explicit `REFRESH_CONFIRMED_AT` timestamps. Cached data without recent refresh is classified as `STALE_CACHED` and rejected by the selection policy.

### 1.2 Credential Store vs Desktop Active State
- **Initial Flaw:** Calling Windows Credential Manager the "Desktop active state".
- **Correction:** Credential store writes are verified as `CREDENTIAL_STORE_IDENTITY_VERIFIED`. True Desktop adoption is downgraded to `UNKNOWN` until in-process turn/session evidence is established by T03/integration.

### 1.3 `agy` vs Desktop Target
- **Initial Flaw:** Conflating `target.Agy` (`Antigravity CLI (agy)`) with Desktop support.
- **Correction:** Upstream source explicitly defines `Agy` as CLI. While both products read `gemini:antigravity`, CLI credential update does not automatically trigger Desktop in-memory hot reload.

### 1.4 Restart Safety
- **Initial Flaw:** Claiming cold restart is verified and required while force-killing `Antigravity.exe`.
- **Correction:** Cold restart requirement is downgraded to `INFERENCE / UNKNOWN`. Force-kill is flagged as `EXPERIMENTAL_UNSAFE`. The supervisor must not force-kill without T01 workspace checkpointing.

### 1.5 Credential Manager Sandboxing Gap
- **Initial Flaw:** Believing `$env:AGM_DATA_DIR` fully sandboxes AGM.
- **Correction:** Documented that `credstore.WriteToken()` operates globally on the user logon session's Windows Credential Manager. All test suites now use mock payload injection.

---

## 2. Unresolved Risks and Items for Integration Review

1. **Desktop Adoption of Token:** Must be verified in integration with T03 by executing a real model turn after switch.
2. **Conversation Continuity:** Supervisor must preserve active conversation IDs across restart (Gate B).
3. **Workspace Checkpoint before Restart:** Supervisor must execute `git status` / diff check before any application restart.
