# Adversarial Self-Review (T03)

## Objective

Actively attempt to break, invalidate, or challenge the proposed desktop conversation restore and automatic resume submission architecture before it reaches the main orchestrator.

---

### 1. Does the selector survive app restart?
- **CDP DevTools Port**: Electron writes a fresh ephemeral port to `%APPDATA%\Antigravity\DevToolsActivePort` upon every launch. A static port (e.g. `9234`) will FAIL after restart unless configured via `--remote-debugging-port=9234`. Our auto-discovery reads `DevToolsActivePort` directly, which survives restarts. However, if the process is killed before writing the file or if a stale file remains from a hard crash, the watchdog could read a stale port until socket verification fails.
- **Conversation UUID**: The conversation UUID (e.g. `54fa3d23-64f3-4fb4-b790-02cdd1e92d75`) is persisted in `~/.gemini/antigravity/conversations/<uuid>.db` and mapped to `a[href^="/c/<uuid>"]`. This is persistent across app restarts.
- **DOM Selectors**: `[data-lexical-editor="true"]` and `button[data-testid="send-button"]` survive restarts in Antigravity 2.10.x, but could be altered during major frontend updates.

---

### 2. Does the selector survive account switch?
- **Conversation Isolation**: If an account switch wipes or isolates the local session storage, does the conversation link `a[href^="/c/<uuid>"]` remain in the sidebar?
  - If the conversation was tied strictly to Account A, Account B might not see the conversation in its sidebar list.
  - If the conversation is NOT visible in the UI after account switch, navigating directly to `/c/<uuid>` might redirect to `/` or throw an unauthenticated/unauthorized error.
  - **Mitigation**: The watchdog must treat missing conversation as `CONVERSATION_INACCESSIBLE` and trigger durable repository-level context rehydration rather than blindly creating an empty conversation.

---

### 3. Could two conversations match?
- **Title Matching Risk**: Matching by title (`--title`) has high collision risk (e.g., multiple chats titled "T01" or "Bugfix").
- **Mitigation**: Our implementation strictly enforces UUID-first resolution (`--uuid`). Title matching will fail-safe with `CONVERSATION_AMBIGUOUS` if more than one conversation matches. Never use "first visible conversation" or partial titles in automated production recovery.

---

### 4. Could CDP target the wrong window?
- **Multi-Window Electron Apps**: Antigravity may open auxiliary windows (e.g., Settings dialog, detached diff window, background worker page).
- **Target Selection Risk**: Querying `/json/list` returns multiple targets. Blindly picking `targets[0]` could connect to a background webview or developer tool instead of the main chat workspace.
- **Mitigation**: The CDP client filters by `type === "page"` and verifies the presence of `window.location.href.includes('/c/')` and the `[data-lexical-editor="true"]` DOM node before attempting any interactions.

---

### 5. Could UIA find the wrong textbox?
- **Native Accessibility Tree in Chromium**: In Windows UI Automation, Chromium webviews often expose a generic `DocumentControl` with unlabeled `EditControl` elements. Without `--force-renderer-accessibility`, standard UIA cannot reliably differentiate the chat composer from search bars, file filters, or terminal inputs.
- **Verdict**: UIA is fragile for Lexical rich-text input. CDP is strictly preferred.

---

### 6. Could prompt be inserted but not sent?
- **Risk**: Lexical is a React-managed contenteditable framework. If text is injected via `element.innerText = text` or `document.execCommand('insertText')` without triggering React/Lexical's synthetic event bus, the visual DOM might show text while React internal state remains empty (`send-button` remains disabled).
- **Runtime Verification**: We verified that `Input.insertText` and native `Input.dispatchKeyEvent` via CDP properly notify Lexical, causing `button[data-testid="send-button"]` to transition from `disabled: true` to `disabled: false`.
- **Safety Gate**: The pipeline includes a discrete `3_text_verified` phase that verifies `send-button.disabled === false` before triggering submission.

---

### 7. Could prompt be sent twice?
- **Crash Window Risk**: If the watchdog sends the prompt, the agent starts thinking, and the watchdog process crashes before recording `TURN_STARTED`, a subsequent watchdog restart might re-submit the resume prompt.
- **Mitigation**: Before any submission, the watchdog checks:
  1. Is a turn already executing (`Stop Task` button present / `isExecuting: true`)?
  2. Does the last message in history already match the resume prompt?
  If either is true, submission is ABORTED with `TURN_ALREADY_ACTIVE` / `RESUME_ALREADY_PRESENT`.
- **Classification**: Due to network/process crash race conditions, the system achieves **EFFECTIVELY_ONCE** (idempotent submission guarded by state inspection), not mathematically strict ACID exactly-once.

---

### 8. Could Antigravity reject the message after the UI accepts it?
- **Risk**: Quota exhaustion might occur immediately upon submission, resulting in a toast/banner ("Quota exceeded", "Rate limit reached") while the UI appears to have accepted the message.
- **Mitigation**: The supervisor must not treat turn start as proof of task completion. It must continuously monitor for quota error banners or terminal stops.

---

### 9. Could a turn start without our detector seeing it?
- **Risk**: If the LLM produces an instantaneous one-token error or stops immediately, the `Stop Task` button might flash for <100ms and disappear before our polling loop observes it.
- **Mitigation**: Our detector checks both `hasStopButton` and the appearance of a new `[role="article"]` node / cleared composer state.

---

### 10. Could app updates break selectors?
- **Risk**: An Antigravity desktop update from 2.10.x to 3.x could rename `data-lexical-editor` or `data-testid="send-button"`.
- **Mitigation**:
  1. OpenCLI failed precisely because it relied on outdated `antigravity.agentSidePanelInputBox` and `convo-pill-` testids.
  2. The watchdog adapter must implement multi-attribute selector cascades (`data-testid`, `aria-label="Message input"`, `role="combobox"`).
  3. If all selectors fail, fail-safe immediately with `COMPOSER_NOT_FOUND` instead of guessing coordinates.

---

### 11. Could fallback unexpectedly create a new conversation?
- **Risk**: If the intended conversation is missing, an aggressive recovery script might click "New Conversation", polluting the workspace and losing previous conversation context.
- **Mitigation**: Strict policy: NEVER create a new conversation silently. If identity resolution fails, return `CONVERSATION_NOT_FOUND` / `CONVERSATION_AMBIGUOUS` and let the supervisor invoke repository-level rehydration.
