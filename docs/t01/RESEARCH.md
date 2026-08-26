# Antigravity Desktop State and Conversation Forensics (T01 Research Report)

## Executive Summary

This research establishes the exact runtime architecture, process tree, local filesystem storage schema, conversation identity system, and quota failure observability mechanisms for **Antigravity Desktop 2.10.0** on Windows 10/11.

---

## 1. Process Forensics (Task A)

### Classification: `VERIFIED_RUNTIME`
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

### Localhost Services & Ports
1. **Chrome DevTools Protocol (CDP)**:
   - Port dynamically assigned and recorded in `%APPDATA%\Antigravity\DevToolsActivePort` (e.g. `58859`).
   - Endpoint: `ws://127.0.0.1:<port>/devtools/browser/<uuid>`
   - HTTP Target Discovery: `http://127.0.0.1:<port>/json/list` and `/json/version`
   - Active Target URL format: `https://127.0.0.1:<ls_port>/c/<cascade_id>?section=<project_id>`
2. **Host Bridge Server**:
   - Hosted by Electron main process on `http://127.0.0.1:58860` (controlled via `--host_bridge_token`).
3. **Language Server gRPC / HTTPS Service**:
   - Hosted by `language_server.exe` on dynamic port (e.g. `https://127.0.0.1:58861/`).
4. **Language Server HTTP Service**:
   - Hosted on secondary dynamic port (e.g. `http://127.0.0.1:58862`).

### Idle vs. Active Generation States
- **Active Generation**:
  - `language_server.exe` establishes outbound HTTPS/2 SSE connections to `daily-cloudcode-pa.googleapis.com` or `generativelanguage.googleapis.com` calling `:streamGenerateContent?alt=sse`.
  - Child processes (`pwsh.exe`, `conhost.exe`) spawned under `language_server.exe` for tool calls.
  - Active write locks and writes on `%USERPROFILE%\.gemini\antigravity\conversations\<cascade_id>.db-wal` and `%USERPROFILE%\.gemini\antigravity\brain\<cascade_id>\.system_generated\logs\transcript.jsonl`.
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
  - `step_type` (INTEGER)
  - `status` (INTEGER)
  - `has_subtrajectory` (NUMERIC)
  - `metadata` (BLOB)
  - `error_details` (BLOB)
  - `permissions` (BLOB)
  - `task_details` (BLOB)
  - `render_info` (BLOB)
  - `step_payload` (BLOB)
  - `step_format` (INTEGER)
- `trajectory_metadata_blob`:
  - `id` (TEXT DEFAULT 'main')
  - `data` (BLOB) - Serialized protobuf containing workspace URI, git branch name, and project UUID.
- `gen_metadata`, `executor_metadata`, `parent_references`, `battle_mode_infos`.

---

## 3. Conversation Identity Analysis (Task C)

### Candidate Comparison Matrix

| Candidate Identifier | Source / Location | Stability Across Restart | Stability Across Close/Open | Collision Risk | Confidence | Recommendation |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`cascade_id` (Conversation UUID)** | `<id>.db`, `brain/<id>`, proto index, URL `/c/<id>` | **YES (Durable)** | **YES (Durable)** | None (UUID v4) | `VERIFIED_RUNTIME` | **PRIMARY CANONICAL ID** |
| **`trajectory_id`** | `trajectory_meta` DB table, proto field 4 | **YES (Durable)** | **YES (Durable)** | None (UUID v4) | `VERIFIED_RUNTIME` | Secondary / Verification |
| **`project_id` / Section UUID** | `agyhub_summaries_proto.pb`, `app_storage.json` | **YES (Durable)** | **YES (Durable)** | Low | `VERIFIED_RUNTIME` | Scope Container |
| **Conversation Title** | Proto field 1, DOM `<title>` | YES (Durable) | YES (Durable) | **HIGH (Mutable)** | `VERIFIED_RUNTIME` | Non-Unique Display Only |
| **Workspace URI / Git Branch** | `trajectory_metadata_blob`, proto field 17 | YES (Durable) | YES (Durable) | Medium (multiple convos per repo) | `VERIFIED_RUNTIME` | Context Filter |
| **CDP Target ID** | `/json/list` `id` field | NO (Ephemeral) | NO (Ephemeral) | High across restarts | `VERIFIED_RUNTIME` | Session-only handle |

**Decision**: The canonical conversation identifier is `cascade_id` (UUID format `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`).

---

## 4. Restart & Persistence Dynamics (Task D)

### Classification: `VERIFIED_RUNTIME`
- Antigravity Desktop persists conversation state continuously into two locations:
  1. `<cascade_id>.db` + WAL in `%USERPROFILE%\.gemini\antigravity\conversations/`
  2. `transcript.jsonl` in `%USERPROFILE%\.gemini\antigravity\brain/<cascade_id>/.system_generated/logs/`
- Across app restarts:
  - The SQLite databases remain on disk and are not purged.
  - `agyhub_summaries_proto.pb` persists the list and ordering of conversations.
  - The UI restores the last active conversation based on `app_storage.json` (`new-convo-last-selected-project` and active window route).
  - DevTools port is regenerated on each launch and written to `DevToolsActivePort`.

---

## 5. Account-Switch Survival (Task E)

