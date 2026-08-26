# Research Package Report: Desktop Conversation Restore & Automatic Resume Submission

**Agent ID:** T03  
**Assigned Issue:** #3 — Desktop conversation restore and automatic resume submission  
**Assigned Branch:** `research/T03-conversation-resume`  
**Base SHA:** `3377bf7a0523795a678ac5da951371e3f5ee94c7`  
**Review Iteration:** R4 (Zero-Trust Review Round 3 Compliance)

---

## 1. Executive Summary & Zero-Trust Review Compliance

This research package provides verified contracts, an automated production-function test suite, and prototype adapters for desktop conversation restoration and resume submission on Antigravity Desktop on Windows.

### Critical Safety Principles Enforced:
- **`DO_NOT_SEND` over `POSSIBLY_SEND_TO_WRONG_THREAD`**: Ambiguous target selection or unverified conversation switching results in immediate fail-safe termination.
- **`DO_NOT_RESEND` over `POSSIBLY_DUPLICATE_RESUME`**: If state is unknown, an agent turn is active, previous submission is unconfirmed, or the prompt signature is already present in conversation history, submission is aborted.

---

## 2. Review Round 3 Corrections (Items 1 – 18)

| Item | Problem Addressed | Implementation & Evidence Change | Resulting Classification |
| :--- | :--- | :--- | :--- |
| **Item 1** | Journal did not control recovery | Implemented `evaluate_recovery_permission()`. Loads latest recovery record before attempt creation. Evaluates DOM state, prompt hash, and journal state to permit or block recovery. | `UNIT_TEST` / `SYNTHETIC_SIMULATION` |
| **Item 2** | Unknown duplicate state proceeded | `DUPLICATE_STATE_UNKNOWN` now strictly fails closed (`DO_NOT_SEND` / `RECOVERY_STATE_UNKNOWN`) unless proven first attempt on empty conversation. | `UNIT_TEST` |
| **Item 3** | Journal corruption reset safety state | Corrupted journal returns `JOURNAL_CORRUPTED`, refuses new attempts (`RuntimeError`), and evaluates to terminal `MANUAL_RECONCILIATION_REQUIRED`. | `UNIT_TEST` |
| **Item 4** | Unclear durability guarantee | Implemented `_write_atomic()` with `flush()`, `os.fsync()`, and `os.replace()`. Documented Windows NTFS transaction journaling guarantee. | `UNIT_TEST` |
| **Item 5** | Author role heuristic clashed with T0 reports | Removed `startsWith("T0")`. Evaluates semantic DOM markers (`data-author="user"`, `.user-message`). Unresolvable author triggers `DUPLICATE_STATE_UNKNOWN` and fails closed. | `UNIT_TEST` |
| **Item 6** | Message history queried globally | Scoped all article and button queries strictly to `main [data-testid="conversation-messages"]` or `main`. | `VERIFIED_LIVE_RUNTIME` / `UNIT_TEST` |
| **Item 7** | Turn delta lacked prompt identity | `USER_MESSAGE_OBSERVED` requires exact normalized SHA-256 prompt hash match in user article history in addition to message delta. | `UNIT_TEST` / `SYNTHETIC_SIMULATION` |
| **Item 8** | Merged error/quota articles into turn start | Implemented `correlate_turn_status()`. Distinguishes `ASSISTANT_GENERATION_ACTIVE`, `ASSISTANT_GENERATION_COMPLETED`, `QUOTA_ERROR_OBSERVED`, and `ERROR_RESPONSE_OBSERVED`. | `UNIT_TEST` |
| **Item 9** | Redundant CDP connection | Refactored `QualifiedAntigravityClient` to connect once and reuse the open WebSocket across pipeline operations. | `UNIT_TEST` / `VERIFIED_LIVE_RUNTIME` |
| **Item 10** | Unfiltered CDP WebSocket frames | Added probe response ID matching (`msg.id == 1001`) in qualification loop, ignoring background CDP event frames. | `UNIT_TEST` / `SYNTHETIC_SIMULATION` |
| **Item 11** | Regex substring routing | Canonical route is strictly `/c/<uuid>`. Enforced exact pathname equality and parameterized JavaScript arguments. | `VERIFIED_LIVE_RUNTIME` / `UNIT_TEST` |
| **Item 12** | Tautological unit tests | Refactored `test_suite.py` to invoke actual production functions (`evaluate_recovery_permission`, `RecoveryJournal`, `classify_duplicate_state`, `correlate_turn_status`). | `UNIT_TEST` |
| **Item 13** | Repeated invocation test | Replaced static test with real multi-step restart test: `SUBMISSION_ATTEMPTED` $\rightarrow$ unconfirmed DOM $\rightarrow$ `PREVIOUS_SUBMISSION_UNCONFIRMED` (NO BLIND RESEND). | `UNIT_TEST` |
| **Item 14** | Live IDs in committed tree | Replaced all environment-derived UUIDs with deterministic synthetic UUIDs (`00000000-0000-4000-8000-000000000001`). | `VERIFIED_DOC` |
| **Item 15** | Dry-run active conversation mutation | Implemented navigation-restoring dry-run mode: records initial pathname, inspects target, and navigates back to restore original state. | `VERIFIED_LIVE_RUNTIME` |
| **Item 16** | Unenforced journal transitions | Implemented `ALLOWED_TRANSITIONS` graph. Illegal transitions (e.g. `TURN_STARTED` $\rightarrow$ `NOT_SENT`) raise `ValueError`. | `UNIT_TEST` |
| **Item 17** | Generic FAILED for post-dispatch timeout | Introduced `DISPATCHED_UNCONFIRMED` and `failure_stage="POST_IRREVERSIBLE_UNKNOWN"` to strictly prevent blind retry after input dispatch. | `UNIT_TEST` |
| **Item 18** | Evidence classification precision | Corrected test counts and clearly separated `LIVE_OBSERVED_COMPONENT`, `UNIT_TESTED_BRANCH`, and `NOT_LIVE_TESTED`. | `VERIFIED_DOC` |

