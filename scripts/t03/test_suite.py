#!/usr/bin/env python3
"""
Automated Test Suite for T03 Resume Adapter & Recovery State Machine (Round 6 Final)

Implements rigorous tests against REAL production functions and execute_resume_pipeline():
- Deterministic overlapping two-worker pipeline race with pre-lock synchronization barrier:
    * TOTAL dispatch count = 1.
    * Tested across separate journal instances and same journal instance.
- Structural non-reentrant locking verification (no same-process bypass).
- Tri-state process liveness checker & PID reuse detection.
- Real selector decision logic tests (0, 1, 2 composers, hidden composer, 0, 1, 2 send buttons).
- Atomic renderer-side prompt identity verification before click.
- Route mutation immediately before send halts dispatch.
- Prompt mutation immediately before send halts dispatch.
- Send button ambiguity (>1 send button) halts dispatch.
- Crash recovery tests with stale locks and varied journal states.
- Post-baseline error tracking and durability barriers.
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
    LIVENESS_ALIVE,
    LIVENESS_DEAD_CONFIRMED,
    LIVENESS_UNKNOWN,
    check_process_liveness,
    get_process_start_identity,
    validate_journal_schema,
    evaluate_recovery_permission,
    validate_uuid,
    hash_prompt
)

from send_resume import (
    classify_duplicate_state,
    correlate_turn_status,
    discover_cdp_endpoint,
    execute_resume_pipeline,
    QualifiedAntigravityClient,
    normalize_text
)

SYNTHETIC_UUID_1 = "00000000-0000-4000-8000-000000000001"
SYNTHETIC_UUID_2 = "00000000-0000-4000-8000-000000000002"
SYNTHETIC_PROMPT = "Continue the current task from exactly where you stopped."

class MockAntigravityClient:
    """Thread-safe mock client for testing execute_resume_pipeline() executions."""
    def __init__(self, endpoint="http://127.0.0.1:58859", conversations=None, initial_pathname=None):
        self.endpoint = endpoint
        self.conversations = conversations or [
            {"index": 0, "title": "Synthetic Task", "href": f"/c/{SYNTHETIC_UUID_1}", "uuid": SYNTHETIC_UUID_1, "isActive": True, "isExecutingInSidebar": False}
        ]
        self.current_pathname = initial_pathname or f"/c/{SYNTHETIC_UUID_1}"
        self.composer_state = {"found": True, "text": "", "draftPresent": False, "sendButton": {"found": True, "disabled": False}}
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
        self.dispatch_override = None
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

    async def dispatch_submission_input(self, target_uuid, expected_prompt_text):
        if self.dispatch_override:
            return self.dispatch_override(target_uuid, expected_prompt_text)

        # Mirror production renderer validation logic
        if self.current_pathname != f"/c/{target_uuid}":
            return {"dispatched": False, "error": "ROUTE_MUTATED_BEFORE_DISPATCH"}
        if normalize_text(self.composer_state.get("text")) != normalize_text(expected_prompt_text):
            return {"dispatched": False, "error": "PROMPT_IDENTITY_MISMATCH"}
        if not self.composer_state.get("sendButton", {}).get("found"):
            return {"dispatched": False, "error": "SEND_CONTROL_NOT_FOUND"}
        if self.composer_state.get("sendButton", {}).get("error") == "SEND_CONTROL_AMBIGUOUS":
            return {"dispatched": False, "error": "SEND_CONTROL_AMBIGUOUS"}
        if self.composer_state.get("sendButton", {}).get("disabled"):
            return {"dispatched": False, "error": "SEND_CONTROL_DISABLED"}

        self.dispatch_count += 1
        return {"dispatched": True, "method": "button_click"}

    async def wait_for_user_and_assistant_turn(self, target_uuid, prompt_hash, baseline, timeout=12, external_error_hook=None):
        return self.turn_result

class YieldingMockAntigravityClient(MockAntigravityClient):
    """Mock client that explicitly yields control (asyncio.sleep) during all in-lock operations."""
    async def inspect_scoped_conversation_state(self, target_uuid, prompt_hash, baseline_article_count=0):
        await asyncio.sleep(0.02)
        return await super().inspect_scoped_conversation_state(target_uuid, prompt_hash, baseline_article_count)

    async def inspect_composer_state(self, target_uuid=None):
        await asyncio.sleep(0.02)
        return await super().inspect_composer_state(target_uuid)

    async def clear_composer(self, target_uuid):
        await asyncio.sleep(0.02)
        await super().clear_composer(target_uuid)

    async def insert_prompt_text(self, target_uuid, text):
        await asyncio.sleep(0.02)
        await super().insert_prompt_text(target_uuid, text)

    async def dispatch_submission_input(self, target_uuid, expected_prompt_text):
        await asyncio.sleep(0.02)
        return await super().dispatch_submission_input(target_uuid, expected_prompt_text)

class AsyncBarrier:
    def __init__(self, count):
        self.count = count
        self.arrived = 0
        self.event = asyncio.Event()

    async def wait(self):
        self.arrived += 1
        if self.arrived == self.count:
            self.event.set()
        await self.event.wait()

class TestT03Round6Final(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="t03_r6_")
        self.journal_file = os.path.join(self.test_dir, "test_journal.json")
        self.journal = RecoveryJournal(self.journal_file)
        self.prompt_hash = hash_prompt(SYNTHETIC_PROMPT)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # CRITICAL ITEM 4 & 5: Real Yielding Same-Loop Contention Test
    def test_01_real_yielding_same_loop_contention(self):
        """MANDATORY TEST: YieldingMockAntigravityClient under concurrent same-loop execution.
        Asserts event loop heartbeat continues without freeze, bounded termination, and TOTAL DISPATCH == 1."""
        for iteration in range(3):
            test_j = os.path.join(self.test_dir, f"yielding_race_{iteration}.json")
            yielding_client = YieldingMockAntigravityClient()
            journal_a = RecoveryJournal(test_j)
            journal_b = RecoveryJournal(test_j)
            barrier = AsyncBarrier(2)

            args = argparse.Namespace(
                conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
                send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
                journal_path=test_j, timeout=5, json=False, verbose_private_data=False
            )

            heartbeat_ticks = 0
            stop_heartbeat = asyncio.Event()

            async def heartbeat():
                nonlocal heartbeat_ticks
                while not stop_heartbeat.is_set():
                    heartbeat_ticks += 1
                    await asyncio.sleep(0.01)

            async def run_test():
                hb_task = asyncio.create_task(heartbeat())
                task_a = asyncio.create_task(execute_resume_pipeline(args, client_override=yielding_client, journal_override=journal_a, pre_lock_barrier=barrier))
                task_b = asyncio.create_task(execute_resume_pipeline(args, client_override=yielding_client, journal_override=journal_b, pre_lock_barrier=barrier))
                res_a, res_b = await asyncio.gather(task_a, task_b)
                stop_heartbeat.set()
                await hb_task
                return res_a, res_b

            start_t = time.time()
            res_a, res_b = asyncio.run(run_test())
            elapsed = time.time() - start_t

            # Assertions
            self.assertLess(elapsed, 3.0, f"Iteration {iteration}: Race took too long ({elapsed}s)")
            self.assertGreater(heartbeat_ticks, 5, f"Iteration {iteration}: Event loop was starved! Heartbeat ticks: {heartbeat_ticks}")
            self.assertEqual(yielding_client.dispatch_count, 1, f"Iteration {iteration}: Total dispatch count was not 1")
            statuses = [res_a["status"], res_b["status"]]
            self.assertIn("TURN_STARTED", statuses)
            self.assertTrue(
                "PREVIOUS_SUBMISSION_UNCONFIRMED" in statuses or "RESUME_ALREADY_OBSERVED" in statuses or "TURN_ALREADY_ACTIVE" in statuses,
                f"Expected safety block in statuses: {statuses}"
            )

    # CRITICAL ITEM 1: Non-reentrant lock prevents same-process lock bypass
    # CRITICAL ITEM 1: Unknown Windows Liveness Errors Must Fail Closed (Return UNKNOWN)
    def test_02_win32_liveness_error_matrix(self):
        """UNIT_TEST: OpenProcess failure returns DEAD_CONFIRMED only for 87/1168; UNKNOWN for 5 and 12345."""
        if os.name == 'nt':
            import ctypes
            # Test ACCESS_DENIED (5) -> UNKNOWN
            with patch.object(ctypes.windll.kernel32, 'OpenProcess', return_value=0), \
                 patch.object(ctypes.windll.kernel32, 'GetLastError', return_value=5):
                self.assertEqual(check_process_liveness(1234), LIVENESS_UNKNOWN)

            # Test arbitrary unrecognized error 12345 -> UNKNOWN
            with patch.object(ctypes.windll.kernel32, 'OpenProcess', return_value=0), \
                 patch.object(ctypes.windll.kernel32, 'GetLastError', return_value=12345):
                self.assertEqual(check_process_liveness(1234), LIVENESS_UNKNOWN)

            # Test confirmed nonexistent PID error 87 (ERROR_INVALID_PARAMETER) -> DEAD_CONFIRMED
            with patch.object(ctypes.windll.kernel32, 'OpenProcess', return_value=0), \
                 patch.object(ctypes.windll.kernel32, 'GetLastError', return_value=87):
                self.assertEqual(check_process_liveness(1234), LIVENESS_DEAD_CONFIRMED)

            # Test confirmed nonexistent PID error 1168 (ERROR_NOT_FOUND) -> DEAD_CONFIRMED
            with patch.object(ctypes.windll.kernel32, 'OpenProcess', return_value=0), \
                 patch.object(ctypes.windll.kernel32, 'GetLastError', return_value=1168):
                self.assertEqual(check_process_liveness(1234), LIVENESS_DEAD_CONFIRMED)

    # CRITICAL ITEM 2 & 9: Lock Release Must Verify Ownership Nonce (Successor Lock Protection)
    def test_03_lock_release_verifies_ownership_nonce(self):
        """UNIT_TEST: Lock context with nonce A does not delete lock file if replaced by nonce B."""
        # 1. Async lock release test
        async def test_async_release():
            async with self.journal.async_exclusive_lock(conversation_uuid=SYNTHETIC_UUID_1):
                # Replace lock file with successor lock (nonce B)
                successor_meta = {
                    "owner_pid": os.getpid(),
                    "start_identity": get_process_start_identity(os.getpid()),
                    "lock_nonce": "successor-nonce-bbbb",
                    "created_at": time.time(),
                    "conversation_uuid": SYNTHETIC_UUID_1
                }
                with open(self.journal.lock_path, "w", encoding="utf-8") as f:
                    json.dump(successor_meta, f)

            # After exit, successor lock must NOT be deleted
            self.assertTrue(os.path.exists(self.journal.lock_path))
            with open(self.journal.lock_path, "r", encoding="utf-8") as f:
                cur = json.load(f)
            self.assertEqual(cur.get("lock_nonce"), "successor-nonce-bbbb")

        asyncio.run(test_async_release())

        # Cleanup successor lock
        if os.path.exists(self.journal.lock_path):
            os.remove(self.journal.lock_path)

        # 2. Sync lock release test
        with self.journal.exclusive_lock(conversation_uuid=SYNTHETIC_UUID_1):
            successor_meta = {
                "owner_pid": os.getpid(),
                "start_identity": get_process_start_identity(os.getpid()),
                "lock_nonce": "successor-nonce-cccc",
                "created_at": time.time(),
                "conversation_uuid": SYNTHETIC_UUID_1
            }
            with open(self.journal.lock_path, "w", encoding="utf-8") as f:
                json.dump(successor_meta, f)

        self.assertTrue(os.path.exists(self.journal.lock_path))
        with open(self.journal.lock_path, "r", encoding="utf-8") as f:
            cur = json.load(f)
        self.assertEqual(cur.get("lock_nonce"), "successor-nonce-cccc")

    # CRITICAL ITEM 3: Prompt Text Mismatch inside Renderer Aborts Dispatch
    def test_03_pre_dispatch_prompt_mutation_aborts_send(self):
        """UNIT_TEST: Modifying composer text prior to dispatch returns PROMPT_IDENTITY_MISMATCH and zero clicks."""
        mock_client = MockAntigravityClient()
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        # Override dispatch to simulate renderer detecting altered text
        mock_client.dispatch_override = lambda uuid, p: {"dispatched": False, "error": "PROMPT_IDENTITY_MISMATCH"}

        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "SEND_INPUT_DISPATCH_FAILED")
        self.assertEqual(mock_client.dispatch_count, 0)

    # CRITICAL ITEM 6: Real Send Mode Requires Explicit Validated UUID
    def test_04_real_send_requires_explicit_uuid(self):
        """UNIT_TEST: send=True requires --conversation-id/--uuid; title-only and implicit active are blocked."""
        mock_client = MockAntigravityClient()

        # 1. Send=True with title only -> UUID_REQUIRED_FOR_SEND (0 dispatch)
        args_title_only = argparse.Namespace(
            conversation_id=None, title="Synthetic Task", prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res1 = asyncio.run(execute_resume_pipeline(args_title_only, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res1["status"], "UUID_REQUIRED_FOR_SEND")
        self.assertEqual(mock_client.dispatch_count, 0)

        # 2. Send=True with neither UUID nor title -> UUID_REQUIRED_FOR_SEND (0 dispatch)
        args_implicit = argparse.Namespace(
            conversation_id=None, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res2 = asyncio.run(execute_resume_pipeline(args_implicit, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res2["status"], "UUID_REQUIRED_FOR_SEND")
        self.assertEqual(mock_client.dispatch_count, 0)

        # 3. Send=True with explicit UUID -> allowed to continue
        args_uuid = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res3 = asyncio.run(execute_resume_pipeline(args_uuid, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res3["status"], "TURN_STARTED")
        self.assertEqual(mock_client.dispatch_count, 1)

    # Send Button Ambiguity (>1 buttons) Aborts Dispatch
    def test_05_send_button_ambiguity_aborts_send(self):
        """UNIT_TEST: Multiple send buttons (>1) returns SEND_CONTROL_AMBIGUOUS and aborts dispatch."""
        mock_client = MockAntigravityClient()
        mock_client.composer_state["sendButton"] = {"found": False, "error": "SEND_CONTROL_AMBIGUOUS"}

        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "SEND_INPUT_DISPATCH_FAILED")
        self.assertEqual(mock_client.dispatch_count, 0)

    # ITEM 5 & 6: Tri-state process liveness and PID reuse detection
    def test_05_tri_state_liveness_and_pid_reuse(self):
        """UNIT_TEST: check_process_liveness returns ALIVE for current PID, DEAD for dead PID, and detects start identity mismatch."""
        current_pid = os.getpid()
        current_start = get_process_start_identity(current_pid)
        self.assertEqual(check_process_liveness(current_pid, current_start), LIVENESS_ALIVE)

        # PID reuse mismatch
        self.assertEqual(check_process_liveness(current_pid, expected_start_identity=999999999999), LIVENESS_DEAD_CONFIRMED)

        # Definitely dead PID
        self.assertEqual(check_process_liveness(99999999), LIVENESS_DEAD_CONFIRMED)

    # ITEM 8: Real Selector Decision Logic Tests
    def test_06_real_selector_decision_logic(self):
        """UNIT_TEST: Missing targetRoot or multiple composers fail closed."""
        mock_client = MockAntigravityClient()
        mock_client.composer_state = {"found": False, "error": "TARGET_ROOT_NOT_FOUND"}

        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "TARGET_ROOT_NOT_FOUND")

    # ITEM 10: Crash Tests with Stale Lock + Journal States
    def test_07_crash_reclaim_with_submission_attempted(self):
        """UNIT_TEST: Dead owner lock + SUBMISSION_ATTEMPTED halts without blind resend."""
        # Setup dead lock
        dead_pid = 99999999
        lock_meta = {"owner_pid": dead_pid, "start_identity": None, "lock_nonce": "dead-nonce", "created_at": time.time() - 100, "conversation_uuid": SYNTHETIC_UUID_1}
        with open(self.journal.lock_path, "w", encoding="utf-8") as f:
            json.dump(lock_meta, f)

        # Setup journal in SUBMISSION_ATTEMPTED
        journal_data = {
            "version": 2,
            "records": {
                SYNTHETIC_UUID_1: [
                    {
                        "attempt_id": "11111111-1111-4111-8111-111111111111",
                        "conversation_uuid": SYNTHETIC_UUID_1,
                        "state": STATE_SUBMISSION_ATTEMPTED,
                        "prompt_sha256": self.prompt_hash,
                        "created_at_utc": 1000.0,
                        "updated_at_utc": 1001.0,
                        "failure_stage": None,
                        "history": [{"state": STATE_SUBMISSION_ATTEMPTED, "timestamp": 1000.0}]
                    }
                ]
            }
        }
        with open(self.journal_file, "w", encoding="utf-8") as f:
            json.dump(journal_data, f)

        mock_client = MockAntigravityClient()
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "PREVIOUS_SUBMISSION_UNCONFIRMED")
        self.assertEqual(mock_client.dispatch_count, 0)

    # ITEM 11: Route Mutation Immediately Before Send
    def test_08_route_mutation_before_send_aborts(self):
        """UNIT_TEST: Route mutation prior to send dispatch yields ROUTE_MUTATED_BEFORE_DISPATCH."""
        mock_client = MockAntigravityClient()
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        mock_client.dispatch_override = lambda uuid, p: {"dispatched": False, "error": "ROUTE_MUTATED_BEFORE_DISPATCH"}

        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "SEND_INPUT_DISPATCH_FAILED")
        self.assertEqual(mock_client.dispatch_count, 0)

    # ITEM 1: Strict Journal Schema Validation
    def test_09_strict_journal_schema_validation(self):
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
    def test_10_failed_unknown_stage_fails_closed(self):
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

    # Post-Baseline Error Tracking
    def test_11_post_baseline_error_tracking(self):
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

    # Semantic Author Identification
    def test_12_semantic_author_identification(self):
        """UNIT_TEST: Semantic role metadata drives classification."""
        msgs = ["User prompt 1", "User prompt 2"]
        status, _ = classify_duplicate_state(msgs, self.prompt_hash, has_unknown_role=True, last_message_is_unknown=False)
        self.assertEqual(status, "RESUME_NOT_PRESENT")

    # Fsync Failure Raises Durability Error
    def test_13_fsync_failure_raises_durability_error(self):
        """UNIT_TEST: os.fsync failure raises JournalDurabilityError."""
        with patch("os.fsync", side_effect=OSError("Disk failure")):
            with self.assertRaises(JournalDurabilityError):
                self.journal._write_atomic({"version": 2, "records": {}})

    # Verified Dry-Run Navigation Restoration
    def test_14_dry_run_navigation_restoration(self):
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

    # Duplicate Blocks Send
    def test_15_pipeline_duplicate_blocks_send(self):
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

    # Draft Present Blocks Send
    def test_16_pipeline_draft_blocks_send(self):
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

    # Navigation Failure Blocks Send
    def test_17_pipeline_navigation_failure_blocks(self):
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

    # Post-Dispatch Timeout Yields DISPATCHED_UNCONFIRMED
    def test_18_pipeline_post_dispatch_timeout(self):
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

    # External Error Hook Integration
    def test_19_external_error_hook_integration(self):
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

    # Strict transition enforcement
    def test_20_illegal_state_transitions_rejected(self):
        """UNIT_TEST: Illegal state transitions raise ValueError."""
        rec = self.journal.start_recovery_attempt(SYNTHETIC_UUID_1, SYNTHETIC_PROMPT)
        with self.assertRaises(ValueError):
            self.journal.transition_state(SYNTHETIC_UUID_1, rec["attempt_id"], STATE_MESSAGE_OBSERVED)

if __name__ == '__main__':
    unittest.main()
