# Research Package Report: Desktop Conversation Restore & Automatic Resume Submission

**Agent ID:** T03  
**Assigned Issue:** #3 — Desktop conversation restore and automatic resume submission  
**Assigned Branch:** `research/T03-conversation-resume`  
**Base SHA:** `3377bf7a0523795a678ac5da951371e3f5ee94c7`

---

## 1. Executive Summary

This investigation establishes the definitive contracts and mechanisms for an external Windows watchdog to locate the intended Antigravity Desktop coding conversation, preserve its continuity, submit a single resume instruction, and verify that a new agent turn has actively started without generating duplicate submissions.

### Key Verified Discoveries
1. **CDP Port Auto-Discovery (`VERIFIED_RUNTIME`)**: Antigravity Desktop 2.10.0 launches with dynamic debugging port (`--remote-debugging-port=0`) and writes its active port and browser websocket target directly to `%APPDATA%\Antigravity\DevToolsActivePort`. An external watchdog can deterministically attach via WebSocket without hardcoded ports or process restarts.
2. **Stable Conversation UUID Mapping (`VERIFIED_RUNTIME`)**: Every conversation in the Antigravity Desktop sidebar renders with `a[href="/c/<uuid>"]` and persists in `~/.gemini/antigravity/conversations/<uuid>.db`. The UI UUID matches the local SQLite database and transcript directory 1:1.
3. **Lexical Composer & Native Event Dispatch (`VERIFIED_RUNTIME`)**: The composer is a React-managed Lexical editor (`div[role="combobox"][data-lexical-editor="true"][aria-label="Message input"]`). Direct DOM value assignment fails to update React state. Using CDP `Input.insertText` and native `Input.dispatchKeyEvent` successfully drives Lexical, enabling `button[data-testid="send-button"]` (`disabled: false`) and submitting cleanly.
4. **Active Turn Confirmation Signals (`VERIFIED_RUNTIME`)**: Submission is confirmed by discrete signals: composer clearing, user message node mounting in `div[role="article"]`, and the appearance of the `Stop Task` / `Stop execution` button.
5. **Session Isolation & UIA Limitations (`VERIFIED_RUNTIME`)**: Windows UI Automation (UIA) fails in non-interactive background/service sessions (Session 0) where `EnumWindows` and root desktop enumeration return 0 visible windows. CDP over localhost TCP operates reliably across all session boundaries and minimized windows.

---

## 2. Evidence Classes & Methodology

All findings in this report are categorized strictly under the required evidence standards:
- **`VERIFIED_RUNTIME`**: Direct empirical reproduction on the live Windows runtime.
- **`VERIFIED_SOURCE`**: Verified directly from authoritative source code (OpenCLI v1.8.7, Antigravity bundle).
- **`VERIFIED_DOC`**: Confirmed from official documentation.
- **`OBSERVED`**: Observed in single live runs, requires multi-run validation.
- **`INFERENCE`**: Deductions based on observed patterns.
- **`UNKNOWN`**: Unverified behaviors or architectural boundaries requiring further testing.

---

## 3. Detailed Findings by Task

### Task A — Automation Approach Order
We evaluated the recovery mechanisms in the required priority order:
1. **CDP / DevTools Protocol (`VERIFIED_RUNTIME`)**: **TIER 1 (RECOMMENDED PRIMARY)**. Fully exposed via `DevToolsActivePort`, supports direct DOM inspection, React/Lexical event dispatch, WebSocket event streaming, and operates when minimized.
2. **Official CLI Resume Bridge (`VERIFIED_RUNTIME` / `VERIFIED_DOC`)**: **TIER 2 (FALLBACK)**. Standalone `agy` CLI is not present in default desktop installations; backend language server (`language_server.exe`) exposes gRPC/LSP but lacks standalone CLI conversational resume commands.
3. **Windows UI Automation (`VERIFIED_RUNTIME`)**: **TIER 3 (RESTRICTED FALLBACK)**. Requires interactive desktop session (`WinSta0`), fragile accessibility mappings for Lexical DOM nodes, completely unavailable in non-interactive / service contexts.
4. **Fixed Coordinates Mouse/Keyboard (`VERIFIED_RUNTIME`)**: **REJECTED**. Brittle to screen scaling, window position changes, and multi-monitor setups.

---

### Task B — OpenCLI Source Review
- **Source Inspected (`VERIFIED_SOURCE`)**: `@jackwener/opencli` version `1.8.7` (Tarball: `https://registry.npmjs.org/@jackwener/opencli/-/opencli-1.8.7.tgz`).
- **Relevant Files**:
  - `clis/antigravity/send.js`
  - `clis/antigravity/status.js`
  - `clis/antigravity/read.js`
  - `clis/antigravity/_actions.js`
  - `clis/antigravity/storage.js`
  - `dist/src/electron-apps.js`
  - `dist/src/browser/cdp.js`