### Classification: `VERIFIED_RUNTIME` / `INFERENCE`
- **Distinction**:
  - `LOCAL_DATA_EXISTS`: All `<cascade_id>.db` files and `brain/<cascade_id>` folders exist independently on the local Windows disk and remain physically intact across account operations.
  - `UI_CAN_OPEN_CONVERSATION`: The Electron UI lists conversations mapped in `agyhub_summaries_proto.pb`. When an account is switched, the UI may refresh project sessions, but because conversation SQLite databases are stored in local `%USERPROFILE%\.gemini\antigravity\`, local data is preserved.
  - Note: T02 owns the live account rotation mechanics and authentication token replacement.

---

## 6. Quota Failure Observability (Task F & G)

### Concrete Runtime Quota Failure Log Evidence
Directly captured from `%APPDATA%\Antigravity\logs\language_server.log`:
```text
Line 4134: ERROR: logging before google.Init: I0826 17:40:05.441110  181166 run.go:367] Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 3h24m54s.), retrying in 1s
Line 4135: ERROR: logging before google.Init: I0826 17:40:08.537815  181166 run.go:367] Run: attempt 2 failed (RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 3h24m51s.), retrying in 1.85516771s
Line 4137: ERROR: logging before google.Init: E0826 17:40:12.155991  181166 errorreport.go:223] agent executor error: calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 3h24m48s.
Line 4138: ERROR: logging before google.Init: E0826 17:40:12.178117  181166 errorreport.go:223] calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 3h24m48s.
```

### Signal Classification & Observability Matrix
| Signal | Source | Strength | False Positive Risk | Disambiguation Rule |
| :--- | :--- | :--- | :--- | :--- |
| **`RESOURCE_EXHAUSTED (code 429)` log event** | `%APPDATA%\Antigravity\logs\language_server.log` | **STRONG** | Low | Tail log for `RESOURCE_EXHAUSTED` and `Individual quota reached`. |
| **UI Quota Toast / Error Card** | Electron DOM / CDP evaluation | **STRONG** | Low | Check for text "Individual quota reached" or "quota" in DOM. |
| **Active Turn Termination without Complete Status** | SQLite `steps` table status / transcript | **MEDIUM** | Medium | Can occur on crash or user cancel. Verify log to confirm quota. |
| **Absence of Child Process Activity** | Win32 Process Tree | **WEAK** | High | Occurs during normal thinking, user pause, idle. Never use alone. |
| **Inactivity / Unchanged Workspace** | Filesystem MTime | **WEAK** | Extreme | Normal behavior between prompts. Must NEVER trigger rotation. |

### False Positive Disambiguation
- **Normal Idle**: Process alive, no log error, step status is clean `DONE`.
- **User Pause / Waiting for Input**: UI waiting for user feedback; no `RESOURCE_EXHAUSTED` log; step status indicates waiting.
- **Model Thinking**: Active outbound network connections, no error in log.
- **Network Loss**: Error logs report connection reset or DNS failure, NOT `RESOURCE_EXHAUSTED (code 429)`.
- **App Crash**: Process dies, DevTools port becomes unreachable.

---

## 7. Proposed Watchdog Supervisor Adapter Contract (Task H)

```python
# Proposed Read-Only Adapter Contract for Desktop Forensics & Quota Detection

from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from enum import Enum

class QuotaState(Enum):
    NORMAL = "NORMAL"
    SUSPECTED = "SUSPECTED"
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
    last_modified_epoch: float

@dataclass
class DesktopSnapshot:
    is_running: bool
    main_pid: Optional[int]
    language_server_pid: Optional[int]
    devtools_port: Optional[int]
    cdp_ws_url: Optional[str]
    active_conversation_id: Optional[str]
    active_page_title: Optional[str]
    conversations: List[ConversationRef]

@dataclass
class QuotaEvidence:
    state: QuotaState
    confidence: float                    # 0.0 to 1.0 (1.0 = confirmed log/DOM error)
    error_message: Optional[str]         # Raw error text (e.g. "RESOURCE_EXHAUSTED (code 429)...")
    resets_in_str: Optional[str]         # e.g. "3h24m48s"
    log_timestamp: Optional[str]
    source: str                          # "language_server.log", "cdp_dom", etc.
    retryable: bool

class AntigravityDesktopAdapter:
    def inspect_desktop(self) -> DesktopSnapshot:
        """Collects process tree, listening ports, CDP status, and active conversation."""
        ...

    def locate_conversation(self, cascade_id: str) -> Optional[ConversationRef]:
        """Locates conversation metadata from SQLite DB and summaries protobuf."""
        ...

    def find_conversations_by_workspace(self, workspace_path: str) -> List[ConversationRef]:
        """Finds all conversation IDs matching a repository/workspace root."""
        ...

    def detect_quota_failure(self, timeout_ms: int = 1000) -> QuotaEvidence:
        """Inspects language_server.log and CDP DOM for strong quota exhaustion signals."""
        ...
```

---

## 8. Summary of Findings by Verification Class

- **`VERIFIED_RUNTIME`**:
  - Electron main + language_server.exe architecture and dynamic port bindings.
  - `DevToolsActivePort` remote debugging port and `/json/list` target extraction.
  - SQLite database schema (`trajectory_meta`, `steps`, `trajectory_metadata_blob`).
  - `agyhub_summaries_proto.pb` binary structure and field mappings.
  - Exact `RESOURCE_EXHAUSTED (code 429)` error log format and retry pattern in `language_server.log`.
  - Canonical identity `cascade_id` in URL `/c/<id>`, filename `<id>.db`, and proto summaries.
- **`VERIFIED_SOURCE` / `VERIFIED_DOC`**:
  - Built-in guide `antigravity-guide` surface and CLI/Desktop specifications.
- **`OBSERVED`**:
  - Automatic reopening behavior of last active project upon launch.
- **`INFERENCE`**:
  - Account switching by T02 does not delete local SQLite database files on disk because they reside in user-scoped `%USERPROFILE%\.gemini\antigravity\conversations\`.
- **`UNKNOWN`**:
  - Live UI behavior when an account is switched while a conversation is mid-turn with pending uncommitted step (owned by T02/T03 integration).
