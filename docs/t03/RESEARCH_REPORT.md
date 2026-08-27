# Research Package Report: Desktop Conversation Restore & Automatic Resume Submission

**Agent ID:** T03  
**Assigned Issue:** #3 — Desktop conversation restore and automatic resume submission  
**Assigned Branch:** `research/T03-conversation-resume`  
**Base SHA:** `3377bf7a0523795a678ac5da951371e3f5ee94c7`  
**Review Iteration:** R8 (Zero-Trust Review Round 7 Compliance — Final Async-Runtime Closure)

---

## 1. Architecture Boundary & Multi-Manager Segregation

T03 owns **ONLY** the Antigravity Desktop conversation restoration, verification, and resume submission adapter. It does **NOT** implement an account database, account rotation, multi-account manager, or credential manager; all multi-account coordination is delegated to the supervisor and T02 AGM integration.

### Core Hardened Principles:
- **`DO_NOT_SEND` over `POSSIBLY_SEND_TO_WRONG_THREAD`**: Ambiguous target selection, route pathname divergence, missing target container, or multiple composers fail closed immediately.
- **`DO_NOT_RESEND` over `POSSIBLY_DUPLICATE_RESUME`**: If state is unconfirmed, an agent turn is active, or the prompt signature exists in conversation history, submission is aborted.
- **In-Lock Non-Blocking Async Architecture**: Authoritative evaluation, fresh disk re-read, reconciliation, reservation, text insertion, durable `SUBMISSION_ATTEMPTED` barrier, and input dispatch are strictly executed **inside** an async non-blocking cross-process lock (`async_exclusive_lock`) using explicit unlocked private methods.

---

## 2. Review Round 7 Defect Remediation (Items 1 – 14)

| Item | Problem Addressed | Production Change | Resulting Classification |
| :--- | :--- | :--- | :--- |
| **Item 1** | Binary/fail-open Win32 process liveness check | `check_process_liveness()` returns `PROCESS_DEAD_CONFIRMED` only for `ERROR_INVALID_PARAMETER` (87) / `ERROR_NOT_FOUND` (1168). `ERROR_ACCESS_DENIED` (5) and all unrecognized Win32 errors (e.g. 12345) return `PROCESS_LIVENESS_UNKNOWN` (fail closed). | `UNIT_TESTED_BRANCH` |
| **Item 2 & 9** | Blind lock file deletion on context release | `async_exclusive_lock()` and `exclusive_lock()` verify that on-disk `owner_pid`, `start_identity`, and `lock_nonce` match the releasing context before deleting the lock file, strictly protecting successor locks. | `UNIT_TESTED_BRANCH` |
| **Item 3** | Unsafe stale lock deletion races | Reclaiming confirmed-dead locks uses atomic quarantine rename (`lock_path.stale.<nonce>.tmp`), verified against the inspected nonce before removal. | `UNIT_TESTED_BRANCH` |
| **Item 4 & 5** | Synchronous file lock blocking event loop | Implemented non-blocking `@asynccontextmanager async_exclusive_lock()` with `await asyncio.sleep(0.05)`. Tested with `YieldingMockAntigravityClient` yielding during all in-lock calls; proved event loop heartbeat ticks > 5 and dispatch count = 1. | `UNIT_TESTED_BRANCH` |
| **Item 6** | Real send mode permitted title / active fallback | `execute_resume_pipeline()` strictly requires an explicit validated UUID when `send=True`. Title-only and implicit active selection return `UUID_REQUIRED_FOR_SEND` (0 dispatches). | `UNIT_TESTED_BRANCH` |
| **Item 7 & 8** | Fallback to generic main on missing message container | `targetRoot.querySelector('[data-testid="conversation-messages"]')` is strictly enforced. Missing message container returns `MESSAGE_CONTAINER_NOT_FOUND` and fails closed (0 dispatches). | `UNIT_TESTED_BRANCH` |
| **Item 10** | Malformed lock file handling | `validate_lock_metadata()` enforces required types and positive timestamps. Malformed lock files fail closed without blind deletion. | `UNIT_TESTED_BRANCH` |
| **Item 11** | Post-dispatch transitions under concurrency | Forward-only journal transitions enforced; unconfirmed previous submissions block subsequent attempts. | `UNIT_TESTED_BRANCH` |
| **Item 12** | Submission attempted failure semantics | Pre-dispatch validation failures transition to `FAILED` with `PRE_IRREVERSIBLE`. Dispatch exceptions (CDP disconnect/crash) transition to `FAILED` with `POST_IRREVERSIBLE_UNKNOWN`, strictly blocking blind resend. | `UNIT_TESTED_BRANCH` |
| **Item 13** | Ambiguous target root element | `document.querySelectorAll('main')` filtered for visible elements. Multiple visible roots return `TARGET_ROOT_AMBIGUOUS` and fail closed (0 dispatches). | `UNIT_TESTED_BRANCH` |
| **Item 14** | Exact evidence and non-destructive standards | Maintained `REAL SEND = NOT_LIVE_TESTED` safety baseline; no claim of mathematically guaranteed exactly-once; local concurrent send exclusion tested. | `VERIFIED_DOC` |