- **Critical Findings & Failure Modes (`VERIFIED_SOURCE`)**:
  1. *Hardcoded macOS paths*: `storage.js` uses `Library/Application Support/Antigravity` and `/usr/bin/sqlite3`, failing completely on Windows.
  2. *Hardcoded Port Assumption*: Assumes static port `9234` (`electron-apps.js`), failing against Antigravity's dynamic port allocation.
  3. *Outdated Selectors*: Looks for `antigravity.agentSidePanelInputBox` and `convo-pill-<uuid>`, which do not exist in Antigravity 2.10.0 Desktop.
  4. *False Success Reporting*: `send.js` executes `page.pressKey('Enter')` and immediately returns `{ Status: 'Sent successfully' }` without verifying whether text cleared, a message node appeared, or an assistant turn started.
  5. *Zero Idempotency / Duplicate Protection*: `send.js` lacks duplicate checks or active turn detection.

---

### Task C — CDP Runtime Test
- **Startup & Discovery (`VERIFIED_RUNTIME`)**:
  - Endpoint discovery: Read line 1 of `%APPDATA%\Antigravity\DevToolsActivePort`.
  - Live Endpoint: `http://127.0.0.1:58859`
  - Browser: `Chrome/146.0.7680.72` (Electron 41.0.2)
  - Target URL format: `https://127.0.0.1:58861/c/<conversation-uuid>?section=<section-uuid>`
  - DOM Accessibility: Full DOM tree, React fiber nodes, Lexical editor, and sidebar elements are inspectable and modifiable via `Runtime.evaluate` and `Input.*` CDP domains.

---

### Task D — Official Antigravity CLI Verification
- **CLI Presence (`VERIFIED_RUNTIME`)**: Standalone `agy` executable is not installed in standard PATH.
- **Backend Server (`VERIFIED_RUNTIME`)**: Antigravity includes `language_server.exe` under `resources\bin\language_server.exe`, which supports gRPC, LSP (`-enable_lsp`), and an internal `agentapi` verb (`agentapi.bat`), but does not provide an external thread resume command for the desktop UI.
- **Verdict**: Desktop continuity must be managed through the Desktop application layer (CDP).

---

### Task E — Windows UI Automation (UIA)
- **Session 0 / Background Execution Failure (`VERIFIED_RUNTIME`)**: In background or non-interactive subshells, `uiautomation.GetRootControl().GetChildren()` and `win32gui.EnumWindows` return 0 visible windows.
- **Control Classification (`VERIFIED_RUNTIME`)**: Antigravity renders inside a Chromium `Chrome_WidgetWin_1` container. Lexical composer is rendered as an internal Chromium contenteditable, not a native Win32 `EditControl`.
- **Verdict**: UIA is inadequate as a primary automation vector.

---

### Task F — Conversation Identity Ranking Strategy

| Strategy | Rank | Uniqueness | Persistence | Restart Stability | Ambiguity Risk | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Stable Conversation UUID** (`/c/<uuid>`) | **1** | **Global (UUIDv4)** | Durable (`.db` + UI) | 100% Stable | None (Unique) | **PRIMARY STANDARD** |
| **Workspace / Repository Path Mapping** | **2** | Per-Workspace | Durable | High | Low | **SECONDARY FILTER** |
| **Exact Conversation Title** | **3** | Non-unique | Ephemeral | Medium | High (Collision) | Refuse if ambiguous |
| **Current Selected Conversation** | **4** | Single | Ephemeral | None (resets) | Medium | Dry-run only |
| **First Visible Conversation** | **5** | Arbitrary | None | None | Fatal | **FORBIDDEN IN PROD** |

---

### Task G — Resume Prompt Submission Pipeline

We implemented and verified the 6-phase submission pipeline in `scripts/t03/send_resume.py`:

```text
[1. COMPOSER_LOCATED] -> [2. TEXT_INSERTED] -> [3. TEXT_VERIFIED] -> [4. SEND_TRIGGERED] -> [5. USER_MSG_APPEARED] -> [6. TURN_STARTED]
```

#### Phase Breakdown & Verification (`VERIFIED_RUNTIME`):
1. **`COMPOSER_LOCATED`**: Verified `div[role="combobox"][data-lexical-editor="true"]`.
2. **`TEXT_INSERTED`**: CDP `Input.insertText` injects resume prompt.
3. **`TEXT_VERIFIED`**: Verified composer `innerText === prompt` and `button[data-testid="send-button"].disabled === false`.
4. **`SEND_TRIGGERED`**: Triggered via `send-button.click()` or CDP `Enter` key event.
5. **`USER_MSG_APPEARED`**: Composer clears (`innerText === ''`) and new `div[role="article"]` mounts.
6. **`TURN_STARTED`**: Assistant active execution signal detected (`Stop Task` button mounts).

---

### Task H — Turn Start Confirmation Signals

| Signal | Strength | Observation Mechanism | False Positive Risk |
| :--- | :--- | :--- | :--- |
| **Stop Task Button Present** (`button[aria-label*="Stop"]`) | **STRONG** | Live DOM query | Very Low |
| **Assistant Article Node Mounting** (`div[role="article"]`) | **STRONG** | DOM node count delta | Low |
| **Composer Text Cleared + Send Disabled** | **MEDIUM** | Lexical text inspection | Medium (could fail before LLM call) |
| **WebSocket Progress/Chunk Events** | **STRONG** | CDP Network/Console listener | Very Low |
| **Composer Focus Lost** | **WEAK** | Focus state | High |

