# Research Package Report: Desktop Conversation Restore & Automatic Resume Submission

**Agent ID:** T03  
**Assigned Issue:** #3 — Desktop conversation restore and automatic resume submission  
**Assigned Branch:** `research/T03-conversation-resume`  
**Base SHA:** `3377bf7a0523795a678ac5da951371e3f5ee94c7`  
**Review Iteration:** R7 (Zero-Trust Review Round 6 Compliance — Final Closure)

---

## 1. Architecture Boundary & Executive Summary

T03 owns **ONLY** the Antigravity Desktop conversation restoration, verification, and resume submission adapter. It does **NOT** implement an account database, account rotation, multi-account manager, or credential manager; all multi-account coordination is delegated to the supervisor and T02 AGM integration.

### Core Hardened Principles:
- **`DO_NOT_SEND` over `POSSIBLY_SEND_TO_WRONG_THREAD`**: Ambiguous target selection, route pathname divergence, missing target container, or multiple composers fail closed immediately.
- **`DO_NOT_RESEND` over `POSSIBLY_DUPLICATE_RESUME`**: If state is unconfirmed, an agent turn is active, or the prompt signature exists in conversation history, submission is aborted.
- **In-Lock Non-Reentrant Authoritative Path**: Authoritative evaluation, fresh disk re-read, reconciliation, reservation, text insertion, durable `SUBMISSION_ATTEMPTED` barrier, and input dispatch are strictly executed **inside** an exclusive non-reentrant cross-process lock using explicit unlocked private methods.

---

## 2. Review Round 6 Defect Remediation (Items 1 – 14)

| Item | Problem Addressed | Production Change | Resulting Classification |
| :--- | :--- | :--- | :--- |
| **Item 1** | Same-process lock bypass via depth counter | Eliminated `_lock_depth` counter bypass. External `exclusive_lock` is strictly non-reentrant. Internal operations inside an existing lock invoke explicit private methods (`_start_recovery_attempt_unlocked`, `_transition_state_unlocked`, `_reconcile_existing_attempt_unlocked`). | `UNIT_TESTED_BRANCH` |
| **Item 2** | Non-overlapping race test | Implemented `test_01_deterministic_overlapping_two_worker_pipeline_race` with an explicit `AsyncBarrier(2)` synchronizing both workers at the pre-lock inspection boundary. Proved total dispatch count = 1 across multiple iterations. | `UNIT_TESTED_BRANCH` |
| **Item 3** | Prompt hash checked after irreversible click | Moved prompt verification directly into renderer JS inside `dispatch_submission_input()`. Normalizes and asserts equality between current composer text and expected prompt BEFORE clicking Send. Zero clicks on mismatch. | `UNIT_TESTED_BRANCH` |
| **Item 4** | Enter key fallback in autonomous safe path | Completely removed Enter key fallback from the autonomous supervisor path. Submission strictly requires a single verified, visible, enabled send button (`SEND_CONTROL_NOT_FOUND` / `SEND_CONTROL_DISABLED` fail closed). | `UNIT_TESTED_BRANCH` |
| **Item 5** | Binary OpenProcess == 0 liveness check | Implemented tri-state process liveness checker (`check_process_liveness`): `PROCESS_ALIVE`, `PROCESS_DEAD_CONFIRMED`, `PROCESS_LIVENESS_UNKNOWN`. Inspects Win32 `GetLastError()` (e.g. `ERROR_ACCESS_DENIED` $\rightarrow$ UNKNOWN). Stale lock reclaimed ONLY for `PROCESS_DEAD_CONFIRMED`. | `UNIT_TESTED_BRANCH` |
| **Item 6** | PID reuse vulnerability | Added `start_identity` (process creation timestamp via `GetProcessTimes` on Windows / `/proc/<pid>/stat` on Unix) and `lock_nonce` to lock metadata. If PID is alive but start identity differs, lock is recognized as stale. | `UNIT_TESTED_BRANCH` |
| **Item 7** | Conflated mainContainer DOM scope | Formally separated DOM scopes: `targetRoot = document.querySelector('main')`, `messageContainer = targetRoot.querySelector('[data-testid="conversation-messages"]')`, `composer = targetRoot.querySelector('[data-lexical-editor="true"]')`, `sendControl = targetRoot.querySelector('button[data-testid="send-button"]')`. Zero `document.body` fallback. | `UNIT_TESTED_BRANCH` |
| **Item 8** | Unexercised selector decision logic | Refactored renderer state queries into structured tests verifying handling of 0/1/2 composers, hidden composers, and 0/1/2 send buttons. | `UNIT_TESTED_BRANCH` |
| **Item 9** | Post-dispatch transition lock ownership | Public `transition_state()` acquires exclusive lock before updating state on disk. In-lock pipeline phases use explicit `_transition_state_unlocked()`. | `UNIT_TESTED_BRANCH` |
| **Item 10** | Crash tests with stale locks & journal states | Implemented unit test simulating dead owner lock with journal in `STATE_SUBMISSION_ATTEMPTED`. Reclaims lock, re-reads disk, evaluates `PREVIOUS_SUBMISSION_UNCONFIRMED`, and blocks send (0 dispatches). | `UNIT_TESTED_BRANCH` |
| **Item 11** | Route mutation immediately before send | `dispatch_submission_input()` asserts exact route `/c/<target_uuid>` inside JS immediately before clicking. Route divergence returns `ROUTE_MUTATED_BEFORE_DISPATCH` (0 dispatches). | `UNIT_TESTED_BRANCH` |
| **Item 12** | Prompt mutation immediately before send | In-renderer check asserts normalized text equality before click. Mismatches return `PROMPT_IDENTITY_MISMATCH` (0 dispatches). | `UNIT_TESTED_BRANCH` |
| **Item 13** | Send button ambiguity (>1 send button) | In-renderer check requires `sendBtns.length === 1`. Multiple matching buttons return `SEND_CONTROL_AMBIGUOUS` and fail closed (0 dispatches). | `UNIT_TESTED_BRANCH` |
| **Item 14** | Exact synchronized race test claim | Confirmed via deterministic barrier-synchronized race across 3 iterations: Worker A = `TURN_STARTED`, Worker B = `TURN_ALREADY_ACTIVE` / `PREVIOUS_SUBMISSION_UNCONFIRMED`, Total Dispatches = 1. | `UNIT_TESTED_BRANCH` |

