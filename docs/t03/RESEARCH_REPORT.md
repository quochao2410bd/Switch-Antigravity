# Research Package Report: Desktop Conversation Restore & Automatic Resume Submission

**Agent ID:** T03  
**Assigned Issue:** #3 — Desktop conversation restore and automatic resume submission  
**Assigned Branch:** `research/T03-conversation-resume`  
**Base SHA:** `3377bf7a0523795a678ac5da951371e3f5ee94c7`  
**Review Iteration:** R5 (Zero-Trust Review Round 4 Compliance)

---

## 1. Executive Summary & Zero-Trust Review Compliance

This research package provides verified contracts, an automated production-function test suite, and prototype adapters for desktop conversation restoration and resume submission on Antigravity Desktop on Windows.

### Critical Safety Principles Enforced:
- **`DO_NOT_SEND` over `POSSIBLY_SEND_TO_WRONG_THREAD`**: Ambiguous target selection or unverified conversation switching results in immediate fail-safe termination.
- **`DO_NOT_RESEND` over `POSSIBLY_DUPLICATE_RESUME`**: If state is unknown, an agent turn is active, previous submission is unconfirmed, or the prompt signature is already present in conversation history, submission is aborted.

---

## 2. Review Round 4 Defect Remediation (Items 1 – 16)

| Item | Problem Addressed | Production Change | Resulting Classification |
| :--- | :--- | :--- | :--- |
| **Item 1** | Journal schema lacked semantic validation | Implemented `validate_journal_schema()` checking version (2), UUID formats, valid state enums, timestamps, and history records. Unsupported version returns `SCHEMA_UNSUPPORTED`; invalid schema returns `SCHEMA_INVALID` and fails closed. | `UNIT_TESTED_BRANCH` |
| **Item 2** | `FAILED` unknown stage fell through to retry | Enforced in `evaluate_recovery_permission()` that only confirmed `FAILED + PRE_IRREVERSIBLE` permits retry. Missing/unknown/invalid failure stages return `MANUAL_RECONCILIATION_REQUIRED` (fail closed). | `UNIT_TESTED_BRANCH` |
| **Item 3** | Unprotected concurrent double send | Implemented cross-process advisory file lock (`exclusive_lock()`) spanning read state $\rightarrow$ evaluate permission $\rightarrow$ reserve attempt $\rightarrow$ text insert $\rightarrow$ durable `SUBMISSION_ATTEMPTED` $\rightarrow$ input dispatch. Verified second worker times out. | `UNIT_TESTED_BRANCH` |
| **Item 4** | Substring `includes()` route matching | Replaced with exact URL path parsing (`URL.pathname`) matching `/c/<uuid>`. Automatic navigation requires exact extracted UUID equality. | `UNIT_TESTED_BRANCH` |
| **Item 5** | Target chat container fallback to `document.body` | Scoped queries strictly to `main [data-testid="conversation-messages"]` or `main`. Missing container returns `TARGET_CHAT_CONTAINER_NOT_FOUND` (no `document.body` fallback). | `UNIT_TESTED_BRANCH` |
| **Item 6** | Historical quota errors poisoned new turns | Captured baseline article counts before dispatch. `correlate_turn_status()` scans only new post-baseline nodes for quota/generic errors. | `UNIT_TESTED_BRANCH` |
| **Item 7** | Text-based author heuristics (`User:`) | Removed all text prefix heuristics. Semantic role attributes (`data-author`, `.user-message`, `[data-testid="user-turn"]`) drive author classification. Unknown roles fail closed only when affecting latest message. | `UNIT_TESTED_BRANCH` |
| **Item 8** | First-attempt boolean was hardcoded false | Derived first-attempt status dynamically from journal records: `is_first_attempt = (latest_rec is None and j_status == "NOT_FOUND")`. | `UNIT_TESTED_BRANCH` |
| **Item 9** | Pipeline orchestration function untested | Refactored `execute_resume_pipeline()` to support dependency injection. Implemented 8 end-to-end pipeline integration tests covering all error, timeout, quota, and success paths. | `UNIT_TESTED_BRANCH` |
| **Item 10** | Unrealistic single-connection test | Created realistic mock WebSocket backend verifying single-connection lifecycle, context reuse, and interleaved event frame handling. | `UNIT_TESTED_BRANCH` |
| **Item 11** | Fsync failure silently ignored | Fsync failures in `_write_atomic()` raise `JournalDurabilityError`, aborting execution before send with `JOURNAL_DURABILITY_FAILED` (`DO_NOT_SEND`). | `UNIT_TESTED_BRANCH` |
| **Item 12** | Unverified dry-run navigation restoration | Verified return status of `switch_conversation_verified(original_uuid)`. Failure reports `DRY_RUN_RESTORE_FAILED`. Handled `original_uuid is None`. | `UNIT_TESTED_BRANCH` |
| **Item 13** | Unwired external error correlation | Integrated `external_error_hook` into `wait_for_user_and_assistant_turn()` and `execute_resume_pipeline()`, allowing supervisor/T01 quota log evidence correlation. | `UNIT_TESTED_BRANCH` |
| **Item 14** | Unconfirmed attempts not reconciled forward | Implemented `reconcile_existing_attempt()`: advances `SUBMISSION_ATTEMPTED` $\rightarrow$ `MESSAGE_OBSERVED` if prompt confirmed in live DOM; advances to `TURN_STARTED` if active turn detected. Strictly one-way forward progression. | `UNIT_TESTED_BRANCH` |
| **Item 15** | Dangerous manual override exposed in supervisor API | Removed `--dangerous-manual-override` parameter from `execute_resume_pipeline()`. The production adapter is structurally incapable of bypassing safety rules. | `UNIT_TESTED_BRANCH` |
| **Item 16** | Narrow privacy grep | Expanded privacy audit across broad generic UUIDs, path prefixes, emails, and credentials. Full tree confirmed sanitized. | `VERIFIED_DOC` |