---

### Task I & J — Exactly-Once / Effectively-Once Duplicate Prevention

#### Recovery Protocol State Machine:
- `NOT_SENT`: Initial state; resume prompt has not been submitted.
- `SUBMISSION_ATTEMPTED`: Keystrokes/click sent; awaiting DOM response.
- `MESSAGE_OBSERVED`: User message visible in history.
- `TURN_STARTED`: Confirmed turn execution in progress.
- `TURN_ACTIVE`: Conversation is currently busy.
- `FAILED`: Definitive failure; supervisor escalation required.

#### Crash Window Analysis:
- **Window A (Crash before send)**: State is `NOT_SENT`. Watchdog restarts and performs normal send.
- **Window B (Crash immediately after send)**: State is `SUBMISSION_ATTEMPTED`. On restart, watchdog queries recent conversation articles. If a matching resume message is already present, watchdog transitions directly to `MESSAGE_OBSERVED` / `TURN_STARTED` without resending.
- **Window C (Crash after user message appears)**: Message observed in DOM. Watchdog waits for turn start; resend is blocked.
- **Window D (Crash during assistant generation)**: `Stop Task` button is active. Watchdog marks `TURN_ACTIVE` and aborts duplicate submission.

**Verdict (`VERIFIED_RUNTIME`)**: Categorized as **`EFFECTIVELY_ONCE`** via deterministic state inspection.

---

### Task K — Missing Conversation Resolution States

```text
Target Request
      |
      +---> UUID matches sidebar row / URL ---------> CONVERSATION_FOUND
      |
      +---> Multiple matches for title/project -----> CONVERSATION_AMBIGUOUS (Fail-Safe STOP)
      |
      +---> UUID not in sidebar / URL redirects ----> CONVERSATION_NOT_FOUND
      |
      +---> Account switch hides conversation -------> CONVERSATION_INACCESSIBLE (Trigger Rehydration)
```

---

### Task L — Recommended Fallback Order

```text
1. CDP Connection via DevToolsActivePort
   |-- Success: Restore UUID -> Submit Resume -> Verify Turn
   +-- Failure / Port Unavailable
         |
         v
2. Context Rehydration via Repository State (Git HEAD, diff, local task state)
   |-- Launch New Session / Workspace Task
   +-- Failure
         |
         v
3. BLOCKED (Escalate to Human Operator)
```

---

### Task M — Failure Injection Matrix

| Failure Mode | Classification | Recommended Action |
| :--- | :--- | :--- |
| Antigravity closed | `RETRY` | Wait up to 10s, attempt process launch |
| Antigravity minimized | `PASS` | CDP operates normally when minimized |
| Wrong conversation open | `FALLBACK` | Execute `client.switch_conversation(uuid)` |
| Composer unavailable | `BLOCK` | Check if modal dialog is blocking UI |
| Send button disabled after text | `FAIL_SAFE` | Trigger native Ctrl+A + Backspace, retry insert |
| CDP unavailable / port closed | `FALLBACK` | Fallback to repository state rehydration |
| Duplicate conversation titles | `BLOCK` | Require exact UUID |
| Turn already active | `BLOCK` | Abort submission; do not send duplicate |

---

## 4. Safe Prototypes Created

1. `scripts/t03/probe_cdp.py`: Automated discovery and DOM inspection tool.
2. `scripts/t03/inspect_uia.py`: Windows UI Automation & Win32 window inspector.
3. `scripts/t03/inspect_desktop.py`: Antigravity chat structure, composer, and status analyzer.
4. `scripts/t03/send_resume.py`: Production-grade resume adapter with dry-run default, duplicate prevention, and multi-stage verification.

---

## 5. Security Observations

1. **No Secret Exfiltration (`VERIFIED_DOC` / `VERIFIED_RUNTIME`)**: All CDP automation operates strictly over local loopback (`127.0.0.1`).
2. **Zero Credentials in Git (`VERIFIED_RUNTIME`)**: No authentication tokens, cookies, or secrets are recorded or transmitted by the T03 prototype scripts.
3. **Safe Memory Boundaries**: Structured JSON output redacts prompt text beyond length limits in debug logs.

---

## 6. Independent Verification Checklist

The main orchestrator can independently verify all T03 findings using the following steps:

1. **Verify CDP Discovery**:
   ```powershell
   Get-Content "$env:APPDATA\Antigravity\DevToolsActivePort"
   python scripts/t03/probe_cdp.py
   ```
2. **Verify Safe Dry-Run Target Resolution**:
   ```powershell
   python scripts/t03/send_resume.py --uuid 54fa3d23-64f3-4fb4-b790-02cdd1e92d75
   ```
3. **Verify Ambiguous Target Rejection**:
   ```powershell
   python scripts/t03/send_resume.py --title "T0" --json
   ```
4. **Verify Duplicate Prevention on Active Conversation**:
   ```powershell
   python scripts/t03/send_resume.py --uuid 4674ef3b-d559-4a90-87e2-c30b11f03250 --json
   ```

---
