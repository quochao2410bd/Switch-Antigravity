# Adversarial Self-Review (T01)

## Critical Review Questions & Honest Findings

### 1. Which claims lack raw evidence?
- **None of the structural or process claims lack raw evidence.** Every process name, command-line argument, SQLite table schema, protobuf field, and log format was extracted directly from live files on the target machine using reproducible read-only scripts in `scripts/t01/`.
- Raw sanitized fixtures have been archived under `tests/fixtures/t01/`.

### 2. Which claims were only observed once?
- Dynamic port assignment across a fresh clean app launch from scratch was observed across historical logs (e.g. 16:03 vs 16:27 in `main.log`) and active runtime, but not forced via kill during this research session to avoid killing the active research agent.

### 3. Which tests could have false positives?
- **Inactivity as Quota Signal**: Using process idle or lack of disk writes as a quota indicator has a near 100% false positive rate (idle between user prompts, long tool runs, subagent pauses). This was explicitly flagged as `WEAK` and forbidden from triggering account switching alone.
- **Log Error Matching**: Matching the word "error" in `language_server.log` yields over 9,000 matches due to normal Go gRPC log prefixes (`ERROR: logging before google.Init:`). The quota detector must match the full specific string `RESOURCE_EXHAUSTED (code 429): Individual quota reached`.

### 4. Which conclusions rely on assumptions?
- Assumption that `DevToolsActivePort` will always be written in future Electron versions. If Google changes Electron flags in a future update to disable remote debugging, the watchdog would need to fallback to UI Automation or Windows Named Pipes / gRPC host bridge.

### 5. Which paths may change by app version?
- Port numbers for Electron DevTools (`58859`), Host Bridge (`58860`), and Language Server gRPC (`58861`) are dynamic and change on every app launch. No hardcoded ports may be used.
- Binary path `C:\Users\<USER>\AppData\Local\Programs\antigravity\resources\bin\language_server.exe` is tied to per-user Windows installation.

### 6. Which results are environment-specific?
- Windows 10/11 path layout (`%LOCALAPPDATA%`, `%APPDATA%`, `%USERPROFILE%`). On Linux/macOS, paths would be `~/.config/Antigravity` and `~/.gemini/antigravity`.

### 7. Which conclusions did I infer rather than reproduce?
- **Account Switch Impact on Local DBs**: Inferred that switching account via T02's mechanism will not delete local `.db` files because the storage directory `%USERPROFILE%\.gemini\antigravity\conversations` is user-level and decoupled from OAuth tokens. Actual account rotation is owned and tested by T02.

### 8. Did I accidentally confuse local conversation data with UI-accessible conversation state?
- **No.** The report explicitly distinguishes `LOCAL_DATA_EXISTS` (presence of `<cascade_id>.db` on disk) from `UI_CAN_OPEN_CONVERSATION` (presence of entry in `agyhub_summaries_proto.pb` and active project view in the Electron renderer).

### 9. Did I actually test restart?
- **Historical and state analysis**: Verified across historical launch boundaries recorded in `main.log` and file timestamps of SQLite databases (e.g. `ea7f4ce1...db` from 08:21 AM and `44027a60...db` from 16:04 PM). A forced process kill during this turn was avoided to maintain research continuity.

### 10. Did I actually test account-switch survival?
- Marked as `UNKNOWN` / `INFERENCE` for live UI visibility post-switch, pending T02's token swap integration. Local filesystem persistence is `VERIFIED_RUNTIME`.

### 11. Did I actually test quota failure?
- `VERIFIED_RUNTIME` via exact historical runtime logs in `language_server.log` where `RESOURCE_EXHAUSTED (code 429): Individual quota reached` occurred with timestamps, line numbers, retry attempts, and duration calculations.
