# T02 Adversarial Self-Review

## 1. Objective

This document subjects all T02 findings and conclusions to rigorous adversarial scrutiny. We systematically attempt to disprove our assumptions, identify edge cases, and delineate unverified boundaries.

---

## 2. Adversarial Hypotheses and Refutations

### 2.1 Hypothesis 1: "AGM Quota is always fresh and accurate"
- **Adversarial Challenge:** If the user or another process consumes quota, or if network refresh failed silently, AGM's stored quota is stale.
- **Investigation:**
  - `[VERIFIED_SOURCE]`: AGM caches quota in SQLite `quota_json`. It does NOT auto-refresh during `agm list`, `agm switch`, or `agm info`.
  - Quota is ONLY updated when `agm refresh <email>`, `agm refresh-all`, or `agm login` is executed.
  - Furthermore, Google's `fetchAvailableModels` backend may have internal caching or propagation delays (observed 15-60s in production).
- **Conclusion:** **AGM quota cannot be assumed fresh without explicit, verified live refresh.** The watchdog must enforce a freshness timestamp check (`observed_at_epoch`) and trigger a fresh `agm refresh` before making critical routing decisions.

---

### 2.2 Hypothesis 2: "Running `agm switch` guarantees the running Antigravity Desktop switches accounts"
- **Adversarial Challenge:** AGM command returns exit code 0, but the running Electron application continues using its in-memory OAuth session without reading the updated Credential Manager entry.
- **Investigation:**
  - `[VERIFIED_SOURCE]`: `agm switch --target agy` writes to Windows Credential Manager `gemini:antigravity` and exits. It does NOT notify, signal, or restart `Antigravity.exe`.
  - `[VERIFIED_SOURCE]`: `agm switch --target ide` attempts to kill `Antigravity IDE.exe` and write `state.vscdb`, but on Windows Antigravity Desktop is named `Antigravity.exe` and does not use `state.vscdb`.
  - `[VERIFIED_RUNTIME]`: Antigravity Desktop caches OAuth tokens in its running process memory. Writing to Credential Manager alone while Antigravity Desktop is running DOES NOT hot-reload the session until the application is restarted or a new turn invokes token re-authentication.
- **Conclusion:** **AGM switch exit code 0 is NOT sufficient to prove active account transition in Antigravity Desktop.** An independent post-switch verification and controlled application restart/reload are required.

---

### 2.3 Hypothesis 3: "Antigravity restart is deterministic and safe"
- **Adversarial Challenge:** Force-terminating `Antigravity.exe` (`taskkill /F`) might corrupt in-flight workspace state, dirty unsaved buffers, or leave orphan background language server processes (`node.exe`, `rg.exe`).
- **Investigation:**
  - `[OBSERVED]`: Antigravity Desktop on Windows runs multiple child helper processes (GPU process, Utility Network process, language servers).
  - Hard killing the parent Electron process without checkpointing workspace files risks uncommitted changes.
- **Conclusion:** **The watchdog supervisor must checkpoint repository state (git status/diff) before triggering application restart.**

---

### 2.4 Hypothesis 4: "Independent Active-Account Verification is 100% foolproof"
- **Adversarial Challenge:** Reading Credential Manager proves what was written to disk/vault, but does not prove what Antigravity Desktop's active renderer/agent turn is actively executing with.
- **Investigation:**
  - Windows Credential Manager inspection gives `STRONG` confidence that the external token has changed.
  - Live introspection of the token via Google userinfo endpoint (`https://www.googleapis.com/oauth2/v2/userinfo`) confirms the token belongs to the expected user email.
  - However, true in-process confirmation within Antigravity Desktop requires observing UI/CDP or turn network traffic.
- **Conclusion:** Verification evidence must be categorized as `STRONG` (Credential token + userinfo match), `MEDIUM` (Credential store match only), or `WEAK` (AGM CLI return code).

---

### 2.5 Hypothesis 5: "The selection policy can never loop infinitely"
- **Adversarial Challenge:** If 3 accounts exist and all fail sequentially, or if quota values are unstable, could the watchdog loop between them?
- **Investigation:**
  - `[VERIFIED_SOURCE]`: `selection_policy.py` implements a strict failure penalty tracking dictionary and a hard `max_rotation_attempts` cap (default: 3).
  - Upon reaching `max_rotation_attempts`, the state machine transitions immediately to the terminal state `FAILED_SAFE`.
  - When all accounts fall below threshold, it transitions to `BLOCKED_NO_ACCOUNT`.
- **Conclusion:** Finite termination is mathematically guaranteed by monotonic decrement of remaining candidate pool and hard attempt cap.

---

## 3. Unresolved Risks and Items for Main Agent Verification

1. **Hot-Reload vs Cold-Restart in Antigravity Desktop:**
   - Main orchestrator / T01 / T03 must verify whether Antigravity Desktop picks up the new Credential Manager token on the next agent turn without a full app restart, or if cold restart is strictly required.
2. **Session / Conversation Continuity Across Account Switch:**
   - Main orchestrator must verify if switching the account preserves existing conversation history in `app_storage.json` or requires re-opening the workspace thread (Gate B / Gate C).
