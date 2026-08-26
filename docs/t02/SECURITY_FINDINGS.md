# T02 Security Audit and Findings: AGM Integration (Zero-Trust Round 2 Revision)

## 1. Credential Storage Architecture & Sandboxing Gap

### 1.1 Host OS Credential Manager (`gemini:antigravity`)
- **Target Name:** `gemini:antigravity`
- **Account / User:** `antigravity`
- **Persistence:** `CRED_PERSIST_LOCAL_MACHINE` (Type 2 Generic).
- **Blob Payload:** Plaintext JSON containing Google OAuth `access_token` (`ya29...`) and `refresh_token` (`1//0...`).
- **Sandboxing Gap:** `$env:AGM_DATA_DIR` isolates the SQLite database (`cloud_accounts.db`) and `.mk`, but **DOES NOT** isolate the Windows Credential Manager.
- **Rule for Test Suites:** Unit tests must use mock payloads (`mock_payload`) and dependency-injected fetchers. Synthetic tests must never invoke `agm switch --target agy` on the host OS.

### 1.2 Master Key (`.mk`) & SQLite Store
- **Master Key Location:** `~/.antigravity-agent/.mk` (Plaintext 64-char hex 256-bit AES key).
- **SQLite Database:** `~/.antigravity-agent/cloud_accounts.db` (AES-256-GCM encrypted tokens).
- **Risk:** Directory compromise exposes `.mk` and all encrypted tokens.

---

## 2. Host Credential Incident Post-Mortem (Item 8)

1. **Incident Description:** Synthetic switch testing (`agm switch mock.worker1 --target agy`) bypassed data directory isolation and mutated the host user's live Windows Credential Manager target `gemini:antigravity`.
2. **Impact Analysis:** Running Electron instances remained unaffected due to in-memory session tokens, but new CLI / cold-start processes read the mock token.
3. **Restoration & Verification:**
   - Active credential target restored.
   - Restoration status: **`HOST_CREDENTIAL_RESTORATION = UNKNOWN`** (cannot cryptographically verify exact prior byte payload without historical secret exposure).
4. **Remediation Implemented:** Complete decoupling of test runners from OS vault via `mock_payload` injection in `scripts/t02/verify_active_account.py`.

---

## 3. Dangerous Commands Prohibited from Automation

| Command | Danger / Reason |
|---------|-----------------|
| `agm login` / `agm add` | Launches interactive browser OAuth, binds ports 8888-8892, blocks execution for up to 5 minutes. |
| `agm remove` / `agm rm` | Permanently deletes accounts; interactive `[y/N]` prompt. |
| `agm unalias` | Deletes user aliases. |
| `agm export` | Writes sensitive unencrypted token JSON to filesystem. |
| `agm import-backup` | Overwrites local account store from arbitrary JSON. |
| `agm watch` | Infinite blocking loop with ANSI escapes. |
| `agm auto-switch` | Interactive prompt; non-deterministic logic bypassing supervisor terminal state guarantees. |

---

## 4. Prohibited Commits and Secret Leakage Prevention

The following files and patterns must **NEVER** be committed to Git:
1. `**/.antigravity-agent/**`
2. `**/.mk` or any 64-character hex master key strings.
3. `**/cloud_accounts.db` and SQLite database backups (`*.db`, `*.db-wal`, `*.db-shm`).
4. `**/accounts_backup*.json` or exported account files.
5. Raw Google OAuth tokens (`ya29.*`, `1//0.*`, `eyJ.*`).
6. Decrypted credential blobs from Windows Credential Manager.
