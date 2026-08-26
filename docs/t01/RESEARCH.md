# Antigravity Desktop State and Conversation Forensics (T01 Amended Research Report)

## Executive Summary

This research establishes the exact runtime architecture, process tree, local filesystem storage schema, conversation identity system, and quota failure observability mechanisms for **Antigravity Desktop 2.10.0** on Windows. All findings have been updated with rigorous evidence classifications, tightened deterministic quota detection, multi-source conversation correlation, and downgraded claims where controlled multi-session experiments were not directly executed.

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
Antigravity.exe (Main Browser Process, PID: 14472)
  |-- Antigravity.exe (--type=gpu-process, PID: 6008)
  |-- Antigravity.exe (--type=utility --utility-sub-type=network.mojom.NetworkService, PID: 7380)
  |-- Antigravity.exe (--type=utility --utility-sub-type=audio.mojom.AudioService, PID: 4552)
  |-- Antigravity.exe (--type=utility --utility-sub-type=video_capture.mojom.VideoCaptureService, PID: 6852)
  |-- Antigravity.exe (--type=renderer, PID: 12992) [Electron Frontend UI]
  \-- language_server.exe (PID: 7520) [Agent Orchestrator & Backend RPC Service]
        |-- conhost.exe
        \-- pwsh.exe / cmd.exe [Tool execution workers]
```
*(Sanitized fixture: `tests/fixtures/t01/sample_process_tree.json`)*

### Localhost Services & Ports (`VERIFIED_RUNTIME`)
1. **Chrome DevTools Protocol (CDP)**:
   - Port dynamically assigned and recorded in `%APPDATA%\Antigravity\DevToolsActivePort` (e.g. `58859`).
   - Endpoint: `ws://127.0.0.1:<port>/devtools/browser/<uuid>`
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

### Idle vs. Active Generation States (`OBSERVED`)
- **Active Generation**:
  - `language_server.exe` establishes outbound HTTPS/2 SSE connections to `daily-cloudcode-pa.googleapis.com` calling `:streamGenerateContent?alt=sse` *(Sanitized fixture: `tests/fixtures/t01/sample_sse_stream_log.txt`)*.
  - Active write locks and writes on `%USERPROFILE%\.gemini\antigravity\conversations\<cascade_id>.db-wal` and `transcript.jsonl`.
- **Idle State**:
  - Outbound SSE streams closed.
  - Child processes terminate.
  - SQLite WAL files checkpointed.

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
| `%USERPROFILE%\.gemini\antigravity\brain\<cascade_id>\` | Directory Tree | Conversation transcripts (`transcript.jsonl`), subagent logs, task outputs, and artifacts (`implementation_plan.md`, `walkthrough.md`). |

### SQLite Conversation Schema (`<cascade_id>.db`)
Each conversation is backed by a standalone SQLite database containing:
- `trajectory_meta`:
  - `trajectory_id` (TEXT, UUID) - Primary Key
  - `cascade_id` (TEXT, UUID) - Conversation ID matching filename
  - `trajectory_type` (INTEGER)
  - `source` (INTEGER)
- `steps`:
  - `idx` (INTEGER) - Primary Key step counter
  - `step_type` (INTEGER), `status` (INTEGER), `has_subtrajectory` (NUMERIC), `metadata` (BLOB), `error_details` (BLOB), `permissions` (BLOB), `task_details` (BLOB), `render_info` (BLOB), `step_payload` (BLOB), `step_format` (INTEGER).
- `trajectory_metadata_blob`:
  - `id` (TEXT DEFAULT 'main'), `data` (BLOB - Serialized protobuf containing workspace URI, git branch name, and project UUID).
*(Sanitized fixture: `tests/fixtures/t01/sample_trajectory_meta.json`)*

---

## 3. Conversation Identity Analysis & 5-Source Correlation (Task C)

### Candidate Comparison Matrix

| Candidate Identifier | Source / Location | Stability Across Restart | Stability Across Close/Open | Collision Risk | Confidence | Recommendation |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`cascade_id` (Conversation UUID)** | `<id>.db`, `brain/<id>`, proto index, URL `/c/<id>` | **Durable (Disk)** | **Durable (Disk)** | **Negligible / Very Low** (RFC 4122 v4 UUID format) | `VERIFIED_RUNTIME` | **PRIMARY CANONICAL ID** |
| **`trajectory_id`** | `trajectory_meta` DB table, proto field 4 | **Durable (Disk)** | **Durable (Disk)** | Negligible / Very Low (UUID) | `VERIFIED_RUNTIME` | Secondary / Verification |
| **`project_id` / Section UUID** | `agyhub_summaries_proto.pb`, `app_storage.json` | Durable (Disk) | Durable (Disk) | Low | `VERIFIED_RUNTIME` | Scope Container |
| **Conversation Title** | Proto field 1, DOM `<title>` | Durable (Disk) | Durable (Disk) | **HIGH (Mutable/Duplicate)** | `VERIFIED_RUNTIME` | Non-Unique Display Only |
| **Workspace URI / Git Branch** | `trajectory_metadata_blob`, proto field 17 | Durable (Disk) | Durable (Disk) | Medium (multiple convos per repo) | `VERIFIED_RUNTIME` | Context Filter |
| **CDP Target ID** | `/json/list` `id` field | Ephemeral (Session) | Ephemeral (Session) | High across restarts | `VERIFIED_RUNTIME` | Session-only handle |

### 5-Way Cross-Source Correlation Matrix (`VERIFIED_RUNTIME`)
Validated across all 9 local conversation databases using `scripts/t01/correlate_cascade_id.py`:
1. **DB Filename**: `%USERPROFILE%\.gemini\antigravity\conversations\<cascade_id>.db`
2. **SQLite Table**: `SELECT cascade_id FROM trajectory_meta` matches DB filename exactly (9/9 matches).
3. **Brain Path**: `%USERPROFILE%\.gemini\antigravity\brain\<cascade_id>\` directory exists (9/9 matches).
4. **Protobuf Index**: Field 17 Subfield 6 of `%USERPROFILE%\.gemini\antigravity\agyhub_summaries_proto.pb` matches `cascade_id` (9/9 matches).
5. **Active CDP Target URL**: Active Electron renderer page URL path is `https://127.0.0.1:<port>/c/<cascade_id>?section=<project_id>`.
*(Sanitized fixture: `tests/fixtures/t01/sample_cross_correlation.json`)*

