# Research Package Report: Desktop Conversation Restore & Automatic Resume Submission

**Agent ID:** T03  
**Assigned Issue:** #3 — Desktop conversation restore and automatic resume submission  
**Assigned Branch:** `research/T03-conversation-resume`  
**Base SHA:** `3377bf7a0523795a678ac5da951371e3f5ee94c7`  
**Review Iteration:** R3 (Zero-Trust Review Round 2 Compliance)

---

## 1. Executive Summary & Zero-Trust Review Compliance

This updated research package provides verified contracts, automated test suites, and prototype adapters for desktop conversation restoration and resume submission on Antigravity Desktop on Windows.

### Critical Safety Principles Enforced:
- **`DO_NOT_SEND` over `POSSIBLY_SEND_TO_WRONG_THREAD`**: Ambiguous target selection or unverified conversation switching results in immediate fail-safe termination.
- **`DO_NOT_RESEND` over `POSSIBLY_DUPLICATE_RESUME`**: If state is unknown, an agent turn is active, or the prompt signature is already present in conversation history, submission is aborted.

---

## 2. Review Round 2 Corrections

| Item | Problem Addressed | Implementation & Evidence Change | Resulting Classification |
| :--- | :--- | :--- | :--- |
| **CDP Discovery** | Removed static fallback `58859` | Dynamic discovery reads `%APPDATA%\Antigravity\DevToolsActivePort`. Returns `CDP_PORT_FILE_MISSING`, `CDP_PORT_FILE_INVALID`, `CDP_ENDPOINT_UNREACHABLE`. | `VERIFIED_LIVE_RUNTIME` / `UNIT_TEST` |
| **Page Qualification** | Blindly picked `targets[0]` | Enumerate candidates and evaluate independent signals (`hasSidebar`, `hasComposer`, `hasAppConfig`). Returns `APP_PAGE_NOT_FOUND`, `APP_PAGE_AMBIGUOUS`, `APP_PAGE_QUALIFIED`. | `VERIFIED_LIVE_RUNTIME` / `SYNTHETIC_SIMULATION` |
| **Verified Navigation** | Relied on substring and fixed sleep | Exact route check (`pathname === '/c/' + uuid`). Returns `CONVERSATION_SWITCH_VERIFIED`, `CONVERSATION_SWITCH_TIMEOUT`, `CONVERSATION_SWITCH_WRONG_TARGET`. | `VERIFIED_LIVE_RUNTIME` / `SYNTHETIC_SIMULATION` |
| **Duplicate Detection** | Checked generic stop button only | Normalized prompt SHA-256 compared strictly against user messages (`data-author="user"`). Assistant text quoting prompt does not trigger false duplicates. | `VERIFIED_LIVE_RUNTIME` / `SYNTHETIC_SIMULATION` |
| **Scoped Active Turn** | Scanned whole document for stop buttons | Scoped strictly to `main button[aria-label*="Stop"]`, excluding sidebar buttons (`[data-testid="conversation-list-sidebar"]`). Target A stays IDLE when Target B runs. | `VERIFIED_LIVE_RUNTIME` / `SYNTHETIC_SIMULATION` |
| **Baseline Turn Delta** | Pre-existing articles caused false turn start | Baseline captured pre-send (`userMessageCount`, `assistantMessageCount`, `isMainTurnActive`). Post-send evaluates delta against baseline. | `VERIFIED_LIVE_RUNTIME` / `SYNTHETIC_SIMULATION` |
| **Separate States** | Merged user appearance with turn start | Explicit sequence: `SEND_INPUT_DISPATCHED` $\rightarrow$ `USER_MESSAGE_OBSERVED` $\rightarrow$ `ASSISTANT_TURN_STARTED`. Quota failure flags `USER_MESSAGE_OBSERVED_ASSISTANT_PENDING`. | `VERIFIED_LIVE_RUNTIME` / `SYNTHETIC_SIMULATION` |
| **Read-Only Dry-Run** | Default dry run cleared/typed in composer | Default dry-run is strictly read-only. Unsubmitted drafts flag `COMPOSER_DRAFT_PRESENT`. Mutation testing requires `--probe-composer-write`. | `VERIFIED_LIVE_RUNTIME` / `SYNTHETIC_SIMULATION` |
| **Dangerous Override** | Unchecked `--force` flag | Normal `--force` removed. Replaced by explicit `--dangerous-manual-override-do-not-use-in-production`. Autonomous supervisor forbidden from using it. | `UNIT_TEST` |
| **Exact Title Matching** | Substring title match | Automatic title selection requires exact normalized equality. Multi-match returns `CONVERSATION_AMBIGUOUS`. UUID is canonical selector. | `VERIFIED_LIVE_RUNTIME` / `SYNTHETIC_SIMULATION` |
| **Journal Integration** | Journal was standalone | `RecoveryJournal` integrated into `send_resume.py`. Writes `SUBMISSION_ATTEMPTED` BEFORE irreversible input dispatch. | `VERIFIED_LIVE_RUNTIME` / `UNIT_TEST` |
| **Crash Ordering** | Undocumented crash recovery behavior | Detailed state machine transitions and restart rules documented. Unknown state $\rightarrow$ `DO_NOT_RESEND`. | `UNIT_TEST` / `SYNTHETIC_SIMULATION` |
| **Test Suite** | Untracked missing test script | Committed `scripts/t03/test_suite.py` with 20 test cases covering all failure modes. | `UNIT_TEST` / `SYNTHETIC_SIMULATION` |
| **Privacy Audit** | Diagnostic tools dumped raw text | Diagnostic tools emit testid counts, element counts, UUIDs, and SHA-256 hashes by default. Full text requires `--verbose-private-data`. | `VERIFIED_LIVE_RUNTIME` |
| **Documentation Integrity**| Overclaimed production readiness | Downgraded to research prototype, effectively-once guarded recovery protocol, and Session 1 background context. | `VERIFIED_DOC` |

