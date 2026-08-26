# Antigravity Desktop State and Conversation Forensics (T01 Research Report)

## Executive Summary

This research establishes the runtime architecture, process tree, local storage schema, conversation identity system, and quota failure observability mechanisms for **Antigravity Desktop 2.10.0** on Windows. All findings have been updated for **Round 4 Zero-Trust Standards**, featuring:
- Strict mandatory baseline for polling (`poll_new_events()` directly rejects missing baseline with `BASELINE_REQUIRED`, exit code 5, before any filesystem access).
- Enforced session & PID binding (caller omission of bound `language_server_process_id` or `supervisor_session_id` strictly returns `BASELINE_INVALID`, exit code 2).
- Binary mode (`open(..., "rb")`) with newline-delimited record buffering (guaranteeing partial/split writes cannot lose events or corrupt byte offsets).
- Filesystem identity tracking on Windows (`dev`, `ino`, `ctime_ns`, `size_at_creation`) detecting file replacements of smaller, equal, and larger sizes (`BASELINE_INVALID`).
- Versioned baseline schema (`1.0.0`) with path, offset, and process/session validation.
- Terminology correction: log-prefix numeric field classified as `log_thread_id` / `OBSERVED_FORMAT` (never inferred as process PID).
- Strict state semantics: `NO_NEW_EVENT` returns `UNKNOWN_OR_UNCHANGED` (never reverts sticky quota state to `NORMAL`).
- Event ID and SHA-256 bound directly to actual complete record bytes (`evt_<ino>_<start>_<end>_<record_sha256[:16]>`).
- Supervisor cursor persistence contract with explicit crash replay and deduplication handling.
- Default output sanitization (raw log lines suppressed unless explicit diagnostic mode).
- Exact URL pathname route matching for CDP conversation page targets (`type == "page"`, `/c/<uuid>`).
- Reverse-engineered protobuf mapping classification (`REVERSE_ENGINEERED_RUNTIME_MAPPING`).

---

## 1. Process Forensics (Task A)

### Classification: `VERIFIED_LIVE_RUNTIME` (within single running session)
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

### Localhost Services & Ports (`VERIFIED_LIVE_RUNTIME`)
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

### Classification: `VERIFIED_LIVE_RUNTIME`

### Primary Storage Locations
| Path | File / Dir | Role |
| :--- | :--- | :--- |
| `%APPDATA%\Antigravity\app_storage.json` | JSON File | UI state, last selected project (`new-convo-last-selected-project`), pane tabs, preferences. |
| `%APPDATA%\Antigravity\DevToolsActivePort` | Text File | Remote debugging port and browser WS path. |
| `%APPDATA%\Antigravity\logs\main.log` | Log File | Electron lifecycle, updater, language_server spawn arguments and dynamic ports. |
| `%APPDATA%\Antigravity\logs\language_server.log` | Log File | Language server internal logs, RPC events, API requests/responses, and quota/auth errors. |
| `%USERPROFILE%\.gemini\antigravity\antigravity_state.pbtxt` | Text Proto | Installation UUID, onboarding flags, model preferences. |
| `%USERPROFILE%\.gemini\antigravity\agyhub_summaries_proto.pb` | Binary Proto | Global index of all conversations, titles, message counts, workspace roots, git branches, timestamps, and project associations (`REVERSE_ENGINEERED_RUNTIME_MAPPING`). |
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
| **`cascade_id` (Conversation UUID)** | `<id>.db`, `brain/<id>`, proto index, URL `/c/<id>` | **Durable (Disk)** | **Negligible / Very Low** (All 9 observed conform to UUID v4 RFC 4122) | `VERIFIED_LIVE_RUNTIME` | **PRIMARY CANONICAL ID** |
| **`trajectory_id`** | `trajectory_meta` DB table, proto field 4 | Durable (Disk) | Negligible / Very Low (UUID v4) | `VERIFIED_LIVE_RUNTIME` | Secondary / Verification |
| **`project_id` / Section UUID** | `agyhub_summaries_proto.pb`, `app_storage.json` | Durable (Disk) | Low | `VERIFIED_LIVE_RUNTIME` | Scope Container |
| **Conversation Title** | Proto field 1, DOM `<title>` | Durable (Disk) | **HIGH (Mutable/Duplicate)** | `VERIFIED_LIVE_RUNTIME` | Non-Unique Display Only |
| **Workspace URI / Git Branch** | `trajectory_metadata_blob`, proto field 17 | Durable (Disk) | Medium (multiple convos per repo) | `VERIFIED_LIVE_RUNTIME` | Context Filter |
| **CDP Target ID** | `/json/list` `id` field | Ephemeral (Session) | High across restarts | `VERIFIED_LIVE_RUNTIME` | Session-only handle |

