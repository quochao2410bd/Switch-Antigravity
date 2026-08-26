# Adversarial Self-Review (T01 Round 4)

## Critical Review & Honest Gap Analysis

### 1. Mandatory Baseline in `poll_new_events()` Function
- **Defect in Round 3**: `poll_new_events()` checked `if not target_path or not os.path.exists(target_path)` before checking baseline validity, allowing `baseline=None` with missing log to return `LOG_UNAVAILABLE` (exit 3) instead of `BASELINE_REQUIRED` (exit 5).
- **Correction**: `poll_new_events()` immediately checks `if not isinstance(baseline, dict) or not baseline:` and returns `BASELINE_REQUIRED` (exit 5) prior to any filesystem inspection. Verified with unit tests.

### 2. Session & PID Binding Enforcement
- **Defect in Round 3**: Caller omission of `current_ls_pid` or `session_id` bypassed baseline validation gates.
- **Correction**: If `language_server_process_id` or `supervisor_session_id` are bound in baseline, omission by caller returns `BASELINE_INVALID` with `rebaseline_required: True` (exit 2).

### 3. Exact CDP Page Target Parsing
- **Defect in Round 3**: Regex matching allowed prefix/suffix partial paths and did not filter non-page targets strictly.
- **Correction**: Filter strictly by `type == "page"`, parse pathname, match exact regex `^/c/([0-9a-fA-F-]{36})$`, and perform exact UUID string equality. Tested with negative synthetic targets.

### 4. Event ID & SHA-256 Binding to Complete Record Bytes
- **Defect in Round 3**: `event_sha256` hashed metadata string `ino:start:end:429:resets_in`, creating collision risk for different records with same length and reset duration.
- **Correction**: Hashed complete record bytes (`hashlib.sha256(line_bytes).hexdigest()`), producing distinct `event_id` and `event_sha256` for different record contents. Verified with unit test.

### 5. Deterministic Test Independence from Live Historical Logs
- **Defect in Round 3**: Test suite asserted live log contained `>=20` historical quota events, which is brittle across machine resets or log rotations.
- **Correction**: Test suite is 100% deterministic using synthetic fixtures. Live log check only inspects structural semantics (`isinstance(total_matches, int)`).

### 6. Evidence Classification Matrix
| Area | Evidence Class | Verified Scope | UNKNOWN / Untested Boundary |
| :--- | :--- | :--- | :--- |
| Process Forensics | `VERIFIED_LIVE_RUNTIME` | Electron main, GPU, network utility, renderer, and language_server process tree. | Long-term port stability across days. |
| Quota Signature | `VERIFIED_LIVE_RUNTIME` | Exact `(code 429)` signature identified in live `language_server.log`. | Non-standard quota strings. |
| Production Detector Lifecycle | `UNIT_TEST` / `SYNTHETIC_SIMULATION` | Binary seek, split writes, truncation, replacement, replay IDs, crash replay deduplication. | Live incremental capture while watchdog is running (`NOT_LIVE_TESTED`). |
| Local Identity Correlation | `VERIFIED_LIVE_RUNTIME` | 9/9 DB, table, brain path, and proto index matching. | UI visibility after multi-account swap (`UNKNOWN`). |
| Active CDP Target Match | `VERIFIED_LIVE_RUNTIME` | Exactly 1 active page target matched 1 cascade ID (1/1 eligible targets). | Multi-window CDP routing. |
| App Restart Persistence | `OBSERVED` / `INFERENCE` | Inferred from historical restart timestamps in `main.log`. | Controlled multi-cycle forced process kill (`NOT_LIVE_TESTED`). |
| Account-Switch Survival | `UNKNOWN` | Local files exist in current user profile. | Live account rotation not executed by T01. |
| Repository Privacy Audit | `STATIC_INSPECTION_AUDIT` | Automated regex audit over all T01 files. | Uninspected external system state. |