---

## 3. Recovery Decision Table

| Previous Journal State | Failure Stage | Live DOM State | Evaluated Decision Code | Action / Permission |
| :--- | :--- | :--- | :--- | :--- |
| `None` (First Attempt) | N/A | Idle, Clean History | `NEW_ATTEMPT_ALLOWED` | Proceed with submission |
| `None` | N/A | Scoped Stop Button Present | `TURN_ALREADY_ACTIVE` | **DO NOT SEND** |
| `None` | N/A | Unsubmitted Draft Present | `BLOCKED_DRAFT_PRESENT` | **DO NOT SEND** |
| `None` | N/A | History contains prompt | `RESUME_ALREADY_OBSERVED` | **DO NOT SEND** |
| `NOT_SENT` | None | Idle, Clean History | `NEW_ATTEMPT_ALLOWED` | Proceed with submission |
| `SUBMISSION_ATTEMPTED` | None | Prompt confirmed in DOM | `RESUME_ALREADY_OBSERVED` | **DO NOT RESEND** (reconciled forward) |
| `SUBMISSION_ATTEMPTED` | None | Unconfirmed / Unknown DOM | `PREVIOUS_SUBMISSION_UNCONFIRMED` | **DO NOT RESEND (BLOCKED)** |
| `DISPATCHED_UNCONFIRMED`| `POST_IRREVERSIBLE_UNKNOWN` | Unconfirmed / Unknown DOM | `PREVIOUS_SUBMISSION_UNCONFIRMED` | **DO NOT RESEND (BLOCKED)** |
| `MESSAGE_OBSERVED` | None | Any | `RESUME_ALREADY_OBSERVED` | **DO NOT RESEND** |
| `TURN_STARTED` | None | Any | `TURN_ALREADY_ACTIVE` | **DO NOT RESEND** |
| `TURN_ACTIVE` | None | Any | `TURN_ALREADY_ACTIVE` | **DO NOT RESEND** |
| `FAILED` | `PRE_IRREVERSIBLE` | Idle, Clean History | `NEW_ATTEMPT_ALLOWED` | Safe to retry |
| `FAILED` | Missing / `UNKNOWN` / `POST_IRREVERSIBLE_UNKNOWN` | Any | `MANUAL_RECONCILIATION_REQUIRED` | **DO NOT RESEND (BLOCKED)** |
| Any / `CORRUPTED` | N/A | Corrupted JSON / Invalid Schema | `JOURNAL_CORRUPTED` | **FAIL CLOSED (BLOCKED)** |

---

## 4. Recovery Reconciliation Table

| Initial Journal State | Observed Live DOM Evidence | Reconciled Journal State | Mutation Direction |
| :--- | :--- | :--- | :--- |
| `SUBMISSION_ATTEMPTED` | Last user message matches prompt SHA-256 | `MESSAGE_OBSERVED` | Forward |
| `DISPATCHED_UNCONFIRMED` | Last user message matches prompt SHA-256 | `MESSAGE_OBSERVED` | Forward |
| `MESSAGE_OBSERVED` | Scoped Stop button active in main pane | `TURN_STARTED` | Forward |
| `TURN_STARTED` | Main pane assistant generation active | `TURN_ACTIVE` | Forward |
| Any State | Message missing / unconfirmed | Unchanged | No mutation |