### Correlation Evidence
1. **Local Four-Way Correlation (`VERIFIED_LIVE_RUNTIME`)**:
   - Script: `scripts/t01/correlate_cascade_id.py`
   - Verified across all 9 local conversation databases (9/9 matches):
     - DB Filename: `<cascade_id>.db`
     - SQLite Table: `SELECT cascade_id FROM trajectory_meta`
     - Brain Directory: `brain/<cascade_id>/`
     - Protobuf Index: `agyhub_summaries_proto.pb` (Field 17 Subfield 6, `REVERSE_ENGINEERED_RUNTIME_MAPPING`)
2. **Active Foreground CDP Target Correlation (`VERIFIED_LIVE_RUNTIME`)**:
   - Filtered strictly by `type == "page"` with exact URL pathname regex `^/c/([0-9a-fA-F-]{36})$`.
   - Result: Exactly 1 eligible conversation page target matched 1 cascade ID (1/1 eligible targets).
*(Sanitized fixture: `tests/fixtures/t01/sample_cross_correlation.json`)*

---

## 4. Restart & Persistence Dynamics (Task D)

### Classification: `OBSERVED` / `INFERENCE`
- Historical persistence confirmed across logged launches in `main.log` (08:21 AM, 16:03 PM, 16:27 PM).
- SQLite databases created earlier in the day remained intact and resumed active modifications.
- **Limitation (`NOT_LIVE_TESTED`)**: Controlled multi-cycle forced termination and restart experiments were not performed during this research lane to preserve active subagent communication.

---

## 5. Account-Switch Survival (Task E)

### Classification:
- `LOCAL_DATA_PRESENT_IN_CURRENT_PROFILE`: **`VERIFIED_LIVE_RUNTIME`** (All 9 `.db` databases and `brain/` folders exist locally in `%USERPROFILE%\.gemini\antigravity\`).
- `PERSISTS_ACROSS_ACCOUNT_SWITCH`: **`UNKNOWN`** (Inferred to persist, but live credential swap was not executed by T01).
- `UI_CAN_OPEN_CONVERSATION`: **`UNKNOWN`** (Pending T02 live account rotation test).

---

## 6. Incremental Quota Failure Detection (Task F & G)

### Exact Quota Exhaustion Signature (`VERIFIED_LIVE_RUNTIME`)
Strictly requires exact code 429 and individual quota message:
```text
ERROR: logging before google.Init: E0826 12:00:05.000000   99999 errorreport.go:223] agent executor error: calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 3h24m48s.
```

### Production Incremental Detector Contract (`scripts/t01/quota_detector.py`)
1. **Mandatory Baseline**: `poll_new_events()` directly rejects missing baseline with `BASELINE_REQUIRED` (exit code 5). Historical scan is strictly opt-in via `--historical`.
2. **Session Identity Binding**: Binds to `language_server_process_id` and `supervisor_session_id`. If bound in baseline, omission or mismatch by caller returns `BASELINE_INVALID` (exit code 2).
3. **Binary Mode & Partial Write Buffer**: Uses `open(..., "rb")`. Only advances committed byte offset through the last complete newline byte (`b"\n"`). Trailing incomplete lines remain uncommitted for the next poll cycle without data loss.
4. **File Replacement Detection**: Compares Windows NTFS file identity `(dev, ino, ctime_ns, size_at_creation)`. Replaced files (smaller, equal, or larger) immediately trigger `BASELINE_INVALID` (`rebaseline_required: True`, exit code 2).
5. **Deterministic Event Identification**: Each event produces a unique, replayable `event_id` bound to file inode, start/end byte offsets, and SHA-256 hash of complete record bytes (`evt_<ino>_<start>_<end>_<record_sha256[:16]>`).
6. **Sticky State Ownership**: `NO_NEW_EVENT` returns `current_session_quota_state: "UNKNOWN_OR_UNCHANGED"` and `quota_state_effect: "UNCHANGED"`. It NEVER claims `NORMAL`. State recovery is owned by the supervisor.
7. **Supervisor Cursor & Crash Replay Contract**: Poller returns cursor without mutating baseline. Caller atomically persists cursor only after deduplicating/processing events. If supervisor crashes before cursor commit, replaying the same byte range produces identical `event_id`, enabling supervisor-level deduplication.
8. **Default Output Sanitization**: Normalized JSON output omits raw log text by default (accessible only via `--include-raw-log`).

### Exit Code Specification
- `0`: `BASELINE_INITIALIZED` (on `--init-baseline`) or `NEW_CONFIRMED_QUOTA_EVENT` (on `--baseline`)
- `1`: `NO_NEW_EVENT` (or `NO_HISTORICAL_QUOTA_EVENT` on `--historical`)
- `2`: `BASELINE_INVALID` (schema error, replacement, truncation, PID change, session change)
- `3`: `LOG_UNAVAILABLE` (missing log file)
- `4`: `PARSE_ERROR` (binary read/IO failure)
- `5`: `BASELINE_REQUIRED` (polling attempted without baseline)

---

## 7. Baseline Schema Specification

```json
{
  "schema_version": "1.0.0",
  "canonical_log_path": "C:\\Users\\<USER>\\AppData\\Roaming\\Antigravity\\logs\\language_server.log",
  "committed_byte_offset": 1048576,
  "file_size": 1048576,
  "file_identity": {
    "dev": 3221225472,
    "ino": 14073748835632618,
    "ctime_ns": 1724660853565000000,
    "size_at_creation": 1048576
  },
  "created_at": "2026-08-26T12:00:00Z",
  "language_server_process_id": 10004,
  "supervisor_session_id": "session_alpha_001",
  "status": "BASELINE_INITIALIZED"
}
```

---

## 8. Supervisor Detector Integration Contract (Task H)

```python
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
from enum import Enum