---

## 3. Final Lock Ordering & In-Lock Private Method Architecture

```text
[Pipeline Execution]
  │
  ├─► Pre-lock inspection (Read-only UI/route inspection for telemetry)
  │
  ├─► Barrier synchronization (Optional deterministic testing gate)
  │
  ├─► with journal.exclusive_lock(conversation_uuid):
  │     │
  │     ├─► 1. Re-read fresh journal state from disk & validate schema
  │     ├─► 2. Re-inspect target route (/c/<uuid>) & targetRoot DOM
  │     ├─► 3. Re-inspect composer state (draft present check)
  │     ├─► 4. journal._reconcile_existing_attempt_unlocked()
  │     ├─► 5. Re-read latest record if mutated by reconciliation
  │     ├─► 6. evaluate_recovery_permission()
  │     │       └─► If NOT NEW_ATTEMPT_ALLOWED: release lock & DO NOT SEND
  │     ├─► 7. journal._start_recovery_attempt_unlocked() (STATE_NOT_SENT)
  │     ├─► 8. Insert prompt text into target composer
  │     ├─► 9. Verify inserted text SHA-256 matches intended prompt
  │     ├─► 10. journal._transition_state_unlocked() (STATE_SUBMISSION_ATTEMPTED)
  │     ├─► 11. dispatch_submission_input() (Atomic in-renderer pre-check & click)
  │     └─► 12. If dispatch fails: journal._transition_state_unlocked(STATE_FAILED)
  │
  └─► Outside lock: Observe post-dispatch turns (wait_for_user_and_assistant_turn)
        └─► On state transition: journal.transition_state() (acquires lock -> writes -> releases)
```

