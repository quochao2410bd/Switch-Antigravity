# T02 Security Audit and Findings: AGM Integration (Zero-Trust Revision)

## 1. Executive Summary

This document details the security model, credential storage architecture, and sandbox limitations of **AGM (Antigravity Manager)** for the **Switch-Antigravity** watchdog layer on Windows.

---

## 2. Credential Storage Architecture & Sandboxing Gap

### 2.1 Host OS Credential Manager (`gemini:antigravity`)
- **Target Name:** `gemini:antigravity`
- **Account / User:** `antigravity`
- **Persistence:** `CRED_PERSIST_LOCAL_MACHINE` (Type 2 Generic).
- **Blob Payload:** Plaintext JSON containing Google OAuth `access_token` (`ya29...`) and `refresh_token` (`1//0...`).
- **Critical Security Finding (Sandbox Gap):**
  - Setting `AGM_DATA_DIR` isolates the SQLite database (`cloud_accounts.db`) and the master key (`.mk`), but **DOES NOT** isolate the Windows Credential Manager.
  - Calling `agm switch --target agy` immediately writes into the host user's live Windows Credential Manager target `gemini:antigravity`.
  - **Rule for Supervisor & Tests:** All synthetic testing must use mock credential vaults (`mock_payload`) or sandboxed credential shims. Synthetic tests must never invoke `agm switch --target agy` on the host OS.

### 2.2 Master Key (`.mk`)
- **Location:** `~/.antigravity-agent/.mk` (or `$AGM_DATA_DIR\.mk`).
- **Format:** Plaintext 64-character hexadecimal representation of a 32-byte (256-bit) AES key.
- **Permissions:** Written with POSIX mode `0600` (NTFS user ACLs).
- **Risk:** Stored in plaintext hex; anyone with read access to the directory can decrypt the entire local SQLite store.

### 2.3 SQLite Database (`cloud_accounts.db`)
- **Location:** `~/.antigravity-agent/cloud_accounts.db`.
- **Encryption:** `token_json` and `quota_json` fields are encrypted with AES-256-GCM (`agm_enc_v1:<iv>:<tag>:<ct>`).
- **Risk:** Because `.mk` is located in the same directory by default, directory-level compromise decrypts all tokens.

---

## 3. Dangerous Commands Never to Invoke Automatically

The automated supervisor must **NEVER** invoke the following AGM commands:

| Command | Danger / Reason |
|---------|-----------------|
| `agm login` / `agm add` | Launches interactive browser OAuth, opens local ports 8888-8892, blocks execution for up to 5 minutes waiting for user input. |
| `agm remove` / `agm rm` | Permanently deletes account from store; interactive prompt (`[y/N]`). |
| `agm unalias` | Deletes user aliases; configuration loss. |
| `agm export` | Writes sensitive token structures to an unencrypted file path if misconfigured. |
| `agm import-backup` | Overwrites or alters local account store from arbitrary JSON input. |
| `agm watch` | Enters an infinite live monitoring loop with ANSI terminal clear escapes; blocks watchdog supervisor. |
| `agm auto-switch` | Interactive prompt (`Switch to this account? [y/N]: `); non-deterministic logic that ignores supervisor cooldown and terminal states. |

---

## 4. Prohibited Commits and Secret Leakage Prevention

The following files and patterns must **NEVER** be committed to Git:

1. `**/.antigravity-agent/**`
2. `**/.mk` or any 64-character hex master key strings.
3. `**/cloud_accounts.db` and SQLite database backups (`*.db`, `*.db-wal`, `*.db-shm`).
4. `**/accounts_backup*.json` or exported account files.
5. Raw Google OAuth tokens:
   - `ya29.*` (Google Access Tokens)
   - `1//0.*` (Google Refresh Tokens)
   - `eyJ.*` (Google ID Tokens / JWTs)
6. Decrypted credential blobs from Windows Credential Manager.