class DetectorStatus(Enum):
    BASELINE_INITIALIZED = "BASELINE_INITIALIZED"
    NO_NEW_EVENT = "NO_NEW_EVENT"
    NEW_CONFIRMED_QUOTA_EVENT = "NEW_CONFIRMED_QUOTA_EVENT"
    BASELINE_INVALID = "BASELINE_INVALID"
    LOG_UNAVAILABLE = "LOG_UNAVAILABLE"
    PARSE_ERROR = "PARSE_ERROR"
    BASELINE_REQUIRED = "BASELINE_REQUIRED"

@dataclass
class FileIdentity:
    dev: int
    ino: int
    ctime_ns: int
    size_at_creation: int

@dataclass
class LogBaseline:
    schema_version: str
    canonical_log_path: str
    committed_byte_offset: int
    file_size: int
    file_identity: FileIdentity
    created_at: str
    language_server_process_id: Optional[int] = None
    supervisor_session_id: Optional[str] = None

@dataclass
class QuotaEvent:
    event_id: str                          # e.g. "evt_14073748835632618_1024_1280_51f860466b6312ff"
    event_sha256: str                      # Hash of complete record bytes
    event_record_sha256: str               # Explicit record bytes hash
    code: int                              # 429
    resets_in: Optional[str]               # e.g. "3h24m48s"
    log_timestamp: Optional[str]
    log_thread_id: Optional[str]           # OBSERVED_FORMAT (not process PID)
    source_location: Optional[str]
    event_start_offset: int
    event_end_offset: int
    account_attribution: str = "UNKNOWN_AT_T01_LAYER"
    evidence_class: str = "OBSERVED_FORMAT"

@dataclass
class QuotaPollResult:
    status: DetectorStatus
    event_poll_status: str
    quota_state_effect: str                # "EXHAUSTED", "UNCHANGED"
    current_session_quota_state: str       # "CONFIRMED", "UNKNOWN_OR_UNCHANGED"
    signature_confidence: float            # 1.0 or 0.0
    account_attribution: str               # "UNKNOWN_AT_T01_LAYER"
    new_events_count: int
    events: List[QuotaEvent]
    cursor: int
    rebaseline_required: bool = False
    error: Optional[str] = None
```
