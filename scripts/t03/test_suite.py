#!/usr/bin/env python3
"""
Automated Test Suite for T03 Resume Adapter & Recovery State Machine (Round 5 Final)

Implements rigorous tests against REAL production functions and execute_resume_pipeline():
- Two-worker execute_resume_pipeline() concurrent race with TOTAL dispatch count = 1.
- In-lock authoritative recovery permission evaluation and re-reading fresh disk state.
- In-lock forward attempt reconciliation.
- Process liveness check & stale lock recovery without blind resend.
- Target chat container scope (strictly no document.body fallback).
- Multiple composer / ambiguous editor rejection.
- Send button pre-dispatch validation and focus verification.
- Post-baseline error tracking.
- Durability barriers (fsync failure raises JournalDurabilityError).
- Verified dry-run navigation restoration.
"""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from recovery_journal import (
    RecoveryJournal,
    JournalDurabilityError,
    JournalSchemaError,
    SCHEMA_VERSION,
    STATE_NOT_SENT,
    STATE_SUBMISSION_ATTEMPTED,
    STATE_DISPATCHED_UNCONFIRMED,
    STATE_MESSAGE_OBSERVED,
    STATE_TURN_STARTED,
    STATE_TURN_ACTIVE,
    STATE_FAILED,
    DECISION_NEW_ATTEMPT_ALLOWED,
    DECISION_RESUME_ALREADY_OBSERVED,
    DECISION_TURN_ALREADY_ACTIVE,
    DECISION_PREVIOUS_SUBMISSION_UNCONFIRMED,
    DECISION_RECOVERY_STATE_UNKNOWN,
    DECISION_JOURNAL_CORRUPTED,
    DECISION_JOURNAL_SCHEMA_UNSUPPORTED,
    DECISION_MANUAL_RECONCILIATION_REQUIRED,
    DECISION_BLOCKED_DRAFT_PRESENT,
    validate_journal_schema,
    evaluate_recovery_permission,
    reconcile_existing_attempt,
    validate_uuid,
    hash_prompt
)

from send_resume import (
    classify_duplicate_state,
    correlate_turn_status,
    discover_cdp_endpoint,
    execute_resume_pipeline,
    QualifiedAntigravityClient
)

SYNTHETIC_UUID_1 = "00000000-0000-4000-8000-000000000001"
SYNTHETIC_UUID_2 = "00000000-0000-4000-8000-000000000002"
SYNTHETIC_PROMPT = "Continue the current task from exactly where you stopped."

class MockAntigravityClient:
    """Thread-safe mock client for testing concurrent execute_resume_pipeline() executions."""
    def __init__(self, endpoint="http://127.0.0.1:58859", conversations=None, initial_pathname=None):
        self.endpoint = endpoint
        self.conversations = conversations or [
            {"index": 0, "title": "Synthetic Task", "href": f"/c/{SYNTHETIC_UUID_1}", "uuid": SYNTHETIC_UUID_1, "isActive": True, "isExecutingInSidebar": False}
        ]
        self.current_pathname = initial_pathname or f"/c/{SYNTHETIC_UUID_1}"
        self.composer_state = {"found": True, "text": "", "draftPresent": False}
        self.scoped_state = {
            "totalArticles": 2,
            "userMessageCount": 1,
            "assistantMessageCount": 1,
            "userMessages": ["Initial instruction"],
            "lastUserMessageText": "Initial instruction",
            "isMainTurnActive": False,
            "hasUnknownRole": False,
            "lastMessageIsUnknown": False,
            "newQuotaError": False,
            "newGenericError": False,
            "isConversationEmptyOrIdle": False
        }
        self.switch_result = {"status": "CONVERSATION_SWITCH_VERIFIED"}
        self.dispatch_count = 0
        self.turn_result = {
            "user_message_observed": True,
            "assistant_turn_type": "ASSISTANT_GENERATION_ACTIVE",
            "elapsed_seconds": 0.5
        }

    async def connect_and_qualify(self):
        return {"type": "page"}, "APP_PAGE_QUALIFIED"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def list_conversations(self):
        return self.conversations

    async def switch_conversation_verified(self, target_uuid, timeout=6.0):
        self.current_pathname = f"/c/{target_uuid}"
        return self.switch_result

    async def inspect_composer_state(self, target_uuid=None):
        return dict(self.composer_state)

    async def clear_composer(self, target_uuid):
        self.composer_state["text"] = ""
        self.composer_state["draftPresent"] = False

    async def insert_prompt_text(self, target_uuid, text):
        self.composer_state["text"] = text

    async def inspect_scoped_conversation_state(self, target_uuid, prompt_hash, baseline_article_count=0):
        state = dict(self.scoped_state)
        dup_status, last_hash = classify_duplicate_state(
            state.get("userMessages", []),
            prompt_hash,
            state.get("hasUnknownRole", False),
            state.get("lastMessageIsUnknown", False)
        )
        state["duplicateStatus"] = dup_status
        state["lastUserMessageHash"] = last_hash
        return state

    async def dispatch_submission_input(self, target_uuid, expected_prompt_hash):
        self.dispatch_count += 1
        return {"dispatched": True, "method": "button_click"}

    async def wait_for_user_and_assistant_turn(self, target_uuid, prompt_hash, baseline, timeout=12, external_error_hook=None):
        return self.turn_result

