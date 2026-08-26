# Adversarial Self-Review (T01 Round 2)

## Critical Review & Honest Gap Analysis

### 1. Incremental vs Historical Quota Detection
- **Initial Flaw**: The original detector parsed historical logs globally, which caused any historical quota event to indicate quota exhaustion in active sessions.
- **Correction**: Implemented byte-offset baselines (`create_baseline`, `poll_new_events`). Verified that stale historical quota errors before baseline return `NO_NEW_EVENT`, while newly appended quota errors return `NEW_CONFIRMED_QUOTA_EVENT` exactly once.

### 2. Correlation Scope Precision
- **Initial Flaw**: Claimed 9/9 5-way correlation including CDP. In reality, CDP only reflects the single active foreground conversation.
- **Correction**: Re-scoped to 9/9 Local Four-Way Correlation (DB filename + `trajectory_meta` + brain dir + proto index) and 1/1 Active Foreground CDP URL cross-check.

### 3. Strict Code 429 Contract Enforcement
- **Initial Flaw**: Regex accepted any numeric error code.
- **Correction**: Enforced exact `(code 429)` in regex and added a negative test verifying rejection of `(code 503)`.

### 4. UUID Encoding Observation
- **Precision**: Validated that all 9 observed cascade IDs conform to UUID version 4 RFC 4122 encoding (`9/9 valid UUIDs, 9/9 version 4, 9/9 RFC 4122 variant`). This is an empirical runtime observation, not source proof of generation algorithm.

### 5. Summary of Claims & Boundaries
| Area | Evidence Class | Verified Scope | UNKNOWN / Untested Boundary |
| :--- | :--- | :--- | :--- |
| Process Forensics | `VERIFIED_RUNTIME` | Electron main, GPU, network utility, renderer, and language_server process tree. | Long-term port stability across days. |
| Incremental Quota Detector | `VERIFIED_RUNTIME` | Offset-based new event detection, truncation detection, strict code 429 matching. | Unobserved non-standard quota strings. |
| Local Identity Correlation | `VERIFIED_RUNTIME` | 9/9 DB filename, table, brain path, and proto index matching. | UI visibility after multi-account swap (`UNKNOWN`). |
| App Restart Persistence | `OBSERVED` / `INFERENCE` | Inferred from historical restart timestamps in `main.log`. | Controlled multi-cycle forced process kill. |
| Account-Switch Survival | `UNKNOWN` | Local files exist in current user profile. | Live account rotation not executed by T01. |
