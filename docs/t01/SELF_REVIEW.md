# Adversarial Self-Review (T01 Amended)

## Critical Review & Honest Evidence Gap Analysis

### 1. Evidence Gaps and Limitations
- **No Live Controlled Restart Cycles**: A forced multi-cycle kill and cold restart experiment was NOT performed in this session to preserve the subagent's execution environment. Persistence claims rely on observed historical timestamps across logged restarts in `main.log` and are properly classified as `OBSERVED` / `INFERENCE`.
- **No Live Account Switch Executed**: T01 did not perform a live Google credential swap. `LOCAL_DATA_PRESENT_IN_CURRENT_PROFILE` is verified, but whether the active UI conversation survives an account token rotation remains `UNKNOWN` pending T02 integration tests.
- **Protobuf Wire Schema Lack Official Definitions**: The mappings in `agyhub_summaries_proto.pb` were reverse-engineered by generic wire tag parsing and cross-referenced with SQLite data. They are classified as `OBSERVED` / `INFERENCE` rather than authoritative source.

### 2. Single-Session vs Multi-Session Observations
- The 3 process snapshots in `scripts/t01/inspect_process_forensics.py` were captured 2 seconds apart within a single running instance. They establish runtime consistency within an active session, but do NOT constitute independent multi-session reproductions.

### 3. False Positive Vulnerabilities
- **Quota Error vs Generic Errors**: Matching generic `RESOURCE_EXHAUSTED` or `429` causes severe false positives (backend capacity issues, per-minute rate limits). Quota detection has been tightened to require the full substring `Individual quota reached. Please upgrade your subscription to increase your limits. Resets in <duration>`.
- **Inactivity Signal**: Lack of process or disk activity is a WEAK signal and must never be used alone to infer quota exhaustion.

### 4. Identity & Collision Risk Realism
- Collision risk for `cascade_id` cannot mathematically be claimed as "zero". It is more accurately characterized as "negligible / very low", supported by the standard 128-bit RFC 4122 v4 UUID structure observed across all 9 local conversation databases.

### 5. Summary of Downgraded Claims
| Original Claim | Downgraded Classification | Rationale |
| :--- | :--- | :--- |
| Restart persistence is VERIFIED_RUNTIME | `OBSERVED` / `INFERENCE` | Inferred from historical restart logs and file timestamps; no forced kill executed. |
| Account switch persistence is VERIFIED_RUNTIME | `UNKNOWN` (`PERSISTS_ACROSS_ACCOUNT_SWITCH`) | No live account switch performed by T01. |
| Protobuf semantic fields are VERIFIED_RUNTIME | `OBSERVED` / `INFERENCE` | Heuristic reverse-engineering without published `.proto` schema. |
| Collision risk is None | Negligible / Very Low | RFC 4122 v4 UUID format observed, but zero is an absolute claim. |
