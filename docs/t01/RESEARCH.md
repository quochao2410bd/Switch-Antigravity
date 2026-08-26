# Antigravity Desktop State and Conversation Forensics (T01 Research Report)

## Executive Summary

This research establishes the exact runtime architecture, process tree, local filesystem storage schema, conversation identity system, and quota failure observability mechanisms for **Antigravity Desktop 2.10.0** on Windows. All findings have been updated with stateful incremental quota event detection, 4-way local conversation correlation + 1-way active CDP target cross-check, strict code 429 enforcement, sanitized synthetic fixtures, and precise evidence classifications.

---

## 1. Process Forensics (Task A)

### Classification: `VERIFIED_RUNTIME` (within single running session)
- **Main Executable**: `Antigravity.exe`
- **Installation Path**: `C:\Users\<USER>\AppData\Local\Programs\antigravity\Antigravity.exe`
- **App Data Directory**: `C:\Users\<USER>\AppData\Roaming\Antigravity`
- **Agent/Hub Data Directory**: `C:\Users\<USER>\.gemini\antigravity`
- **App Package Path**: `C:\Users\<USER>\AppData\Local\Programs\antigravity\resources\app.asar`
- **Core RPC Binary**: `C:\Users\<USER>\AppData\Local\Programs\antigravity\resources\bin\language_server.exe`

### Observed Process Tree & Roles
```text
Antigravity.exe (Main Browser Process, PID: 10000)
  |-- Antigravity.exe (--type=gpu-process, PID: 10001)
  |-- Antigravity.exe (--type=utility --utility-sub-type=network.mojom.NetworkService, PID: 10002)
  |-- Antigravity.exe (--type=renderer, PID: 10003) [Electron Frontend UI]
  \-- language_server.exe (PID: 10004) [Agent Orchestrator & Backend RPC Service]
        |-- conhost.exe
        \-- pwsh.exe / cmd.exe [Tool execution workers]
```
*(Sanitized fixture: `tests/fixtures/t01/sample_process_tree.json`)*

### Localhost Services & Ports (`VERIFIED_RUNTIME`)
1. **Chrome DevTools Protocol (CDP)**:
   - Port dynamically assigned and recorded in `%APPDATA%\Antigravity\DevToolsActivePort` (e.g. `58859`).
   - Endpoint: `ws://127.0.0.1:<port>/devtools/browser/<synthetic_uuid>`
   - HTTP Target Discovery: `http://127.0.0.1:<port>/json/list` and `/json/version`
   - Active Target URL format: `https://127.0.0.1:<ls_port>/c/<cascade_id>?section=<project_id>`
   *(Sanitized fixture: `tests/fixtures/t01/sample_cdp_targets.json`)*
2. **Host Bridge Server**:
   - Hosted by Electron main process on `http://127.0.0.1:58860` (spawn arg: `--host_bridge_url=http://127.0.0.1:58860`).
   *(Sanitized fixture: `tests/fixtures/t01/sample_host_bridge_log.txt`)*
3. **Language Server gRPC / HTTPS Service**:
   - Hosted by `language_server.exe` on dynamic port (e.g. `https://127.0.0.1:58861/`).
4. **Language Server HTTP Service**:
   - Hosted on secondary dynamic port (e.g. `http://127.0.0.1:58862`).

---

## 2. Filesystem and Local State (Task B)

### Classification: `VERIFIED_RUNTIME`