---

## 4. Restart & Persistence Dynamics (Task D)

### Classification: `OBSERVED` / `INFERENCE`
- **Observed Historical Persistence**:
  - Inspected historical database timestamps across restarts logged in `main.log` (08:21 AM, 16:03 PM, 16:27 PM).
  - SQLite databases created earlier in the day (e.g. `ea7f4ce1...db` from 08:21 AM and `44027a60...db` from 16:04 PM) remained intact and resumed active modifications during subsequent sessions.
  - `agyhub_summaries_proto.pb` preserved the list of conversation summaries across app restarts.
- **Limitation Note**: Controlled multi-cycle forced termination and restart experiments were NOT performed during this turn to preserve active subagent execution. Direct runtime verification of controlled restart is deferred to future integration testing.

---

## 5. Account-Switch Survival (Task E)

### Classification:
- `LOCAL_DATA_PRESENT_IN_CURRENT_PROFILE`: **`VERIFIED_RUNTIME`** (All 9 `.db` databases and `brain/` folders exist locally in user-level directory `%USERPROFILE%\.gemini\antigravity\`).
- `PERSISTS_ACROSS_ACCOUNT_SWITCH`: **`UNKNOWN`** (Inferred to persist, but live credential swap was not executed).
- `UI_CAN_OPEN_CONVERSATION`: **`UNKNOWN`** (Pending T02 live account rotation test).

---

## 6. Quota Failure Observability & Deterministic Detector (Task F & G)

### Exact Quota Exhaustion Signature (`VERIFIED_RUNTIME`)
Captured directly from `%APPDATA%\Antigravity\logs\language_server.log`:
```text
ERROR: logging before google.Init: E0826 17:40:12.155991  181166 errorreport.go:223] agent executor error: calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 3h24m48s.
```

### Deterministic Quota Detector (`scripts/t01/quota_detector.py`)
- **Matcher**: Strict regex matching `RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in (?P<resets_in>[^.)]+)`
- **Exit Codes**:
  - `0`: Confirmed Individual Quota Exhaustion.
  - `1`: No quota error detected (clean / normal log).
  - `2`: Error opening or reading target log file.
- **Machine-Readable JSON Output**:
  ```json
  {
    "quota_exhausted": true,
    "confidence": 1.0,
    "error_code": 429,
    "error_type": "RESOURCE_EXHAUSTED_INDIVIDUAL_QUOTA",
    "error_message": "Individual quota reached. Please upgrade your subscription to increase your limits.",
    "resets_in": "3h24m48s",
    "latest_timestamp": "0826 17:42:18.690066",
    "total_matches": 20
  }
  ```
- **Test Suite (`scripts/t01/test_quota_detector.py`)**:
  - `PASS`: Test 1 - Positive Quota Fixture (`tests/fixtures/t01/quota_positive.txt`)
  - `PASS`: Test 2 - Negative Generic RESOURCE_EXHAUSTED (`tests/fixtures/t01/quota_negative_generic_resource_exhausted.txt`)
  - `PASS`: Test 3 - Negative Other 429 (`tests/fixtures/t01/quota_negative_429_other.txt`)
  - `PASS`: Test 4 - Negative Normal Log (`tests/fixtures/t01/quota_negative_normal_log.txt`)
  - `PASS`: Test 5 - Live `language_server.log` (20 historical events detected)

### False Positive Disambiguation Matrix
| Signal | Source | Strength | False Positive Risk | Disambiguation Rule |
| :--- | :--- | :--- | :--- | :--- |
| `RESOURCE_EXHAUSTED (code 429): Individual quota reached...` | `language_server.log` | **STRONG** | Zero observed | Match full specific individual quota signature |
| UI Quota Card / Toast | Electron DOM / CDP | **STRONG** | Low | Check DOM for quota message |
| Generic `RESOURCE_EXHAUSTED` (Backend error) | `language_server.log` | Non-Quota | High | Excluded by regex (lacks "Individual quota reached") |
| HTTP 429 Rate Limit (Concurrency/RPM) | `language_server.log` | Non-Quota | High | Excluded by regex (lacks "Individual quota reached") |
| Inactivity / Idle Process | Process / Disk Activity | **WEAK** | Extreme | **NEVER trigger rotation on inactivity alone** |

---

## 7. Protobuf Wire Structure & Field Validation (Task B & C)

### Classification: `OBSERVED` / `INFERENCE`
In the absence of an official `.proto` schema file, field semantics in `%USERPROFILE%\.gemini\antigravity\agyhub_summaries_proto.pb` were decoded via generic tag-length-value parsing (`scripts/t01/parse_summaries_full.py`) and validated by cross-referencing against independent sources:
- **Field 1**: Display Title (matches DOM `<title>` and UI project tree).
- **Field 2**: Message / Step count (matches row count in `steps` SQLite table).
- **Field 3 / 7 / 10**: Timestamps (Unix epoch seconds/nanos matching filesystem MTime).
- **Field 4**: `trajectory_id` (matches `trajectory_meta.trajectory_id`).
- **Field 17 Subfield 6**: `cascade_id` (matches SQLite filename and `trajectory_meta.cascade_id`).
- **Field 17 Subfield 7**: Workspace URI (matches project folder path).
- **Field 17 Subfield 18**: `project_id` (matches `new-convo-last-selected-project` in `app_storage.json`).

---

## 8. Five Most Important Claims: CLAIM -> EVIDENCE -> REPRO COMMAND

| Claim | Claim Details & Class | Sanitized Evidence File | Independent Reproduction Command |
| :--- | :--- | :--- | :--- |
| **Claim 1: Process Architecture & Ports** | Antigravity runs as an Electron app spawning `language_server.exe` with dynamic DevTools port (`VERIFIED_RUNTIME`). | `tests/fixtures/t01/sample_process_tree.json`<br>`tests/fixtures/t01/sample_host_bridge_log.txt` | `Get-CimInstance Win32_Process \| Where-Object { $_.Name -match 'Antigravity\|language_server' } \| Select-Object ProcessId, ParentProcessId, Name, CommandLine`<br>`Get-Content "$env:APPDATA\Antigravity\DevToolsActivePort"` |
| **Claim 2: Deterministic Quota Error Detection** | Runtime quota exhaustion emits a specific signature `RESOURCE_EXHAUSTED (code 429): Individual quota reached...` with `Resets in <duration>` (`VERIFIED_RUNTIME`). | `tests/fixtures/t01/sample_quota_error_log.txt`<br>`tests/fixtures/t01/quota_positive.txt` | `python scripts/t01/quota_detector.py`<br>`python scripts/t01/test_quota_detector.py` |
| **Claim 3: Canonical Identity & 5-Source Correlation** | `cascade_id` (UUID v4) correlates identically across DB filename, SQLite `trajectory_meta`, `brain/` directory, proto index, and CDP URL (`VERIFIED_RUNTIME`). | `tests/fixtures/t01/sample_cross_correlation.json`<br>`tests/fixtures/t01/sample_trajectory_meta.json` | `python scripts/t01/correlate_cascade_id.py` |
| **Claim 4: Local Database Schema** | Each conversation is stored in a separate SQLite DB with `trajectory_meta`, `steps`, and `trajectory_metadata_blob` (`VERIFIED_RUNTIME`). | `tests/fixtures/t01/sample_trajectory_meta.json` | `python scripts/t01/inspect_conversation_db.py` |
| **Claim 5: CDP Endpoint & Active URL Mapping** | DevTools port allows CDP querying of active page target URL format `/c/<cascade_id>?section=<project_id>` (`VERIFIED_RUNTIME`). | `tests/fixtures/t01/sample_cdp_targets.json` | `python scripts/t01/inspect_cdp.py` |

---

## 9. Proposed Future Supervisor Adapter Contract (Task H)

```python
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from enum import Enum

class QuotaState(Enum):
    NORMAL = "NORMAL"
    CONFIRMED = "CONFIRMED"

@dataclass
class ConversationRef:
    cascade_id: str                      # Canonical UUID (e.g. 4674ef3b-d559-4a90-87e2-c30b11f03250)
    trajectory_id: Optional[str]        # Trajectory UUID
    title: str                           # Display title
    project_id: Optional[str]           # Project container UUID
    workspace_uri: str                   # file:/// URI
    git_branch: Optional[str]           # Active branch
    db_path: str                         # Path to local SQLite .db file
    transcript_path: str                 # Path to transcript.jsonl

@dataclass
class QuotaEvidence:
    quota_exhausted: bool
    confidence: float                    # 1.0 if confirmed, 0.0 otherwise
    error_code: Optional[int]            # 429
    error_type: Optional[str]            # "RESOURCE_EXHAUSTED_INDIVIDUAL_QUOTA"
    error_message: Optional[str]
    resets_in: Optional[str]             # e.g. "3h24m48s"
    latest_timestamp: Optional[str]
    total_matches: int
    source_log: str
```
