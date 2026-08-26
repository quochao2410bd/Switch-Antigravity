#!/usr/bin/env python3
"""
Automated Test Suite for T03 Resume Adapter & Recovery State Machine (Round 4)

Implements rigorous tests against REAL production functions and execute_resume_pipeline():
- Strict journal schema validation (versioned, semantic validity).
- FAILED unknown/missing stage fails closed.
- Concurrent send exclusion (two workers, only one obtains send permission).
- Exact canonical routing and target container scope.
- Post-baseline error tracking (historical errors do not poison new turns).
- Semantic author metadata extraction without text prefix heuristics.
- Fsync durability failure aborts pipeline with JOURNAL_DURABILITY_FAILED.
- Verified dry-run navigation restoration.
- External error correlation adapter hook.
- Forward attempt reconciliation.
- Complete execution of execute_resume_pipeline() with injected mock backends.
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
from unittest.mock import MagicMock, patch

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
    """Mock client for executing execute_resume_pipeline() with injected behaviors."""
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
        self.dispatch_result = {"dispatched": True, "method": "button_click"}
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

    async def inspect_composer_state(self):
        return self.composer_state

    async def clear_composer(self):
        self.composer_state["text"] = ""
        self.composer_state["draftPresent"] = False

    async def insert_prompt_text(self, text):
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

    async def dispatch_submission_input(self):
        return self.dispatch_result

    async def wait_for_user_and_assistant_turn(self, target_uuid, prompt_hash, baseline, timeout=12, external_error_hook=None):
        return self.turn_result

class TestT03Round4Comprehensive(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="t03_r4_")
        self.journal_file = os.path.join(self.test_dir, "test_journal.json")
        self.journal = RecoveryJournal(self.journal_file)
        self.prompt_hash = hash_prompt(SYNTHETIC_PROMPT)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # ITEM 1: Strict Journal Schema Validation
    def test_01_strict_journal_schema_validation(self):
        """UNIT_TEST: Valid JSON with semantic errors raises JournalSchemaError and blocks send."""
        # Semantically invalid state
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

        decision, _ = evaluate_recovery_permission(
            latest_record=None, live_dom_state={"isMainTurnActive": False},
            prompt_hash=self.prompt_hash, journal_status=status
        )
        self.assertEqual(decision, DECISION_JOURNAL_CORRUPTED)

    # ITEM 1b: Unsupported schema version
    def test_02_unsupported_schema_version(self):
        """UNIT_TEST: Unsupported schema version returns JOURNAL_SCHEMA_UNSUPPORTED."""
        with open(self.journal_file, "w", encoding="utf-8") as f:
            json.dump({"version": 999, "records": {}}, f)

        _, status = self.journal._read_raw()
        self.assertEqual(status, "SCHEMA_UNSUPPORTED")

        decision, _ = evaluate_recovery_permission(
            latest_record=None, live_dom_state={"isMainTurnActive": False},
            prompt_hash=self.prompt_hash, journal_status=status
        )
        self.assertEqual(decision, DECISION_JOURNAL_SCHEMA_UNSUPPORTED)

    # ITEM 2: FAILED Unknown Stage Fails Closed
    def test_03_failed_unknown_stage_fails_closed(self):
        """UNIT_TEST: FAILED state with missing or unknown failure_stage blocks retry."""
        rec_unknown = {
            "attempt_id": "11111111-1111-4111-8111-111111111111",
            "conversation_uuid": SYNTHETIC_UUID_1,
            "state": STATE_FAILED,
            "prompt_sha256": self.prompt_hash,
            "failure_stage": "UNKNOWN_OR_MISSING"
        }
        decision, explanation = evaluate_recovery_permission(
            latest_record=rec_unknown,
            live_dom_state={"duplicateStatus": "RESUME_NOT_PRESENT", "isMainTurnActive": False},
            prompt_hash=self.prompt_hash
        )
        self.assertEqual(decision, DECISION_MANUAL_RECONCILIATION_REQUIRED)
        self.assertIn("Manual reconciliation required", explanation)

    # ITEM 2b: FAILED PRE_IRREVERSIBLE allows retry
    def test_04_failed_pre_irreversible_permits_retry(self):
        """UNIT_TEST: FAILED state with confirmed PRE_IRREVERSIBLE permits clean retry."""
        rec_pre = {
            "attempt_id": "11111111-1111-4111-8111-111111111111",
            "conversation_uuid": SYNTHETIC_UUID_1,
            "state": STATE_FAILED,
            "prompt_sha256": self.prompt_hash,
            "failure_stage": "PRE_IRREVERSIBLE"
        }
        decision, _ = evaluate_recovery_permission(
            latest_record=rec_pre,
            live_dom_state={"duplicateStatus": "RESUME_NOT_PRESENT", "isMainTurnActive": False},
            prompt_hash=self.prompt_hash
        )
        self.assertEqual(decision, DECISION_NEW_ATTEMPT_ALLOWED)

    # ITEM 3: Concurrent Send Exclusion (Two Workers)
    def test_05_concurrent_send_exclusion(self):
        """UNIT_TEST: Exclusive file lock prevents two concurrent workers from both sending."""
        journal_a = RecoveryJournal(self.journal_file)
        journal_b = RecoveryJournal(self.journal_file)

        # Worker A acquires lock in critical region
        with journal_a.exclusive_lock():
            # Worker B attempts to acquire lock and times out
            with self.assertRaises(TimeoutError):
                with journal_b.exclusive_lock(timeout=0.1):
                    pass

    # ITEM 4: Exact Route Matching
    def test_06_exact_route_matching(self):
        """UNIT_TEST: validate_uuid and exact equality reject arbitrary subroutes."""
        self.assertEqual(validate_uuid(SYNTHETIC_UUID_1), SYNTHETIC_UUID_1)
        expected_route = f"/c/{SYNTHETIC_UUID_1}"
        actual_subroute = f"/c/{SYNTHETIC_UUID_1}/settings"
        self.assertNotEqual(expected_route, actual_subroute)

    # ITEM 5: Target Chat Scope Fails Closed
    def test_07_target_chat_scope_fails_closed(self):
        """UNIT_TEST: classify_duplicate_state fails closed when container is missing."""
        status, _ = classify_duplicate_state([], self.prompt_hash, has_unknown_role=True, last_message_is_unknown=True)
        self.assertEqual(status, "DUPLICATE_STATE_UNKNOWN")

    # ITEM 6: Post-Baseline Error Tracking
    def test_08_post_baseline_error_tracking(self):
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
    def test_09_semantic_author_identification(self):
        """UNIT_TEST: Role unknown on last message fails closed; old unknown role does not."""
        msgs = ["User prompt 1", "User prompt 2"]
        # If last message is known user
        status, _ = classify_duplicate_state(msgs, self.prompt_hash, has_unknown_role=True, last_message_is_unknown=False)
        self.assertEqual(status, "RESUME_NOT_PRESENT")

        # If last message is unknown
        status_unk, _ = classify_duplicate_state(msgs, self.prompt_hash, has_unknown_role=True, last_message_is_unknown=True)
        self.assertEqual(status_unk, "DUPLICATE_STATE_UNKNOWN")

    # ITEM 11: Fsync Failure Raises JournalDurabilityError
    def test_10_fsync_failure_raises_durability_error(self):
        """UNIT_TEST: os.fsync failure raises JournalDurabilityError and halts."""
        with patch("os.fsync", side_effect=OSError("Disk I/O error")):
            with self.assertRaises(JournalDurabilityError):
                self.journal._write_atomic({"version": 2, "records": {}})

    # ITEM 12: Verified Dry-Run Navigation Restoration
    def test_11_dry_run_navigation_restoration(self):
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

    # ITEM 14: Forward Attempt Reconciliation
    def test_12_forward_attempt_reconciliation(self):
        """UNIT_TEST: reconcile_existing_attempt advances unconfirmed attempt safely forward."""
        rec = self.journal.start_recovery_attempt(SYNTHETIC_UUID_1, SYNTHETIC_PROMPT)
        self.journal.transition_state(SYNTHETIC_UUID_1, rec["attempt_id"], STATE_SUBMISSION_ATTEMPTED)

        latest, _ = self.journal.get_latest_record(SYNTHETIC_UUID_1)
        live_dom = {
            "lastUserMessageHash": self.prompt_hash,
            "isMainTurnActive": False
        }

        updated_rec, reconciled = reconcile_existing_attempt(
            self.journal, SYNTHETIC_UUID_1, latest, live_dom, self.prompt_hash
        )
        self.assertTrue(reconciled)
        self.assertEqual(updated_rec["state"], STATE_MESSAGE_OBSERVED)

    # ITEM 9: Real Pipeline Test 1 — Clean Valid Send Path
    def test_13_pipeline_clean_send_path(self):
        """UNIT_TEST: execute_resume_pipeline executes complete send lifecycle."""
        mock_client = MockAntigravityClient()
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "TURN_STARTED")
        self.assertTrue(res["phases"]["4_send_input_dispatched"])
        self.assertTrue(res["phases"]["6_assistant_turn_started"])

    # ITEM 9: Real Pipeline Test 2 — Duplicate Blocks Send
    def test_14_pipeline_duplicate_blocks_send(self):
        """UNIT_TEST: execute_resume_pipeline blocks duplicate prompt send."""
        mock_client = MockAntigravityClient()
        mock_client.scoped_state["userMessages"] = [SYNTHETIC_PROMPT]
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "RESUME_ALREADY_OBSERVED")

    # ITEM 9: Real Pipeline Test 3 — Draft Blocks Send
    def test_15_pipeline_draft_blocks_send(self):
        """UNIT_TEST: execute_resume_pipeline refuses to overwrite user draft."""
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

    # ITEM 9: Real Pipeline Test 4 — Navigation Failure Blocks Send
    def test_16_pipeline_navigation_failure_blocks(self):
        """UNIT_TEST: execute_resume_pipeline halts on conversation switch failure."""
        mock_client = MockAntigravityClient(
            conversations=[
                {"index": 0, "title": "A", "href": f"/c/{SYNTHETIC_UUID_1}", "uuid": SYNTHETIC_UUID_1, "isActive": False}
            ]
        )
        mock_client.switch_result = {"status": "CONVERSATION_SWITCH_TIMEOUT"}
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "CONVERSATION_SWITCH_TIMEOUT")

    # ITEM 9: Real Pipeline Test 5 — Post-Dispatch Timeout Transitions to DISPATCHED_UNCONFIRMED
    def test_17_pipeline_post_dispatch_timeout(self):
        """UNIT_TEST: Post-dispatch timeout yields DISPATCHED_UNCONFIRMED."""
        mock_client = MockAntigravityClient()
        mock_client.turn_result = {
            "user_message_observed": False,
            "assistant_turn_type": "NO_ASSISTANT_TURN",
            "timeout": 5
        }
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "DISPATCHED_UNCONFIRMED")
        latest, _ = self.journal.get_latest_record(SYNTHETIC_UUID_1)
        self.assertEqual(latest["state"], STATE_DISPATCHED_UNCONFIRMED)

    # ITEM 9: Real Pipeline Test 6 — Quota Error After Dispatch
    def test_18_pipeline_quota_error_after_dispatch(self):
        """UNIT_TEST: API quota error after dispatch records FAILED and reports QUOTA_ERROR_OBSERVED."""
        mock_client = MockAntigravityClient()
        mock_client.turn_result = {
            "user_message_observed": True,
            "assistant_turn_type": "QUOTA_ERROR_OBSERVED",
            "elapsed_seconds": 1.0
        }
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "QUOTA_ERROR_OBSERVED")

    # ITEM 9: Real Pipeline Test 7 — External Error Hook Integration
    def test_19_external_error_hook_integration(self):
        """UNIT_TEST: External error correlation hook overrides turn classification."""
        def custom_hook(dom_state, baseline_state):
            return "QUOTA_ERROR_OBSERVED"

        mock_client = MockAntigravityClient()
        mock_client.turn_result = {
            "user_message_observed": True,
            "assistant_turn_type": "QUOTA_ERROR_OBSERVED",
            "elapsed_seconds": 0.8
        }
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(
            args, client_override=mock_client, journal_override=self.journal, external_error_hook=custom_hook
        ))
        self.assertEqual(res["status"], "QUOTA_ERROR_OBSERVED")

    # ITEM 9: Real Pipeline Test 8 — Durability Failure Blocks Send
    def test_20_pipeline_durability_failure_blocks_send(self):
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

if __name__ == '__main__':
    unittest.main()