---

## 3. Final Lock Ordering & In-Lock Async Method Architecture

```text
[Pipeline Execution]
  │
  ├─► Pre-lock inspection (Read-only UI/route inspection for telemetry)
  │
  ├─► Barrier synchronization (Optional deterministic testing gate)
  │
  ├─► async with journal.async_exclusive_lock(conversation_uuid):
  │     │
  │     ├─► 1. Re-read fresh journal state from disk & validate schema
  │     ├─► 2. Re-inspect target route (/c/<uuid>) & unambiguous targetRoot DOM
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
  │     └─► 12. If dispatch fails: journal._transition_state_unlocked(STATE_FAILED, PRE_IRREVERSIBLE or POST_IRREVERSIBLE_UNKNOWN)
  │
  └─► Outside lock: Observe post-dispatch turns (wait_for_user_and_assistant_turn)
        └─► On state transition: await journal.transition_state_async() (acquires async lock -> writes -> releases)
```

---

## 4. Yielding Two-Worker Pipeline Race Result

- **Test Method:** `test_01_real_yielding_same_loop_contention` (3 iterations)
- **Client Implementation:** `YieldingMockAntigravityClient` (explicit `await asyncio.sleep(0.02)` inside all locked operations)
- **Synchronization:** `AsyncBarrier(2)` synchronizing both workers after pre-lock inspection.
- **Worker A Status:** `TURN_STARTED` (dispatched input)
- **Worker B Status:** `PREVIOUS_SUBMISSION_UNCONFIRMED` / `TURN_ALREADY_ACTIVE` (in-lock fresh re-read blocked submission)
- **Event Loop Heartbeat:** Responsive throughout execution (heartbeat ticks > 5)
- **TOTAL DISPATCH COUNT ACROSS BOTH WORKERS:** **1**

---

## 5. Automated Test Suite Results (22 Production Tests)

Command executed:
```powershell
python scripts/t03/test_suite.py
```
Output: `Ran 22 tests in 0.757s - OK`

- `test_01_real_yielding_same_loop_contention`: `UNIT_TESTED_BRANCH` — PASSED
- `test_02_win32_liveness_error_matrix`: `UNIT_TESTED_BRANCH` — PASSED
- `test_03_lock_release_verifies_ownership_nonce`: `UNIT_TESTED_BRANCH` — PASSED
- `test_04_real_send_requires_explicit_uuid`: `UNIT_TESTED_BRANCH` — PASSED
- `test_05_send_button_ambiguity_aborts_send`: `UNIT_TESTED_BRANCH` — PASSED
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
- **Audit Result:** **NO MATCHES FOR AUDITED PATTERNS** (0 matches found, Exit code 1).
- **Live Evidence Boundary:**
  - `REAL SEND = NOT_LIVE_TESTED` (non-destructive safety policy).
  - **No Claim of Mathematically Guaranteed Exactly-Once**: Hardened against duplicate sends, crash recovery fails closed, concurrent local watchdog send exclusion tested.

---

## 7. Remaining Unknown

1. Electron renderer crash behavior during WebSocket frame transmission.
2. Latency variations in DOM node rendering under severe host paging pressure.