---

## 3. Recovery Decision Table

| Previous Journal State | Live DOM State | Expected Prompt Present | Evaluated Decision Code | Action / Permission |
| :--- | :--- | :--- | :--- | :--- |
| `None` (First Attempt) | Idle, Clean History | No | `NEW_ATTEMPT_ALLOWED` | Proceed with submission |
| `None` | Scoped Stop Button Present | Any | `TURN_ALREADY_ACTIVE` | **DO NOT SEND** |
| `None` | Unsubmitted Draft Present | Any | `BLOCKED_DRAFT_PRESENT` | **DO NOT SEND** |
| `None` | History contains prompt | Yes | `RESUME_ALREADY_OBSERVED` | **DO NOT SEND** |
| `NOT_SENT` | Idle, Clean History | No | `NEW_ATTEMPT_ALLOWED` | Proceed with submission |
| `SUBMISSION_ATTEMPTED` | Prompt confirmed in DOM | Yes | `RESUME_ALREADY_OBSERVED` | **DO NOT RESEND** |
| `SUBMISSION_ATTEMPTED` | Unconfirmed / Unknown DOM | Unknown / No | `PREVIOUS_SUBMISSION_UNCONFIRMED` | **DO NOT RESEND (BLOCKED)** |
| `DISPATCHED_UNCONFIRMED`| Unconfirmed / Unknown DOM | Unknown / No | `PREVIOUS_SUBMISSION_UNCONFIRMED` | **DO NOT RESEND (BLOCKED)** |
| `MESSAGE_OBSERVED` | Any | Any | `RESUME_ALREADY_OBSERVED` | **DO NOT RESEND** |
| `TURN_STARTED` | Any | Any | `TURN_ALREADY_ACTIVE` | **DO NOT RESEND** |
| `TURN_ACTIVE` | Any | Any | `TURN_ALREADY_ACTIVE` | **DO NOT RESEND** |
| `FAILED` (`PRE_IRREVERSIBLE`)| Idle, Clean History | No | `NEW_ATTEMPT_ALLOWED` | Safe to retry |
| `FAILED` (`POST_IRREVERSIBLE_UNKNOWN`)| Any | Any | `MANUAL_RECONCILIATION_REQUIRED` | **DO NOT RESEND** |
| Any / `CORRUPTED` | Corrupted JSON file | Any | `JOURNAL_CORRUPTED` | **FAIL CLOSED (BLOCKED)** |

---

## 4. Journal State Transition Graph

```text
       ┌──────────────┐
       │   NOT_SENT   │
       └──────┬───────┘
              │ (write before input dispatch)
              ▼
┌───────────────────────────┐
│   SUBMISSION_ATTEMPTED    │
└──────┬────────────────────┘
       │
       ├─────────────────────────────────────────┐ (DOM timeout / unconfirmed)
       │ (user message observed)                 ▼
       ▼                             ┌───────────────────────────────┐
┌───────────────────────────┐        │    DISPATCHED_UNCONFIRMED     │
│     MESSAGE_OBSERVED      │        └──────────────┬────────────────┘
└──────┬────────────────────┘                       │ (later observed in DOM)
       │ (assistant turn active)                    │
       ▼                                            │
┌───────────────────────────┐                       │
│       TURN_STARTED        │◄──────────────────────┘
└──────┬────────────────────┘
       │
       ▼
┌───────────────────────────┐
│        TURN_ACTIVE        │
└───────────────────────────┘

[Any state can transition to FAILED on definitive pre-dispatch or post-dispatch error. FAILED is terminal.]
```

