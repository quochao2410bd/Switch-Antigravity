# Adversarial Self-Review (T03 - Revision 6 Final)

## Objective

Actively challenge and stress-test the proposed desktop conversation restore and automatic resume submission architecture under zero-trust assumptions.

---

### 1. Does the selector survive app restart?
- **DevTools Port**: Electron writes a fresh ephemeral port to `%APPDATA%\Antigravity\DevToolsActivePort` on startup. Static port fallback (`58859` / `9234`) has been strictly removed. If the port file is missing or stale, `CDP_PORT_FILE_MISSING` or `CDP_ENDPOINT_UNREACHABLE` is returned.
- **Conversation UUID**: The UUID (`/c/<uuid>`) persists in SQLite (`~/.gemini/antigravity/conversations/<uuid>.db`) and remains stable across application restarts.
- **DOM Selectors**: `[data-lexical-editor="true"]` and `[data-testid="send-button"]` survive restarts in Antigravity 2.10.x. If modified in future Electron builds, the adapter fails safe with `COMPOSER_NOT_FOUND` rather than guessing coordinates.

---

### 2. Does the selector survive account switch?
- **Visibility Boundary**: If Account B cannot view conversations started under Account A, direct navigation to `/c/<uuid>` will fail or redirect.
- **Fail-Safe Response**: Exact route verification in `switch_conversation_verified()` detects redirection or timeout and fails with `CONVERSATION_SWITCH_WRONG_TARGET` or `CONVERSATION_SWITCH_TIMEOUT`, invoking repository-level rehydration rather than creating an empty conversation.

---

### 3. Could two conversations match?
- **Exact Equality Contract**: Title matching has been restricted to exact normalized string equality. Substring matching is forbidden for automatic sending.
- **Disambiguation**: If $>1$ conversations share the same exact title, the adapter terminates with `CONVERSATION_AMBIGUOUS`. UUID remains the only automated production selector.

---

### 4. Could CDP target the wrong window?
- **Multi-Page Qualification**: `connect_and_qualify()` inspects Antigravity application headers, sidebar existence, and Lexical composer nodes across all `page` targets.
- **Ambiguity Guard**: If multiple candidate pages match without an unambiguous `/c/` URL, the client terminates with `APP_PAGE_AMBIGUOUS`.

---

### 5. Could UIA find the wrong textbox?
- **Native UIA Limitation**: UIA root enumeration returns 0 controls in background subshell executions in Session 1 (`UIA_BACKGROUND_CONTEXT_OBSERVED`).
- **Verdict**: UIA is fragile and unsuitable for production recovery. CDP is the primary tier.

---

### 6. Could prompt be inserted but not sent?
- **Verification Gate**: Lexical state is verified immediately before dispatching input. If `verified_comp.text != prompt`, the operation fails with `TEXT_INSERTION_FAILED` and records `STATE_FAILED` (`PRE_IRREVERSIBLE`) in the journal.
- **State Semantics**: `SUBMISSION_ATTEMPTED` is durably written before input dispatch. If input fails to dispatch, it transitions to `FAILED` (`PRE_IRREVERSIBLE`).

---

### 7. Could prompt be sent twice?
- **In-Lock Decision Policy**: Authoritative evaluation occurs strictly inside `exclusive_lock()`.
  1. Re-reads latest journal state from disk.
  2. Inspects scoped main pane for `isMainTurnActive` (Stop button present).
  3. Inspects user message hash (`lastUserMessageHash == prompt_hash`).
- **Verdict**: Restarted watchdog after `SUBMISSION_ATTEMPTED` or `MESSAGE_OBSERVED` **NEVER** blindly resends. Evaluates to `PREVIOUS_SUBMISSION_UNCONFIRMED` or `RESUME_ALREADY_OBSERVED`.

---

### 8. Could a turn start without detector observing it?
- **Baseline Delta Tracking**: Captures baseline message counts before dispatching input. Detects both scoped `Stop Task` button, new assistant article nodes, and quota/error responses.

---

### 9. Could fallback unexpectedly create a new conversation?
- **Strict Prohibition**: The adapter strictly refuses to click "New Conversation" automatically. Missing targets return `CONVERSATION_NOT_FOUND` or `CONVERSATION_SWITCH_TIMEOUT`.