---

## 3. Claim / Evidence Matrix

| Claim | Evidence Class | Test Performed | Raw/Sanitized Artifact | Repro Command | Status | Remaining Gap |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **CDP Discovery** | `VERIFIED_LIVE_RUNTIME` | Read `%APPDATA%\Antigravity\DevToolsActivePort` | Line 1: `58859`, Line 2: `/devtools/browser/...` | `python scripts/t03/probe_cdp.py` | PASS | Hard crash could leave stale file until socket test fails |
| **CDP Page Qualification** | `VERIFIED_LIVE_RUNTIME` | Connect to target, verify `hasSidebar` & `hasComposer` | Qualification JSON: `hasSidebar: true, hasComposer: true` | `python scripts/t03/probe_cdp.py` | PASS | Detached auxiliary windows must be filtered |
| **Conversation UUID Identity** | `VERIFIED_LIVE_RUNTIME` | Extract sidebar links `a[href^="/c/"]` | `uuid: "54fa3d23-..."` matching `.db` filename | `python scripts/t03/inspect_desktop.py` | PASS | Account switch visibility depends on backend auth |
| **Conversation Navigation** | `VERIFIED_LIVE_RUNTIME` | Click sidebar link, poll pathname | `status: "CONVERSATION_SWITCH_VERIFIED"` | `python scripts/t03/send_resume.py --uuid <uuid> --json` | PASS | Network latency on remote backend sync |
| **Composer Targeting** | `VERIFIED_LIVE_RUNTIME` | Locate `[data-lexical-editor="true"]` | `role: "combobox", ariaLabel: "Message input"` | `python scripts/t03/send_resume.py --uuid <uuid> --json` | PASS | Major Electron UI rework could change tag/attribute |
| **Message Submission** | `SYNTHETIC_SIMULATION` / `NOT_LIVE_TESTED` | Native CDP `Input.insertText` + Send click | `send_input_dispatched: true` | `scripts/t03/test_suite.py` | PASS | Actual live destructive prompt submission not executed in test |
| **User Message Observation** | `SYNTHETIC_SIMULATION` | Delta check on user article nodes & prompt hash | `lastUserMessageHash` matching `prompt_sha256` | `scripts/t03/test_suite.py` | PASS | UI rendering delay under heavy CPU load |
| **Assistant Turn Observation** | `VERIFIED_LIVE_RUNTIME` | Monitor scoped `main button[aria-label*="Stop"]` | `isMainTurnActive: true` | `python scripts/t03/inspect_desktop.py` | PASS | Instantaneous 1-token error might not hold stop button |
| **Duplicate Prevention** | `VERIFIED_LIVE_RUNTIME` | Inspect history hash and scoped active turn | Status: `TURN_ALREADY_ACTIVE` / `RESUME_MESSAGE_PRESENT` | `python scripts/t03/send_resume.py --uuid <uuid> --json` | PASS | Non-standard prompts without consistent formatting |
| **Crash Recovery Journal** | `UNIT_TEST` | Atomic state transition & corrupt quarantine | `t03_recovery_journal.json` (.tmp + replace) | `python scripts/t03/test_suite.py` | PASS | OS-level filesystem lock failure |
| **UIA Limitation** | `OBSERVED_LIVE_RUNTIME` | `uiautomation.GetRootControl().GetChildren()` in Session 1 | `Total root controls enumerated: 0` | `python scripts/t03/inspect_uia.py` | PASS | Requires desktop interactive window station |