---

## 4. Deterministic Overlapping Two-Worker Pipeline Race Result

- **Test Method:** `test_01_deterministic_overlapping_two_worker_pipeline_race` (3 iterations)
- **Synchronization:** `AsyncBarrier(2)` synchronizing both workers after pre-lock inspection.
- **Worker A Status:** `TURN_STARTED` (dispatched input)
- **Worker B Status:** `TURN_ALREADY_ACTIVE` / `PREVIOUS_SUBMISSION_UNCONFIRMED` (in-lock re-read blocked submission)
- **TOTAL DISPATCH COUNT ACROSS BOTH WORKERS:** **1**

---

## 5. Automated Test Suite Results (20 Production Tests)

Command executed:
```powershell
python scripts/t03/test_suite.py
```
Output: `Ran 20 tests in 0.281s - OK`

- `test_01_deterministic_overlapping_two_worker_pipeline_race`: `UNIT_TESTED_BRANCH` — PASSED
- `test_02_non_reentrant_lock_prevents_same_process_bypass`: `UNIT_TESTED_BRANCH` — PASSED
- `test_03_pre_dispatch_prompt_mutation_aborts_send`: `UNIT_TESTED_BRANCH` — PASSED
- `test_04_send_button_ambiguity_aborts_send`: `UNIT_TESTED_BRANCH` — PASSED
- `test_05_tri_state_liveness_and_pid_reuse`: `UNIT_TESTED_BRANCH` — PASSED
- `test_06_real_selector_decision_logic`: `UNIT_TESTED_BRANCH` — PASSED
- `test_07_crash_reclaim_with_submission_attempted`: `UNIT_TESTED_BRANCH` — PASSED
- `test_08_route_mutation_before_send_aborts`: `UNIT_TESTED_BRANCH` — PASSED
- `test_09_strict_journal_schema_validation`: `UNIT_TESTED_BRANCH` — PASSED
- `test_10_failed_unknown_stage_fails_closed`: `UNIT_TESTED_BRANCH` — PASSED
- `test_11_post_baseline_error_tracking`: `UNIT_TESTED_BRANCH` — PASSED
- `test_12_semantic_author_identification`: `UNIT_TESTED_BRANCH` — PASSED
- `test_13_fsync_failure_raises_durability_error`: `UNIT_TESTED_BRANCH` — PASSED
- `test_14_dry_run_navigation_restoration`: `UNIT_TESTED_BRANCH` — PASSED
- `test_15_pipeline_duplicate_blocks_send`: `UNIT_TESTED_BRANCH` — PASSED
- `test_16_pipeline_draft_blocks_send`: `UNIT_TESTED_BRANCH` — PASSED
- `test_17_pipeline_navigation_failure_blocks`: `UNIT_TESTED_BRANCH` — PASSED
- `test_18_pipeline_post_dispatch_timeout`: `UNIT_TESTED_BRANCH` — PASSED
- `test_19_external_error_hook_integration`: `UNIT_TESTED_BRANCH` — PASSED
- `test_20_illegal_state_transitions_rejected`: `UNIT_TESTED_BRANCH` — PASSED

---

## 6. Privacy Audit & Evidence Boundary

- **Audited Patterns:** `Users\\`, `bearer`, `api_key`, `password`, un-sanitized UUIDs.
- **Audit Result:** **NO MATCHES FOR AUDITED PATTERNS** (Exit code 1).
- **Live Evidence Boundary:**
  - `REAL SEND = NOT_LIVE_TESTED` (non-destructive safety policy).
  - All claims backed by live read-only component discovery, unit assertions, and synchronized concurrency simulations.
  - **No Claim of Mathematically Guaranteed Exactly-Once**: Hardened against duplicate sends, crash recovery fails closed, concurrent local watchdog send exclusion tested.

---

## 7. Remaining Unknown

1. Electron renderer crash behavior during WebSocket frame transmission.
2. Latency variations in DOM node rendering under severe host paging pressure.
