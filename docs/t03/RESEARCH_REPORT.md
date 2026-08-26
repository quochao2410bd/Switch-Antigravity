# Research Package Report: Desktop Conversation Restore & Automatic Resume Submission

**Agent ID:** T03  
**Assigned Issue:** #3 — Desktop conversation restore and automatic resume submission  
**Assigned Branch:** `research/T03-conversation-resume`  
**Base SHA:** `3377bf7a0523795a678ac5da951371e3f5ee94c7`  
**Review Iteration:** R6 (Zero-Trust Review Round 5 Compliance — Final Closure)

---

## 1. Executive Summary & Zero-Trust Safety Baseline

This research package provides verified contracts, automated production test suites, and prototype adapters for desktop conversation restore and resume submission on Antigravity Desktop on Windows.

### Critical Safety Guarantees Enforced:
- **`DO_NOT_SEND` over `POSSIBLY_SEND_TO_WRONG_THREAD`**: Ambiguous target selection, unverified conversation switching, missing chat containers, or unconfirmed route pathnames result in immediate fail-safe termination.
- **`DO_NOT_RESEND` over `POSSIBLY_DUPLICATE_RESUME`**: If state is unconfirmed, an agent turn is active, previous submission is unconfirmed, or the prompt signature is already present in conversation history, submission is aborted.
- **In-Lock Decision & Concurrency Exclusion**: The authoritative recovery decision, disk re-read, reconciliation, reservation, text insertion, durable `SUBMISSION_ATTEMPTED` barrier, and input dispatch are strictly executed **inside** an exclusive cross-process file lock.

---

## 2. Review Round 5 Defect Remediation (Items 1 – 16)

| Item | Problem Addressed | Production Change | Resulting Classification |
| :--- | :--- | :--- | :--- |
| **Item 1** | TOCTOU double-send race outside lock | Moved authoritative permission evaluation, disk re-read, reconciliation, and reservation strictly **inside** `exclusive_lock()`. Pre-lock checks are informative only. | `UNIT_TESTED_BRANCH` |
| **Item 2** | Concurrency test did not race `execute_resume_pipeline()` | Implemented `test_01_two_worker_concurrent_pipeline_race` executing two concurrent `execute_resume_pipeline()` invocations. Proved total dispatch count = 1. | `UNIT_TESTED_BRANCH` |
| **Item 3** | Unlocked journal reconciliation mutations | Moved `reconcile_existing_attempt()` inside the exclusive lock. Re-reads latest record immediately if mutated. | `UNIT_TESTED_BRANCH` |
| **Item 4** | Composer fell back to `document.body` | Removed `document.body` fallback from `inspect_composer_state()`. Scoped exclusively to verified target container. | `UNIT_TESTED_BRANCH` |
| **Item 5** | Unscoped global composer selectors | Refactored `clear_composer()` and `insert_prompt_text()` to require exact target route and query composer strictly inside target container. | `UNIT_TESTED_BRANCH` |
| **Item 6** | Global send button dispatch | Scoped `dispatch_submission_input()` to target container. Pre-dispatch revalidation verifies route, target container, prompt hash, and lack of active turn immediately before click/Enter. | `UNIT_TESTED_BRANCH` |
| **Item 7** | Unscoped Enter key fallback | Refactored Enter fallback: explicitly focuses target composer, verifies `document.activeElement === editor`, and halts with `SEND_FOCUS_NOT_VERIFIED` if focus fails. | `UNIT_TESTED_BRANCH` |
| **Item 8** | Unsafe lock persistence on process crash | Implemented lock metadata (`owner_pid`, `created_at`, `conversation_uuid`). Stale lock from dead PID is safely reclaimed; live PID yields `CONCURRENT_LOCK_ACTIVE`. Re-evaluates disk state after reclaim. | `UNIT_TESTED_BRANCH` |
| **Item 9** | Crash while holding lock | Enforced state semantics: crash at `NOT_SENT` allows retry only if clean; crash at `SUBMISSION_ATTEMPTED` or `DISPATCHED_UNCONFIRMED` blocks blind resend upon restart. | `UNIT_TESTED_BRANCH` |
| **Item 10** | Stale pre-lock state retention | Worker B acquiring lock discards pre-lock authorization and fetches fresh journal, DOM, route, and composer state from disk/DOM. | `UNIT_TESTED_BRANCH` |
| **Item 11** | Lock duration bounded to critical region | Lock held strictly through final decision $\rightarrow$ reservation $\rightarrow$ insert $\rightarrow$ durable write $\rightarrow$ dispatch. Released before multi-second turn observation. | `UNIT_TESTED_BRANCH` |
| **Item 12** | Post-dispatch transition race | `transition_state()` acquires exclusive lock internally, re-reads current attempt from disk, verifies state transition graph, and applies atomic write. | `UNIT_TESTED_BRANCH` |
| **Item 13** | Wrong-pane and multiple composer protection | Rejects multiple editors (`COMPOSER_AMBIGUOUS`), missing containers (`TARGET_CHAT_CONTAINER_NOT_FOUND`), and route mutations. | `UNIT_TESTED_BRANCH` |
| **Item 14** | Concurrency test evidence from pipeline | Evidence confirmed via real `execute_resume_pipeline()` race: Worker A returned `TURN_STARTED`, Worker B returned `TURN_ALREADY_ACTIVE` / `PREVIOUS_SUBMISSION_UNCONFIRMED`, total dispatch count = 1. | `UNIT_TESTED_BRANCH` |
| **Item 15** | Wording downgrade for privacy audit | Downgraded to `NO MATCHES FOR AUDITED PATTERNS` across broad regex patterns (`Users\\`, `bearer`, `api_key`, `password`). | `VERIFIED_DOC` |
| **Item 16** | Live evidence boundary | Retained `REAL SEND = NOT_LIVE_TESTED`. Production lane closed using live read-only observations + pipeline test suite + concurrency simulations. | `VERIFIED_DOC` |