---

## 5. Journal Schema Contract

```json
{
  "version": 2,
  "records": {
    "00000000-0000-4000-8000-000000000001": [
      {
        "attempt_id": "11111111-1111-4111-8111-111111111111",
        "conversation_uuid": "00000000-0000-4000-8000-000000000001",
        "state": "TURN_STARTED",
        "prompt_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "created_at_utc": 1772000000.0,
        "updated_at_utc": 1772000005.0,
        "failure_stage": null,
        "history": [
          { "state": "NOT_SENT", "timestamp": 1772000000.0 },
          { "state": "SUBMISSION_ATTEMPTED", "timestamp": 1772000001.0 },
          { "state": "MESSAGE_OBSERVED", "timestamp": 1772000003.0 },
          { "state": "TURN_STARTED", "timestamp": 1772000005.0 }
        ]
      }
    ]
  }
}
```

---

## 6. Concurrent-Send Exclusion Contract

The critical region is protected by `RecoveryJournal.exclusive_lock()`:
1. Acquire exclusive advisory lock (`t03_recovery_journal.json.lock`).
2. Read and validate schema.
3. Evaluate recovery permission (`evaluate_recovery_permission`).
4. Initialize attempt record (`STATE_NOT_SENT`).
5. Insert and verify composer text.
6. Durably transition journal to `STATE_SUBMISSION_ATTEMPTED` (with `os.fsync`).
7. Dispatch input (Enter key / Send button).
8. Release exclusive lock.

Any concurrent process attempting to acquire the lock during this critical region times out and halts immediately without double-sending.

---

## 7. Automated Test Suite Results (20 Production Tests)

Command executed:
```powershell
python scripts/t03/test_suite.py
```
Results: `Ran 20 tests in 0.199s - OK`

- `test_01_strict_journal_schema_validation`: `UNIT_TESTED_BRANCH` — PASSED
- `test_02_unsupported_schema_version`: `UNIT_TESTED_BRANCH` — PASSED
- `test_03_failed_unknown_stage_fails_closed`: `UNIT_TESTED_BRANCH` — PASSED
- `test_04_failed_pre_irreversible_permits_retry`: `UNIT_TESTED_BRANCH` — PASSED
- `test_05_concurrent_send_exclusion`: `UNIT_TESTED_BRANCH` — PASSED
- `test_06_exact_route_matching`: `UNIT_TESTED_BRANCH` — PASSED
- `test_07_target_chat_scope_fails_closed`: `UNIT_TESTED_BRANCH` — PASSED
- `test_08_post_baseline_error_tracking`: `UNIT_TESTED_BRANCH` — PASSED
- `test_09_semantic_author_identification`: `UNIT_TESTED_BRANCH` — PASSED
- `test_10_fsync_failure_raises_durability_error`: `UNIT_TESTED_BRANCH` — PASSED
- `test_11_dry_run_navigation_restoration`: `UNIT_TESTED_BRANCH` — PASSED
- `test_12_forward_attempt_reconciliation`: `UNIT_TESTED_BRANCH` — PASSED
- `test_13_pipeline_clean_send_path`: `UNIT_TESTED_BRANCH` — PASSED
- `test_14_pipeline_duplicate_blocks_send`: `UNIT_TESTED_BRANCH` — PASSED
- `test_15_pipeline_draft_blocks_send`: `UNIT_TESTED_BRANCH` — PASSED
- `test_16_pipeline_navigation_failure_blocks`: `UNIT_TESTED_BRANCH` — PASSED
- `test_17_pipeline_post_dispatch_timeout`: `UNIT_TESTED_BRANCH` — PASSED
- `test_18_pipeline_quota_error_after_dispatch`: `UNIT_TESTED_BRANCH` — PASSED
- `test_19_external_error_hook_integration`: `UNIT_TESTED_BRANCH` — PASSED
- `test_20_pipeline_durability_failure_blocks_send`: `UNIT_TESTED_BRANCH` — PASSED

---

## 8. Repro Checklist for Main Orchestrator

1. **Run Full Production Test Suite**:
   ```powershell
   python scripts/t03/test_suite.py
   ```
2. **Execute Read-Only Navigation-Restoring Dry-Run**:
   ```powershell
   python scripts/t03/send_resume.py --uuid 00000000-0000-4000-8000-000000000001 --json
   ```
3. **Execute Privacy-Hardened Desktop Inspection**:
   ```powershell
   python scripts/t03/inspect_desktop.py
   ```
4. **Execute Windows Session Context & UIA Inspection**:
   ```powershell
   python scripts/t03/inspect_uia.py
   ```