---

## 4. Crash Window Analysis & Journal Write Ordering

The critical recovery sequence is strictly ordered:
```text
1. Inspect Target DOM
   ↓ (Crash here: state NOT_SENT -> Watchdog resends safely)
2. Verify Target & Duplicate State
   ↓ (Crash here: state NOT_SENT -> Watchdog resends safely)
3. Durably write SUBMISSION_ATTEMPTED to journal (.tmp + atomic replace)
   ↓ (Crash here: state SUBMISSION_ATTEMPTED -> Watchdog inspects DOM first; DO_NOT_RESEND if unknown)
4. Dispatch Input (Button click or Enter key)
   ↓ (Crash here: state SUBMISSION_ATTEMPTED -> Watchdog inspects DOM first; DO_NOT_RESEND if prompt present)
5. Observe User Message in DOM
   ↓ (Crash here: state MESSAGE_OBSERVED -> Prompt present in DOM; DO_NOT_RESEND)
6. Durably write MESSAGE_OBSERVED to journal
   ↓ (Crash here: state MESSAGE_OBSERVED -> Prompt present in DOM; DO_NOT_RESEND)
7. Observe Assistant Turn Start (Stop button / assistant delta)
   ↓ (Crash here: state TURN_STARTED -> Turn active; DO_NOT_RESEND)
8. Durably write TURN_STARTED to journal
```

---

## 5. Automated Failure Test Matrix (20 Test Cases)