---

## 3. Final Lock Ordering Contract

```text
[Pipeline Execution]
  │
  ├─► Pre-lock inspection (Read-only UI/route inspection for telemetry)
  │
  ├─► Acquire journal.exclusive_lock(conversation_uuid)
  │     │
  │     ├─► 1. Re-read fresh journal state from disk & validate schema
  │     ├─► 2. Re-inspect target route (/c/<uuid>) & target container DOM
  │     ├─► 3. Re-inspect composer state (draft present check)
  │     ├─► 4. Reconcile existing attempt forward if live DOM confirms it
  │     ├─► 5. Re-read latest record if mutated by reconciliation
  │     ├─► 6. Authoritative evaluate_recovery_permission()
  │     │       └─► If NOT NEW_ATTEMPT_ALLOWED: release lock & DO NOT SEND
  │     ├─► 7. Reserve attempt in STATE_NOT_SENT
  │     ├─► 8. Insert prompt text into target composer
  │     ├─► 9. Verify inserted text SHA-256 matches intended prompt
  │     ├─► 10. Durably transition to STATE_SUBMISSION_ATTEMPTED (os.fsync)
  │     ├─► 11. Pre-dispatch revalidation (route, container, prompt hash, focus)
  │     └─► 12. Dispatch input (Send button click / Enter key)
  │
  ├─► Release journal.exclusive_lock()
  │
  └─► Outside lock: Observe post-dispatch turns (wait_for_user_and_assistant_turn)
        └─► On state transition: acquire mutation lock -> transition -> release
```

---

## 4. Two-Worker Pipeline Race Result

- **Test Name:** `test_01_two_worker_concurrent_pipeline_race`
- **Execution Command:** `python -m unittest scripts/t03/test_suite.py`
- **Worker A Final Status:** `TURN_STARTED` (dispatched input)
- **Worker B Final Status:** `TURN_ALREADY_ACTIVE` / `PREVIOUS_SUBMISSION_UNCONFIRMED` (in-lock re-read blocked submission)
- **Total Dispatch Count Across Both Workers:** **1**

---

## 5. Stale Lock Recovery Contract

1. **Lock Metadata Format:** `{"owner_pid": <int>, "created_at": <float>, "conversation_uuid": <str>}`.
2. **Conflict Resolution:** If lock file exists, reader parses `owner_pid` and tests process liveness via OS APIs (`OpenProcess` on Windows / `os.kill(pid, 0)` on Unix).
3. **Dead Process Reclaim:** If owner process is confirmed dead, the stale lock is unlinked and re-acquired.
4. **Post-Reclaim Fail-Safe:** Reclaiming a stale lock **never** implies send is safe. The worker immediately re-reads journal from disk and evaluates state under standard zero-trust rules.
5. **Live Process:** If owner PID is alive, worker waits up to `timeout` seconds or raises `TimeoutError` (`CONCURRENT_LOCK_ACTIVE`).

---

## 6. Crash Window Matrix