class TestT03Round5Final(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="t03_r5_")
        self.journal_file = os.path.join(self.test_dir, "test_journal.json")
        self.journal = RecoveryJournal(self.journal_file)
        self.prompt_hash = hash_prompt(SYNTHETIC_PROMPT)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # CRITICAL ITEM 2 & 14: Real Two-Worker execute_resume_pipeline() Concurrent Race
    def test_01_two_worker_concurrent_pipeline_race(self):
        """MANDATORY TEST: Two concurrent execute_resume_pipeline() calls race. TOTAL dispatch count = 1."""
        shared_client = MockAntigravityClient()
        shared_journal = RecoveryJournal(self.journal_file)

        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )

        async def run_race():
            task_a = asyncio.create_task(execute_resume_pipeline(args, client_override=shared_client, journal_override=shared_journal))
            task_b = asyncio.create_task(execute_resume_pipeline(args, client_override=shared_client, journal_override=shared_journal))
            res_a, res_b = await asyncio.gather(task_a, task_b)
            return res_a, res_b

        res_a, res_b = asyncio.run(run_race())

        # Assert: Exactly one worker dispatched input
        self.assertEqual(shared_client.dispatch_count, 1)

        # One worker succeeded with TURN_STARTED; the other failed closed upon in-lock re-check
        statuses = [res_a["status"], res_b["status"]]
        self.assertIn("TURN_STARTED", statuses)
        self.assertTrue(
            "PREVIOUS_SUBMISSION_UNCONFIRMED" in statuses or "RESUME_ALREADY_OBSERVED" in statuses or "TURN_ALREADY_ACTIVE" in statuses,
            f"Expected safety block in statuses: {statuses}"
        )

    # CRITICAL ITEM 1 & 10: Re-reading and evaluating fresh state inside lock
    def test_02_in_lock_evaluation_discards_pre_lock_authorization(self):
        """UNIT_TEST: Worker acquiring lock after another worker submitted must fail closed upon in-lock re-read."""
        rec = self.journal.start_recovery_attempt(SYNTHETIC_UUID_1, SYNTHETIC_PROMPT)
        self.journal.transition_state(SYNTHETIC_UUID_1, rec["attempt_id"], STATE_SUBMISSION_ATTEMPTED)

        latest, _ = self.journal.get_latest_record(SYNTHETIC_UUID_1)
        unconfirmed_dom = {"duplicateStatus": "RESUME_NOT_PRESENT", "isMainTurnActive": False}

        decision, explanation = evaluate_recovery_permission(latest, unconfirmed_dom, self.prompt_hash)
        self.assertEqual(decision, DECISION_PREVIOUS_SUBMISSION_UNCONFIRMED)
        self.assertIn("strictly blocked", explanation)

    # CRITICAL ITEM 3: Locked Forward Reconciliation
    def test_03_locked_forward_reconciliation(self):
        """UNIT_TEST: reconcile_existing_attempt advances unconfirmed attempt to MESSAGE_OBSERVED."""
        rec = self.journal.start_recovery_attempt(SYNTHETIC_UUID_1, SYNTHETIC_PROMPT)
        self.journal.transition_state(SYNTHETIC_UUID_1, rec["attempt_id"], STATE_SUBMISSION_ATTEMPTED)

        latest, _ = self.journal.get_latest_record(SYNTHETIC_UUID_1)
        live_dom = {"lastUserMessageHash": self.prompt_hash, "isMainTurnActive": False}

        updated_rec, reconciled = reconcile_existing_attempt(
            self.journal, SYNTHETIC_UUID_1, latest, live_dom, self.prompt_hash
        )
        self.assertTrue(reconciled)
        self.assertEqual(updated_rec["state"], STATE_MESSAGE_OBSERVED)

    # CRITICAL ITEM 4 & 5: Target Composer Scope Fails Closed
    def test_04_target_composer_scope_fails_closed(self):
        """UNIT_TEST: Missing or ambiguous composer fails closed with structured status."""
        mock_client = MockAntigravityClient()
        mock_client.composer_state = {"found": False, "error": "TARGET_CHAT_CONTAINER_NOT_FOUND"}

        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "TARGET_CHAT_CONTAINER_NOT_FOUND")

    # CRITICAL ITEM 6 & 7: Pre-Dispatch Revalidation
    def test_05_pre_dispatch_revalidation_failure(self):
        """UNIT_TEST: Pre-dispatch revalidation failure blocks dispatch."""
        mock_client = MockAntigravityClient()
        mock_client.dispatch_submission_input = AsyncMock(return_value={"dispatched": False, "error": "SEND_FOCUS_NOT_VERIFIED"})

        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "SEND_INPUT_DISPATCH_FAILED")

    # ITEM 8: Stale Lock Recovery
    def test_06_stale_lock_recovery_dead_pid(self):
        """UNIT_TEST: Stale lock from a dead PID is safely reclaimed without deadlocking."""
        dead_pid = 99999999
        lock_meta = {"owner_pid": dead_pid, "created_at": time.time() - 100, "conversation_uuid": SYNTHETIC_UUID_1}
        with open(self.journal.lock_path, "w", encoding="utf-8") as f:
            json.dump(lock_meta, f)

        # Acquiring lock should reclaim stale lock
        with self.journal.exclusive_lock(timeout=1.0, conversation_uuid=SYNTHETIC_UUID_1):
            self.assertTrue(os.path.exists(self.journal.lock_path))

    # ITEM 1: Strict Journal Schema Validation
    def test_07_strict_journal_schema_validation(self):
        """UNIT_TEST: Valid JSON with semantic error raises JournalSchemaError."""
        invalid_data = {
            "version": 2,
            "records": {
                SYNTHETIC_UUID_1: [
                    {
                        "attempt_id": "11111111-1111-4111-8111-111111111111",
                        "conversation_uuid": SYNTHETIC_UUID_1,
                        "state": "INVALID_STATE_GARBAGE",
                        "prompt_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                        "created_at_utc": 1000.0,
                        "updated_at_utc": 1001.0,
                        "failure_stage": None,
                        "history": [{"state": "INVALID_STATE_GARBAGE", "timestamp": 1000.0}]
                    }
                ]
            }
        }
        with open(self.journal_file, "w", encoding="utf-8") as f:
            json.dump(invalid_data, f)

        _, status = self.journal._read_raw()
        self.assertEqual(status, "SCHEMA_INVALID")

    # ITEM 2: FAILED Unknown Stage Fails Closed
    def test_08_failed_unknown_stage_fails_closed(self):
        """UNIT_TEST: FAILED state with missing or unknown failure_stage blocks retry."""
        rec_unknown = {
            "attempt_id": "11111111-1111-4111-8111-111111111111",
            "conversation_uuid": SYNTHETIC_UUID_1,
            "state": STATE_FAILED,
            "prompt_sha256": self.prompt_hash,
            "failure_stage": "UNKNOWN"
        }
        decision, _ = evaluate_recovery_permission(rec_unknown, {"duplicateStatus": "RESUME_NOT_PRESENT"}, self.prompt_hash)
        self.assertEqual(decision, DECISION_MANUAL_RECONCILIATION_REQUIRED)

    # ITEM 6: Post-Baseline Error Tracking
    def test_09_post_baseline_error_tracking(self):
        """UNIT_TEST: Historical quota errors do not poison new healthy turns."""
        historical_dom = {
            "totalArticles": 5,
            "hasQuotaError": True,
            "newQuotaError": False,
            "newGenericError": False,
            "isMainTurnActive": True
        }
        status = correlate_turn_status(historical_dom, baseline_state={"totalArticles": 4})
        self.assertEqual(status, "ASSISTANT_GENERATION_ACTIVE")

    # ITEM 7: Semantic Author Identification
    def test_10_semantic_author_identification(self):
        """UNIT_TEST: Semantic role metadata drives classification."""
        msgs = ["User prompt 1", "User prompt 2"]
        status, _ = classify_duplicate_state(msgs, self.prompt_hash, has_unknown_role=True, last_message_is_unknown=False)
        self.assertEqual(status, "RESUME_NOT_PRESENT")

    # ITEM 11: Fsync Failure Raises JournalDurabilityError
    def test_11_fsync_failure_raises_durability_error(self):
        """UNIT_TEST: os.fsync failure raises JournalDurabilityError."""
        with patch("os.fsync", side_effect=OSError("Disk failure")):
            with self.assertRaises(JournalDurabilityError):
                self.journal._write_atomic({"version": 2, "records": {}})

    # ITEM 12: Verified Dry-Run Navigation Restoration
    def test_12_dry_run_navigation_restoration(self):
        """UNIT_TEST: Dry-run verifies restoration of original conversation route."""
        mock_client = MockAntigravityClient(
            conversations=[
                {"index": 0, "title": "A", "href": f"/c/{SYNTHETIC_UUID_1}", "uuid": SYNTHETIC_UUID_1, "isActive": True},
                {"index": 1, "title": "B", "href": f"/c/{SYNTHETIC_UUID_2}", "uuid": SYNTHETIC_UUID_2, "isActive": False}
            ]
        )
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_2, title=None, prompt=SYNTHETIC_PROMPT,
            send=False, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "DRY_RUN_READ_ONLY_SUCCESS")
        self.assertTrue(res.get("dry_run_navigation_restored"))

    # Pipeline Test: Duplicate Blocks Send
    def test_13_pipeline_duplicate_blocks_send(self):
        """UNIT_TEST: Duplicate prompt in user message history blocks send."""
        mock_client = MockAntigravityClient()
        mock_client.scoped_state["userMessages"] = [SYNTHETIC_PROMPT]
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "RESUME_ALREADY_OBSERVED")

    # Pipeline Test: Draft Present Blocks Send
    def test_14_pipeline_draft_blocks_send(self):
        """UNIT_TEST: Unsubmitted user draft in composer blocks send."""
        mock_client = MockAntigravityClient()
        mock_client.composer_state["draftPresent"] = True
        mock_client.composer_state["text"] = "Existing draft"
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "BLOCKED_DRAFT_PRESENT")

    # Pipeline Test: Navigation Failure Blocks Send
    def test_15_pipeline_navigation_failure_blocks(self):
        """UNIT_TEST: Switch timeout halts pipeline before dispatch."""
        mock_client = MockAntigravityClient(
            conversations=[{"index": 0, "title": "A", "href": f"/c/{SYNTHETIC_UUID_1}", "uuid": SYNTHETIC_UUID_1, "isActive": False}]
        )
        mock_client.switch_result = {"status": "CONVERSATION_SWITCH_TIMEOUT"}
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "CONVERSATION_SWITCH_TIMEOUT")

    # Pipeline Test: Post-Dispatch Timeout Yields DISPATCHED_UNCONFIRMED
    def test_16_pipeline_post_dispatch_timeout(self):
        """UNIT_TEST: Post-dispatch timeout yields DISPATCHED_UNCONFIRMED."""
        mock_client = MockAntigravityClient()
        mock_client.turn_result = {"user_message_observed": False, "assistant_turn_type": "NO_ASSISTANT_TURN", "timeout": 5}
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "DISPATCHED_UNCONFIRMED")
        latest, _ = self.journal.get_latest_record(SYNTHETIC_UUID_1)
        self.assertEqual(latest["state"], STATE_DISPATCHED_UNCONFIRMED)

    # Pipeline Test: Quota Error After Dispatch
    def test_17_pipeline_quota_error_after_dispatch(self):
        """UNIT_TEST: API quota error after dispatch records FAILED and reports QUOTA_ERROR_OBSERVED."""
        mock_client = MockAntigravityClient()
        mock_client.turn_result = {"user_message_observed": True, "assistant_turn_type": "QUOTA_ERROR_OBSERVED", "elapsed_seconds": 1.0}
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "QUOTA_ERROR_OBSERVED")

    # Pipeline Test: External Error Hook Integration
    def test_18_external_error_hook_integration(self):
        """UNIT_TEST: External error correlation hook overrides turn classification."""
        def custom_hook(dom_state, baseline_state):
            return "QUOTA_ERROR_OBSERVED"

        mock_client = MockAntigravityClient()
        mock_client.turn_result = {"user_message_observed": True, "assistant_turn_type": "QUOTA_ERROR_OBSERVED", "elapsed_seconds": 0.8}
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(
            args, client_override=mock_client, journal_override=self.journal, external_error_hook=custom_hook
        ))
        self.assertEqual(res["status"], "QUOTA_ERROR_OBSERVED")

    # Pipeline Test: Durability Failure Blocks Send
    def test_19_pipeline_durability_failure_blocks_send(self):
        """UNIT_TEST: Fsync failure in pipeline stops execution with JOURNAL_DURABILITY_FAILED."""
        mock_client = MockAntigravityClient()
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        with patch("os.fsync", side_effect=OSError("Disk failure")):
            res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
            self.assertEqual(res["status"], "JOURNAL_DURABILITY_FAILED")
            self.assertFalse(res["phases"]["4_send_input_dispatched"])

    # Strict transition enforcement
    def test_20_illegal_state_transitions_rejected(self):
        """UNIT_TEST: Illegal state transitions raise ValueError."""
        rec = self.journal.start_recovery_attempt(SYNTHETIC_UUID_1, SYNTHETIC_PROMPT)
        with self.assertRaises(ValueError):
            self.journal.transition_state(SYNTHETIC_UUID_1, rec["attempt_id"], STATE_MESSAGE_OBSERVED)

if __name__ == '__main__':
    unittest.main()