---

## 5. Pre/Post Irreversible Failure Matrix

| Failure Point | Stage Classification | Journal State | Retry Policy |
| :--- | :--- | :--- | :--- |
| **Port File Missing / Invalid** | `PRE_IRREVERSIBLE` | None / `FAILED` | Retryable after supervisor fixes port file |
| **Candidate Qualification Failed** | `PRE_IRREVERSIBLE` | None / `FAILED` | Retryable after supervisor opens valid window |
| **Target Route Switch Timeout** | `PRE_IRREVERSIBLE` | None / `FAILED` | Retryable; composer was never touched |
| **Composer Text Insertion Mismatch** | `PRE_IRREVERSIBLE` | `FAILED` | Retryable; input was never dispatched |
| **Send Button Click Exception** | `PRE_IRREVERSIBLE` | `FAILED` | Retryable; dispatch was aborted |
| **Post-Dispatch DOM Observation Timeout**| `POST_IRREVERSIBLE_UNKNOWN`| `DISPATCHED_UNCONFIRMED`| **BLOCKED**; input already dispatched; resend forbidden |
| **Post-Dispatch Quota 429 Received** | `POST_IRREVERSIBLE_UNKNOWN`| `FAILED` | **BLOCKED**; requires account switch before resend |

---

## 6. Real Production-Function Test Results (20 Scenarios)

All 20 tests call actual production functions:
- `test_01_journal_lifecycle`: `UNIT_TEST` — PASSED
- `test_02_illegal_state_transitions_rejected`: `UNIT_TEST` — PASSED
- `test_03_corrupted_journal_fails_closed`: `UNIT_TEST` — PASSED
- `test_04_durability_barrier`: `UNIT_TEST` — PASSED
- `test_05_uuid_validation`: `UNIT_TEST` — PASSED
- `test_06_author_classification_no_t0_clash`: `UNIT_TEST` — PASSED
- `test_07_duplicate_prompt_detection`: `UNIT_TEST` — PASSED
- `test_08_repeated_invocation_after_submission_attempted`: `UNIT_TEST` — PASSED
- `test_09_repeated_invocation_after_message_observed`: `UNIT_TEST` — PASSED
- `test_10_repeated_invocation_turn_active`: `UNIT_TEST` — PASSED
- `test_11_unknown_duplicate_state_fails_closed`: `UNIT_TEST` — PASSED
- `test_12_draft_present_blocks_send`: `UNIT_TEST` — PASSED
- `test_13_correlate_turn_status`: `UNIT_TEST` — PASSED
- `test_14_post_dispatch_unconfirmed_transition`: `UNIT_TEST` — PASSED
- `test_15_pre_irreversible_failure_permits_clean_restart`: `UNIT_TEST` — PASSED
- `test_16_cdp_discovery_return_codes`: `UNIT_TEST` — PASSED
- `test_17_single_connection_lifecycle`: `UNIT_TEST` — PASSED
- `test_18_exact_title_collision`: `SYNTHETIC_SIMULATION` — PASSED
- `test_19_scoped_stop_button_isolation`: `SYNTHETIC_SIMULATION` — PASSED
- `test_20_first_attempt_on_clean_idle_conversation`: `UNIT_TEST` — PASSED

---

## 7. Repro Checklist for Main Orchestrator

1. **Verify Automated Production-Function Test Suite (All 20 tests pass)**:
   ```powershell
   python scripts/t03/test_suite.py
   ```
2. **Verify Read-Only Navigation-Restoring Dry-Run**:
   ```powershell
   python scripts/t03/send_resume.py --uuid 00000000-0000-4000-8000-000000000001 --json
   ```
3. **Verify Stale Port Rejection**:
   ```powershell
   python scripts/t03/send_resume.py --cdp-endpoint http://127.0.0.1:59999 --json
   ```
4. **Verify Privacy-Hardened Desktop Inspection**:
   ```powershell
   python scripts/t03/inspect_desktop.py
   ```
5. **Verify Windows Session Context & UIA Limitation**:
   ```powershell
   python scripts/t03/inspect_uia.py
   ```