### Primary Storage Locations
| Path | File / Dir | Role |
| :--- | :--- | :--- |
| `%APPDATA%\Antigravity\app_storage.json` | JSON File | UI state, last selected project (`new-convo-last-selected-project`), pane tabs, preferences. |
| `%APPDATA%\Antigravity\DevToolsActivePort` | Text File | Remote debugging port and browser WS path. |
| `%APPDATA%\Antigravity\logs\main.log` | Log File | Electron lifecycle, updater, language_server spawn arguments and dynamic ports. |
| `%APPDATA%\Antigravity\logs\language_server.log` | Log File | Language server internal logs, RPC events, API requests/responses, and quota/auth errors. |
| `%USERPROFILE%\.gemini\antigravity\antigravity_state.pbtxt` | Text Proto | Installation UUID, onboarding flags, model preferences. |
| `%USERPROFILE%\.gemini\antigravity\agyhub_summaries_proto.pb` | Binary Proto | Global index of all conversations, titles, message counts, workspace roots, git branches, timestamps, and project associations. |
| `%USERPROFILE%\.gemini\antigravity\conversations\<cascade_id>.db` | SQLite DB | Durable conversation store per conversation ID. |
| `%USERPROFILE%\.gemini\antigravity\brain\<cascade_id>\` | Directory Tree | Conversation transcripts (`transcript.jsonl`), subagent logs, task outputs, and artifacts. |

### SQLite Conversation Schema (`<cascade_id>.db`)
- `trajectory_meta`: `trajectory_id` (TEXT, UUID Primary Key), `cascade_id` (TEXT, UUID), `trajectory_type` (INTEGER), `source` (INTEGER).
- `steps`: `idx` (INTEGER Primary Key), `step_type`, `status`, `has_subtrajectory`, `metadata`, `error_details`, `permissions`, `task_details`, `render_info`, `step_payload`, `step_format`.
- `trajectory_metadata_blob`: `id` (TEXT DEFAULT 'main'), `data` (BLOB serialized protobuf with workspace URI, branch, project UUID).
*(Sanitized fixture: `tests/fixtures/t01/sample_trajectory_meta.json`)*

---

## 3. Conversation Identity & Correlation Analysis (Task C)

### Candidate Comparison Matrix

| Candidate Identifier | Source / Location | Stability Across Restart | Collision Risk | Confidence | Recommendation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`cascade_id` (Conversation UUID)** | `<id>.db`, `brain/<id>`, proto index, URL `/c/<id>` | **Durable (Disk)** | **Negligible / Very Low** (All 9 observed conform to UUID v4 RFC 4122) | `VERIFIED_RUNTIME` | **PRIMARY CANONICAL ID** |
| **`trajectory_id`** | `trajectory_meta` DB table, proto field 4 | Durable (Disk) | Negligible / Very Low (UUID v4) | `VERIFIED_RUNTIME` | Secondary / Verification |
| **`project_id` / Section UUID** | `agyhub_summaries_proto.pb`, `app_storage.json` | Durable (Disk) | Low | `VERIFIED_RUNTIME` | Scope Container |
| **Conversation Title** | Proto field 1, DOM `<title>` | Durable (Disk) | **HIGH (Mutable/Duplicate)** | `VERIFIED_RUNTIME` | Non-Unique Display Only |
| **Workspace URI / Git Branch** | `trajectory_metadata_blob`, proto field 17 | Durable (Disk) | Medium (multiple convos per repo) | `VERIFIED_RUNTIME` | Context Filter |
| **CDP Target ID** | `/json/list` `id` field | Ephemeral (Session) | High across restarts | `VERIFIED_RUNTIME` | Session-only handle |

### Correlation Evidence
1. **Local Four-Way Correlation (`VERIFIED_RUNTIME`)**:
   - Script: `scripts/t01/correlate_cascade_id.py`
   - Verified across all 9 local conversation databases (9/9 matches):
     - DB Filename: `<cascade_id>.db`
     - SQLite Table: `SELECT cascade_id FROM trajectory_meta`
     - Brain Directory: `brain/<cascade_id>/`
     - Protobuf Index: `agyhub_summaries_proto.pb` (Field 17 Subfield 6)
2. **Active Foreground CDP Target Correlation (`VERIFIED_RUNTIME`)**:
   - Active renderer URL in CDP (`/c/<cascade_id>?section=<project_id>`) matched the currently open foreground conversation (1/1 active targets).
*(Sanitized fixture: `tests/fixtures/t01/sample_cross_correlation.json`)*

---

## 4. Restart & Persistence Dynamics (Task D)

### Classification: `OBSERVED` / `INFERENCE`
- Historical persistence confirmed across logged launches in `main.log` (08:21 AM, 16:03 PM, 16:27 PM).
- SQLite databases created earlier in the day remained intact and resumed active modifications.
- **Limitation**: Controlled multi-cycle forced termination and restart experiments were NOT performed during this turn to preserve active subagent communication. Direct runtime verification of controlled restart is deferred to future integration testing.

---

## 5. Account-Switch Survival (Task E)

### Classification:
- `LOCAL_DATA_PRESENT_IN_CURRENT_PROFILE`: **`VERIFIED_RUNTIME`** (All 9 `.db` databases and `brain/` folders exist locally in `%USERPROFILE%\.gemini\antigravity\`).
- `PERSISTS_ACROSS_ACCOUNT_SWITCH`: **`UNKNOWN`** (Inferred to persist, but live credential swap was not executed).
- `UI_CAN_OPEN_CONVERSATION`: **`UNKNOWN`** (Pending T02 live account rotation test).

---

## 6. Incremental Quota Failure Detection (Task F & G)

### Exact Quota Exhaustion Signature (`VERIFIED_RUNTIME`)
Strictly requires code 429 and exact individual quota message:
```text
ERROR: logging before google.Init: E0826 17:40:12.155991  181166 errorreport.go:223] agent executor error: calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 3h24m48s.
```

### Stateful Incremental Detector Architecture (`scripts/t01/quota_detector.py`)
To prevent old historical quota events from triggering recovery in active sessions:
1. **`create_baseline(log_path, ls_pid)`**: Captures file size offset, mtime, and PID at start of session.
2. **`poll_new_events(baseline, log_path)`**:
   - Reads strictly bytes appended after `baseline.byte_offset`.
   - Returns `NEW_CONFIRMED_QUOTA_EVENT` (exit code 0) if and only if an exact individual quota error was written after baseline.
   - Returns `NO_NEW_EVENT` (exit code 1) if no new quota events occurred.
   - Returns `BASELINE_INVALID` (exit code 2) if log file was truncated/rotated (size < saved offset).
   - Returns `LOG_UNAVAILABLE` (exit code 3) if log file is missing.

### Test Suite Results (`scripts/t01/test_quota_detector.py`)
- `PASS`: Scenario 1 - Stale historical quota event before baseline -> `NO_NEW_EVENT`
- `PASS`: Scenario 2 - Normal non-quota lines appended -> `NO_NEW_EVENT`
- `PASS`: Scenario 3 - One new exact quota line appended -> `NEW_CONFIRMED_QUOTA_EVENT`
- `PASS`: Scenario 4 - Re-polling same file without changes -> `NO_NEW_EVENT`
- `PASS`: Scenario 5 - Second new quota event appended -> `NEW_CONFIRMED_QUOTA_EVENT`
- `PASS`: Scenario 6 - Log truncation / rotation -> `BASELINE_INVALID` (`rebaseline_required=True`)
- `PASS`: Scenario 7 - Strict code 429 enforcement (rejects code 503 fixture)
- `PASS`: Scenario 8 - Generic non-quota RESOURCE_EXHAUSTED rejected
- `PASS`: Scenario 9 - Generic 429 RPM rate limit rejected
- `PASS`: Scenario 10 - Live `language_server.log` historical diagnostic scan

---

## 7. Sanitization Policy & Synthetic Fixtures

All committed test fixtures and documentation utilize deterministic synthetic UUID mappings:
- `SYNTH_CASCADE_ID`: `00000000-0000-4000-8000-000000000001`
- `SYNTH_TRAJECTORY_ID`: `10000000-0000-4000-8000-000000000001`
- `SYNTH_PROJECT_ID`: `90000000-0000-4000-8000-000000000001`

All environment paths are redacted (`C:\Users\<USER>`), real conversation titles replaced with generic task names, and all tokens/CSRF values replaced with `<REDACTED_TOKEN>`.

---

## 8. Five Most Important Claims: CLAIM -> EVIDENCE -> REPRO COMMAND

| Claim | Claim Details & Class | Sanitized Evidence File | Independent Reproduction Command |
| :--- | :--- | :--- | :--- |
| **1. Process Architecture & Ports** | Antigravity runs as an Electron app spawning `language_server.exe` with dynamic DevTools port (`VERIFIED_RUNTIME`). | `tests/fixtures/t01/sample_process_tree.json`<br>`tests/fixtures/t01/sample_host_bridge_log.txt` | `Get-CimInstance Win32_Process \| Where-Object { $_.Name -match 'Antigravity\|language_server' } \| Select-Object ProcessId, ParentProcessId, Name, CommandLine`<br>`Get-Content "$env:APPDATA\Antigravity\DevToolsActivePort"` |
| **2. Incremental Quota Error Detection** | Quota detector accurately separates stale historical events from new events using byte-offset baselines and strictly enforces code 429 (`VERIFIED_RUNTIME`). | `tests/fixtures/t01/sample_quota_error_log.txt`<br>`tests/fixtures/t01/quota_positive.txt`<br>`tests/fixtures/t01/quota_negative_code_503.txt` | `python scripts/t01/test_quota_detector.py` |
| **3. Canonical Identity & 4-Way Local Correlation** | `cascade_id` conforms to UUID v4 RFC 4122 and correlates 9/9 across DB filename, SQLite `trajectory_meta`, `brain/` directory, and proto index (`VERIFIED_RUNTIME`). | `tests/fixtures/t01/sample_cross_correlation.json`<br>`tests/fixtures/t01/sample_trajectory_meta.json` | `python scripts/t01/correlate_cascade_id.py` |
| **4. Local Database Schema** | Each conversation is stored in a separate SQLite DB with `trajectory_meta`, `steps`, and `trajectory_metadata_blob` (`VERIFIED_RUNTIME`). | `tests/fixtures/t01/sample_trajectory_meta.json` | `python scripts/t01/inspect_conversation_db.py` |
| **5. CDP Endpoint & Active URL Mapping** | DevTools port allows CDP querying of active page target URL format `/c/<cascade_id>?section=<project_id>` (`VERIFIED_RUNTIME`). | `tests/fixtures/t01/sample_cdp_targets.json` | `python scripts/t01/inspect_cdp.py` |

---

## 9. Supervisor Detector Integration Contract (Task H)

```python
from dataclasses import dataclass
from typing import Optional, Dict, Any
from enum import Enum

class DetectorStatus(Enum):
    NO_NEW_EVENT = "NO_NEW_EVENT"
    NEW_CONFIRMED_QUOTA_EVENT = "NEW_CONFIRMED_QUOTA_EVENT"
    BASELINE_INVALID = "BASELINE_INVALID"
    LOG_UNAVAILABLE = "LOG_UNAVAILABLE"
    PARSE_ERROR = "PARSE_ERROR"

@dataclass
class LogBaseline:
    log_path: str
    byte_offset: int
    file_size: int
    file_mtime: float
    baseline_timestamp: str
    ls_pid: Optional[int]

@dataclass
class QuotaEventResult:
    status: DetectorStatus
    event_scope: str                     # "NEW_SINCE_BASELINE", "NONE"
    current_session_quota_state: str     # "CONFIRMED", "NORMAL", "UNKNOWN"
    signature_confidence: float          # 1.0 if signature matches, 0.0 otherwise
    resets_in: Optional[str]             # e.g. "3h24m48s"
    latest_event_timestamp: Optional[str]
    cursor: int                          # Updated byte offset for next poll
    rebaseline_required: bool = False
```