| Crash Window Point | Journal State on Disk | Live DOM State on Restart | Restarted Watchdog Action | Send Permitted? |
| :--- | :--- | :--- | :--- | :--- |
| Before `start_recovery_attempt` | `None` / Clean | Idle, Clean History | Evaluates `NEW_ATTEMPT_ALLOWED` | **YES** |
| Between `NOT_SENT` and text insertion | `NOT_SENT` | Idle, Clean History | Evaluates `NEW_ATTEMPT_ALLOWED` | **YES** |
| Between `SUBMISSION_ATTEMPTED` and dispatch | `SUBMISSION_ATTEMPTED` | Prompt NOT in history | Evaluates `PREVIOUS_SUBMISSION_UNCONFIRMED` | **NO (BLOCKED)** |
| Immediately after dispatch before release | `SUBMISSION_ATTEMPTED` | Prompt observed in DOM | Reconciles to `MESSAGE_OBSERVED` | **NO (BLOCKED)** |
| Between dispatch and `MESSAGE_OBSERVED` | `SUBMISSION_ATTEMPTED` | Prompt observed in DOM | Reconciles to `MESSAGE_OBSERVED` | **NO (BLOCKED)** |
| During turn execution | `TURN_STARTED` | Stop button active | Evaluates `TURN_ALREADY_ACTIVE` | **NO (BLOCKED)** |

---

## 7. Target Composer Scope & Pre-Dispatch Revalidation

- **Strict Container Resolution:** All queries must originate from `document.querySelector('main [data-testid="conversation-messages"]') || document.querySelector('main')`. Fallback to `document.body` is strictly prohibited.
- **Ambiguity Guard:** Multiple editors within target container return `COMPOSER_AMBIGUOUS` and fail closed.
- **Pre-Dispatch Revalidation Checklist:**
  1. Route equals `/c/<target_uuid>`.
  2. Verified target container exists in DOM.
  3. No active Stop button in target container.
  4. Exactly one Lexical editor present with matching prompt SHA-256.
  5. If using Enter fallback: `document.activeElement === editor`.

---

## 8. Automated Test Suite Results (20 Production Tests)

Command executed:
```powershell
python -m unittest scripts/t03/test_suite.py
```
Output: `Ran 20 tests in 0.137s - OK`

- `test_01_two_worker_concurrent_pipeline_race`: `UNIT_TESTED_BRANCH` — PASSED
- `test_02_in_lock_evaluation_discards_pre_lock_authorization`: `UNIT_TESTED_BRANCH` — PASSED
- `test_03_locked_forward_reconciliation`: `UNIT_TESTED_BRANCH` — PASSED
- `test_04_target_composer_scope_fails_closed`: `UNIT_TESTED_BRANCH` — PASSED
- `test_05_pre_dispatch_revalidation_failure`: `UNIT_TESTED_BRANCH` — PASSED
- `test_06_stale_lock_recovery_dead_pid`: `UNIT_TESTED_BRANCH` — PASSED
- `test_07_strict_journal_schema_validation`: `UNIT_TESTED_BRANCH` — PASSED
- `test_08_failed_unknown_stage_fails_closed`: `UNIT_TESTED_BRANCH` — PASSED
- `test_09_post_baseline_error_tracking`: `UNIT_TESTED_BRANCH` — PASSED
- `test_10_semantic_author_identification`: `UNIT_TESTED_BRANCH` — PASSED
- `test_11_fsync_failure_raises_durability_error`: `UNIT_TESTED_BRANCH` — PASSED
- `test_12_dry_run_navigation_restoration`: `UNIT_TESTED_BRANCH` — PASSED
- `test_13_pipeline_duplicate_blocks_send`: `UNIT_TESTED_BRANCH` — PASSED
- `test_14_pipeline_draft_blocks_send`: `UNIT_TESTED_BRANCH` — PASSED
- `test_15_pipeline_navigation_failure_blocks`: `UNIT_TESTED_BRANCH` — PASSED
- `test_16_pipeline_post_dispatch_timeout`: `UNIT_TESTED_BRANCH` — PASSED
- `test_17_pipeline_quota_error_after_dispatch`: `UNIT_TESTED_BRANCH` — PASSED
- `test_18_external_error_hook_integration`: `UNIT_TESTED_BRANCH` — PASSED
- `test_19_pipeline_durability_failure_blocks_send`: `UNIT_TESTED_BRANCH` — PASSED
- `test_20_illegal_state_transitions_rejected`: `UNIT_TESTED_BRANCH` — PASSED

---

## 9. Privacy Audit & Evidence Boundary

- **Audited Patterns:** `Users\\`, `bearer`, `api_key`, `password`, un-sanitized UUIDs.
- **Audit Result:** **NO MATCHES FOR AUDITED PATTERNS**.
- **Live Evidence Boundary:**
  - `REAL SEND = NOT_LIVE_TESTED` (non-destructive safety policy).
  - All claims backed by real component execution, unit test assertions, and concurrency simulation.
  - **No Claim of Mathematically Guaranteed Exactly-Once**: Hardened against duplicate sends, crash recovery fails closed, concurrent watchdog send exclusion tested.

---

## 10. Remaining Unknown

1. Electron renderer crash behavior during WebSocket frame transmission.
2. Latency variations in DOM node rendering under severe host paging pressure.