| Test Name | Evidence Class | Verified Behavior |
| :--- | :--- | :--- |
| `test_01_journal_lifecycle` | `UNIT_TEST` | Verifies full state transition and history logging |
| `test_02_corrupt_journal` | `UNIT_TEST` | Quarantines corrupted JSON; initializes clean state |
| `test_03_duplicate_prompt_as_last_user_message` | `SYNTHETIC_SIMULATION` | Identical prompt in last user message triggers `RESUME_MESSAGE_PRESENT` |
| `test_04_same_prompt_earlier_not_latest` | `SYNTHETIC_SIMULATION` | Old prompt 5 turns ago allows resume if latest turn differs |
| `test_05_assistant_contains_same_text` | `SYNTHETIC_SIMULATION` | Assistant quoting prompt does not trigger false duplicate |
| `test_06_target_a_idle_while_sidebar_b_active` | `SYNTHETIC_SIMULATION` | Target A main pane evaluates to IDLE despite active sidebar items |
| `test_07_old_articles_do_not_cause_new_turn_success` | `SYNTHETIC_SIMULATION` | Baseline delta tracking prevents pre-existing messages from triggering turn start |
| `test_08_user_message_observed_assistant_not_started` | `SYNTHETIC_SIMULATION` | Separates user message receipt from assistant generation start (quota fail) |
| `test_09_existing_composer_draft_detected` | `SYNTHETIC_SIMULATION` | Unsubmitted draft flags `COMPOSER_DRAFT_PRESENT` and preserves user text |
| `test_10_missing_devtools_active_port` | `UNIT_TEST` | Missing port file returns `CDP_PORT_FILE_MISSING` without guessing |
| `test_11_stale_unreachable_devtools_endpoint` | `UNIT_TEST` | Unreachable endpoint returns `CDP_ENDPOINT_UNREACHABLE` |
| `test_12_multiple_page_candidates_ambiguous` | `SYNTHETIC_SIMULATION` | $>1$ qualified pages without unambiguous route returns `APP_PAGE_AMBIGUOUS` |
| `test_13_no_qualified_page` | `SYNTHETIC_SIMULATION` | No matching application page returns `APP_PAGE_NOT_FOUND` |
| `test_14_slow_navigation_timeout` | `SYNTHETIC_SIMULATION` | Timeout during route switch returns `CONVERSATION_SWITCH_TIMEOUT` |
| `test_15_wrong_navigation_target` | `SYNTHETIC_SIMULATION` | Route redirection returns `CONVERSATION_SWITCH_WRONG_TARGET` |
| `test_16_exact_title_collision` | `SYNTHETIC_SIMULATION` | Duplicate exact titles return `CONVERSATION_AMBIGUOUS` |
| `test_17_crash_before_send_resend_permitted` | `UNIT_TEST` | `NOT_SENT` allows safe watchdog resend |
| `test_18_crash_after_submission_attempted_inspect_dom`| `UNIT_TEST` | `SUBMISSION_ATTEMPTED` requires DOM inspection before resending |
| `test_19_crash_after_message_observed_do_not_resend` | `UNIT_TEST` | `MESSAGE_OBSERVED` strictly blocks duplicate resume |
| `test_20_repeated_recovery_invocation_blocked` | `SYNTHETIC_SIMULATION` | Consecutive recovery calls on same thread abort safely |

---

## 6. Official CLI Capabilities Recheck

| Capability | Doc Reference | Local Binary Status | Evidence Class | Finding |
| :--- | :--- | :--- | :--- | :--- |
| **Thread List** | `references/cli.md` | `CLI_NOT_INSTALLED_LOCALLY` | `VERIFIED_DOC` | Supported via standalone `agy` CLI commands in docs |
| **Desktop DB Import** | N/A | `NOT_SUPPORTED` | `UNKNOWN` | No documentation establishing direct import of Electron `.db` into `agy` |
| **Resume CLI Session** | `references/cli.md` | `CLI_NOT_INSTALLED_LOCALLY` | `VERIFIED_DOC` | Standalone CLI supports resuming its own past sessions |
| **Continue Desktop Thread via CLI**| N/A | `NOT_SUPPORTED` | `NOT_SUPPORTED_IN_INSPECTED_BINARY` | `language_server.exe` does not expose a standalone desktop resume command |

---

## 7. Independent Repro Checklist for Main Orchestrator

To independently verify the updated T03 package:

1. **Verify Automated Failure & Recovery Test Suite (All 20 tests pass)**:
   ```powershell
   python scripts/t03/test_suite.py
   ```
2. **Verify Read-Only Dry-Run (No DOM mutation, scoped duplicate detection)**:
   ```powershell
   python scripts/t03/send_resume.py --uuid 54fa3d23-64f3-4fb4-b790-02cdd1e92d75 --json
   ```
3. **Verify Stale Port Rejection (No hardcoded fallback)**:
   ```powershell
   python scripts/t03/send_resume.py --cdp-endpoint http://127.0.0.1:59999 --json
   ```
4. **Verify Privacy-Hardened Desktop Inspection**:
   ```powershell
   python scripts/t03/inspect_desktop.py
   ```
5. **Verify Windows Session & UIA Subshell Observation**:
   ```powershell
   python scripts/t03/inspect_uia.py
   ```
