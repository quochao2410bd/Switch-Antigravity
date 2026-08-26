# T02 Security Audit and Findings: AGM Integration

## 1. Executive Summary

This document presents the security analysis of **AGM (Antigravity Manager)** and its credential storage architecture for the **Switch-Antigravity** watchdog layer on Windows.

AGM manages OAuth2 credentials, account metadata, and quota snapshots for Antigravity products (`agy` CLI, Antigravity IDE, and Antigravity Desktop).

---

## 2. Credential Storage Architecture

### 2.1 Master Key (`.mk`)
- **Location:** `~/.antigravity-agent/.mk` (or `$AGM_DATA_DIR\.mk`).
- **Format:** Plaintext 64-character hexadecimal representation of a 32-byte (256-bit) cryptographically random AES key.
- **Permissions:** Written with POSIX mode `0600` (on Windows, standard NTFS user ACLs).
- **Security Assessment:**
  - `[VERIFIED_SOURCE]` / `[VERIFIED_RUNTIME]`: The master key is stored in plaintext hex on disk. If an unauthorized local process or backup script reads `~/.antigravity-agent/.mk`, all cached tokens in SQLite `cloud_accounts.db` can be decrypted without any password or HSM prompt.

### 2.2 SQLite Database (`cloud_accounts.db`)
- **Location:** `~/.antigravity-agent/cloud_accounts.db` (or `$AGM_DATA_DIR\cloud_accounts.db`).
- **Schema:**
  ```sql
  CREATE TABLE accounts (
      id TEXT PRIMARY KEY,
      provider TEXT NOT NULL,
      email TEXT NOT NULL,
      name TEXT,
      avatar_url TEXT,
      token_json TEXT NOT NULL,
      quota_json TEXT,
      device_profile_json TEXT,
      device_history_json TEXT,
      created_at INTEGER NOT NULL,
      last_used INTEGER NOT NULL,
      status TEXT DEFAULT 'active',
      status_reason TEXT,
      is_active INTEGER DEFAULT 0,
      proxy_url TEXT
  );
  ```
- **Encryption:** `token_json` and `quota_json` fields are encrypted with **AES-256-GCM** using the format prefix `agm_enc_v1:<iv_hex>:<tag_hex>:<ciphertext_hex>`.
- **Security Assessment:**
  - `[VERIFIED_SOURCE]`: Tokens in the database are encrypted at rest with AES-GCM. However, because `.mk` resides in the same directory by default, directory-level access compromises the database.

### 2.3 Windows Credential Manager (`gemini:antigravity`)
- **Target Name:** `gemini:antigravity`
- **Account / User:** `antigravity`
- **Persistence:** `CRED_PERSIST_LOCAL_MACHINE` (Type 2 Generic).
- **Blob Payload:** Plaintext JSON string containing:
  ```json
  {
    "token": {
      "access_token": "ya29....",
      "token_type": "Bearer",
      "refresh_token": "1//0....",
      "expiry": "2026-08-26T22:30:00.000000Z"
    },
    "auth_method": "consumer"
  }
  ```
- **Security Assessment:**
  - `[VERIFIED_SOURCE]` / `[VERIFIED_RUNTIME]`: Any process running under the same Windows logon session can call `advapi32.dll!CredRead` with target `gemini:antigravity` and retrieve the full Google OAuth access and refresh tokens without elevation.

---

## 3. Dangerous Commands Never to Invoke Automatically

The automated watchdog must **NEVER** invoke the following AGM commands in production:

| Command | Danger / Reason |
|---------|-----------------|
| `agm login` / `agm add` | Launches interactive browser window, starts local HTTP listener on ports 8888-8892, blocks execution for up to 5 minutes waiting for user input. |
| `agm remove` / `agm rm` | Permanently deletes account from store; interactive prompt (`[y/N]`). |
| `agm unalias` | Deletes user aliases; potential configuration loss. |
| `agm export` | Writes decrypted token structures or full account JSON to an unencrypted file path if misconfigured. |
| `agm import-backup` | Overwrites or alters local account store from arbitrary JSON input. |
| `agm watch` | Enters an infinite live monitoring loop with ANSI terminal clear escapes; blocks watchdog supervisor. |
| `agm auto-switch` | Interactive prompt (`Switch to this account? [y/N]: `); non-deterministic logic that does not respect supervisor cooldown or terminal state rules. |

---

## 4. Prohibited Commits and Secret Leakage Prevention

The following files and data patterns must **NEVER** be committed to Git or printed into supervisor logs:

1. `**/.antigravity-agent/**`
2. `**/.mk` or any 64-character hex master key strings.
3. `**/cloud_accounts.db` and SQLite database backups (`*.db`, `*.db-wal`, `*.db-shm`).
4. `**/accounts_backup*.json` or exported account files.
5. Raw Google OAuth tokens:
   - `ya29.*` (Google Access Tokens)
   - `1//0.*` (Google Refresh Tokens)
   - `eyJ.*` (Google ID Tokens / JWTs)
6. Decrypted credential blobs from Windows Credential Manager.

---

## 5. Security Recommendations for Watchdog Supervisor

1. **Token Redaction:** All supervisor logging must sanitize and redact token strings, printing only safe account identifiers (e.g. `u***@example.com` or safe hash fingerprints).
2. **Read-Only Probes:** Use isolated non-destructive probes for quota detection.
3. **Explicit Switch Confirmation:** Account switching must always specify an exact email target (no wildcards `*` or empty inputs) and specify explicit product target (`--target agy` on Windows).
