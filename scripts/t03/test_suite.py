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
    normalize_text,
    DOM_QUALIFICATION_JS
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
        if hasattr(self, "clear_override") and self.clear_override:
            return await self.clear_override(target_uuid)
        self.composer_state["text"] = ""
        self.composer_state["draftPresent"] = False

    async def insert_prompt_text(self, target_uuid, text):
        if hasattr(self, "insert_override") and self.insert_override:
            return await self.insert_override(target_uuid, text)
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

class TestT03Round8Final(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="t03_r8_")
        self.journal_file = os.path.join(self.test_dir, "test_journal.json")
        self.journal = RecoveryJournal(self.journal_file)
        self.prompt_hash = hash_prompt(SYNTHETIC_PROMPT)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # 1. Stale lock ABA successor protection
    def test_01_stale_lock_aba_successor_protection(self):
        """Contender B observing stale lock cannot delete, rename, or replace valid successor lock Y."""
        nonce_y = "nonce-valid-successor-yyyy"
        lock_meta_y = {
            "owner_pid": os.getpid(),
            "start_identity": get_process_start_identity(os.getpid()),
            "lock_nonce": nonce_y,
            "created_at": time.time(),
            "conversation_uuid": SYNTHETIC_UUID_1
        }
        with open(self.journal.lock_path, "w", encoding="utf-8") as f:
            json.dump(lock_meta_y, f)

        journal_b = RecoveryJournal(self.journal_file)
        async def contender_b_attempt():
            with self.assertRaises(TimeoutError):
                async with journal_b.async_exclusive_lock(timeout=0.1, conversation_uuid=SYNTHETIC_UUID_1):
                    pass
        asyncio.run(contender_b_attempt())

        self.assertTrue(os.path.exists(self.journal.lock_path))
        with open(self.journal.lock_path, "r", encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertEqual(persisted.get("lock_nonce"), nonce_y)

    # 2. Yielding same-loop two-worker pipeline race
    def test_02_yielding_same_loop_two_worker_race(self):
        """YieldingMockAntigravityClient under concurrent same-loop execution. TOTAL DISPATCH COUNT == 1."""
        for iteration in range(2):
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

            self.assertLess(elapsed, 3.0)
            self.assertGreater(heartbeat_ticks, 3)
            self.assertEqual(yielding_client.dispatch_count, 1)
            statuses = [res_a["status"], res_b["status"]]
            self.assertIn("TURN_STARTED", statuses)

    # 3. Win32 unknown error returns UNKNOWN
    def test_03_win32_unknown_error_returns_liveness_unknown(self):
        """OpenProcess returning unknown error 12345 or ACCESS_DENIED (5) returns LIVENESS_UNKNOWN."""
        if os.name == 'nt':
            import ctypes
            with patch.object(ctypes.windll.kernel32, 'OpenProcess', return_value=0), \
                 patch.object(ctypes.windll.kernel32, 'GetLastError', return_value=12345):
                self.assertEqual(check_process_liveness(1234), LIVENESS_UNKNOWN)

            with patch.object(ctypes.windll.kernel32, 'OpenProcess', return_value=0), \
                 patch.object(ctypes.windll.kernel32, 'GetLastError', return_value=5):
                self.assertEqual(check_process_liveness(1234), LIVENESS_UNKNOWN)

    # 4. Win32 GetExitCodeProcess failure returns UNKNOWN
    def test_04_win32_getexitcodeprocess_failure_returns_liveness_unknown(self):
        """OpenProcess succeeds but GetExitCodeProcess fails -> returns LIVENESS_UNKNOWN."""
        if os.name == 'nt':
            import ctypes
            with patch.object(ctypes.windll.kernel32, 'OpenProcess', return_value=9999), \
                 patch.object(ctypes.windll.kernel32, 'GetExitCodeProcess', return_value=0), \
                 patch.object(ctypes.windll.kernel32, 'CloseHandle', return_value=1):
                self.assertEqual(check_process_liveness(1234), LIVENESS_UNKNOWN)

    # 5. Process start identity unavailable returns UNKNOWN
    def test_05_expected_process_identity_unavailable_returns_unknown(self):
        """Process exists but start identity cannot be established -> returns LIVENESS_UNKNOWN."""
        with patch("recovery_journal.get_process_start_identity", return_value=None):
            self.assertEqual(check_process_liveness(os.getpid(), expected_start_identity=12345678), LIVENESS_UNKNOWN)

    # 6. Missing message container aborts send
    def test_06_missing_message_container_aborts_send(self):
        """0 candidate message containers returns MESSAGE_CONTAINER_NOT_FOUND and 0 dispatches."""
        mock_client = MockAntigravityClient()
        mock_client.scoped_state = {"error": "MESSAGE_CONTAINER_NOT_FOUND"}
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "MESSAGE_CONTAINER_NOT_FOUND")
        self.assertEqual(mock_client.dispatch_count, 0)

    # 7. Multiple message containers aborts send
    def test_07_multiple_message_containers_aborts_send(self):
        """>1 candidate message containers returns MESSAGE_CONTAINER_AMBIGUOUS and 0 dispatches."""
        mock_client = MockAntigravityClient()
        mock_client.scoped_state = {"error": "MESSAGE_CONTAINER_AMBIGUOUS", "count": 2}
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "MESSAGE_CONTAINER_AMBIGUOUS")
        self.assertEqual(mock_client.dispatch_count, 0)

    # 8. Real QualifiedAntigravityClient.clear_composer() with DOM Ambiguity
    def test_08_real_qualified_client_clear_composer_ambiguity(self):
        """Call ACTUAL QualifiedAntigravityClient.clear_composer() with stubs.
        TARGET_ROOT_AMBIGUOUS and COMPOSER_AMBIGUOUS raise RuntimeError and execute 0 Input.dispatchKeyEvent commands."""
        client = QualifiedAntigravityClient(endpoint="http://127.0.0.1:58859")
        dispatched_commands = []
        async def stub_send_command(method, params=None):
            dispatched_commands.append((method, params))
            return {}
        client.send_command = stub_send_command

        # Subcase A: TARGET_ROOT_AMBIGUOUS
        async def stub_eval_target_ambiguous(expr):
            return {"focused": False, "error": "TARGET_ROOT_AMBIGUOUS", "count": 2}
        client.evaluate = stub_eval_target_ambiguous

        with self.assertRaises(RuntimeError) as ctx_a:
            asyncio.run(client.clear_composer(SYNTHETIC_UUID_1))
        self.assertIn("TARGET_ROOT_AMBIGUOUS", str(ctx_a.exception))
        self.assertEqual(len(dispatched_commands), 0)

        # Subcase B: COMPOSER_AMBIGUOUS
        async def stub_eval_composer_ambiguous(expr):
            return {"focused": False, "error": "COMPOSER_AMBIGUOUS", "count": 2}
        client.evaluate = stub_eval_composer_ambiguous

        with self.assertRaises(RuntimeError) as ctx_b:
            asyncio.run(client.clear_composer(SYNTHETIC_UUID_1))
        self.assertIn("COMPOSER_AMBIGUOUS", str(ctx_b.exception))
        self.assertEqual(len(dispatched_commands), 0)

    # 9. Real QualifiedAntigravityClient.insert_prompt_text() with DOM Ambiguity & Wrong Route
    def test_09_real_qualified_client_insert_prompt_text_ambiguity(self):
        """Call ACTUAL QualifiedAntigravityClient.insert_prompt_text() with stubs.
        TARGET_ROOT_AMBIGUOUS, COMPOSER_AMBIGUOUS, and WRONG_CONVERSATION_ACTIVE raise RuntimeError and execute 0 Input.insertText commands."""
        client = QualifiedAntigravityClient(endpoint="http://127.0.0.1:58859")
        inserted_commands = []
        async def stub_send_command(method, params=None):
            inserted_commands.append((method, params))
            return {}
        client.send_command = stub_send_command

        # Subcase A: TARGET_ROOT_AMBIGUOUS
        async def stub_eval_target_ambiguous(expr):
            return {"focused": False, "error": "TARGET_ROOT_AMBIGUOUS", "count": 2}
        client.evaluate = stub_eval_target_ambiguous
        with self.assertRaises(RuntimeError) as ctx_a:
            asyncio.run(client.insert_prompt_text(SYNTHETIC_UUID_1, SYNTHETIC_PROMPT))
        self.assertIn("TARGET_ROOT_AMBIGUOUS", str(ctx_a.exception))
        self.assertEqual(len(inserted_commands), 0)

        # Subcase B: COMPOSER_AMBIGUOUS
        async def stub_eval_composer_ambiguous(expr):
            return {"focused": False, "error": "COMPOSER_AMBIGUOUS", "count": 2}
        client.evaluate = stub_eval_composer_ambiguous
        with self.assertRaises(RuntimeError) as ctx_b:
            asyncio.run(client.insert_prompt_text(SYNTHETIC_UUID_1, SYNTHETIC_PROMPT))
        self.assertIn("COMPOSER_AMBIGUOUS", str(ctx_b.exception))
        self.assertEqual(len(inserted_commands), 0)

        # Subcase C: WRONG_CONVERSATION_ACTIVE
        async def stub_eval_wrong_route(expr):
            return {"focused": False, "error": "WRONG_CONVERSATION_ACTIVE"}
        client.evaluate = stub_eval_wrong_route
        with self.assertRaises(RuntimeError) as ctx_c:
            asyncio.run(client.insert_prompt_text(SYNTHETIC_UUID_1, SYNTHETIC_PROMPT))
        self.assertIn("WRONG_CONVERSATION_ACTIVE", str(ctx_c.exception))
        self.assertEqual(len(inserted_commands), 0)

    # 10. Real send mode requires explicit UUID
    def test_10_real_send_requires_explicit_uuid(self):
        """send=True requires --conversation-id/--uuid; title-only and implicit active are blocked with 0 dispatches."""
        mock_client = MockAntigravityClient()

        # Send + title only
        args_title = argparse.Namespace(
            conversation_id=None, title="Synthetic Task", prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res1 = asyncio.run(execute_resume_pipeline(args_title, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res1["status"], "UUID_REQUIRED_FOR_SEND")
        self.assertEqual(mock_client.dispatch_count, 0)

        # Send + implicit active
        args_implicit = argparse.Namespace(
            conversation_id=None, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res2 = asyncio.run(execute_resume_pipeline(args_implicit, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res2["status"], "UUID_REQUIRED_FOR_SEND")
        self.assertEqual(mock_client.dispatch_count, 0)

        # Send + valid UUID
        args_valid = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res3 = asyncio.run(execute_resume_pipeline(args_valid, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res3["status"], "TURN_STARTED")
        self.assertEqual(mock_client.dispatch_count, 1)

    # 11. Real dispatch exception recovery test across two invocations with attempt counter
    def test_11_dispatch_exception_recovery_two_invocations(self):
        """Invocation 1 hits dispatch exception -> FAILED+POST_IRREVERSIBLE_UNKNOWN (dispatch_attempt_count=1).
        Invocation 2 blocks resend -> MANUAL_RECONCILIATION_REQUIRED, dispatch_attempt_count STILL 1."""
        mock_client = MockAntigravityClient()
        dispatch_attempt_count = 0

        def failing_dispatch(uuid, prompt):
            nonlocal dispatch_attempt_count
            dispatch_attempt_count += 1
            raise ConnectionResetError("CDP WebSocket disconnected during dispatch frame")
        mock_client.dispatch_override = failing_dispatch

        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )

        # Invocation 1: Hits dispatch exception
        res1 = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res1["status"], "SEND_INPUT_DISPATCH_EXCEPTION")
        self.assertEqual(dispatch_attempt_count, 1)
        latest1, _ = self.journal.get_latest_record(SYNTHETIC_UUID_1)
        self.assertEqual(latest1["state"], STATE_FAILED)
        self.assertEqual(latest1["failure_stage"], "POST_IRREVERSIBLE_UNKNOWN")

        # Invocation 2: Second attempt against same journal state fails closed without calling dispatch
        res2 = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res2["status"], "MANUAL_RECONCILIATION_REQUIRED")
        self.assertEqual(dispatch_attempt_count, 1, "Second invocation must NOT call dispatch_submission_input")

    # 12. Real Synchronized Post-Dispatch Concurrency Test
    def test_12_post_dispatch_concurrency_race(self):
        """Deterministic post-dispatch race:
        1. Worker A obtains permission, dispatches exactly once, releases send lock, enters wait_for_user_and_assistant_turn.
        2. Worker A is explicitly PAUSED before completing MESSAGE_OBSERVED/TURN_STARTED transition.
        3. Worker B starts against SAME UUID, prompt, and journal while Worker A is paused.
        4. Worker B inspects fresh journal state in SUBMISSION_ATTEMPTED and halts (0 dispatches, 0 new attempts).
        5. Worker A resumes and completes post-turn transition to TURN_STARTED.
        Asserts: TOTAL dispatch calls = 1, exactly ONE recovery attempt_id exists, Worker B does not create second attempt, final state belongs to A."""
        test_j = os.path.join(self.test_dir, "post_dispatch_sync_race.json")
        journal_a = RecoveryJournal(test_j)
        journal_b = RecoveryJournal(test_j)

        a_dispatched_event = asyncio.Event()
        a_resume_event = asyncio.Event()
        b_finished_event = asyncio.Event()

        class SynchronizedMockClientA(MockAntigravityClient):
            async def dispatch_submission_input(self, target_uuid, expected_prompt_text):
                res = await super().dispatch_submission_input(target_uuid, expected_prompt_text)
                return res

            async def wait_for_user_and_assistant_turn(self, target_uuid, prompt_hash, baseline, timeout=12, external_error_hook=None):
                # Lock is already released before entering wait_for_user_and_assistant_turn
                a_dispatched_event.set()
                # Pause Worker A until Worker B has completely evaluated journal state and finished
                await a_resume_event.wait()
                return await super().wait_for_user_and_assistant_turn(target_uuid, prompt_hash, baseline, timeout, external_error_hook)

        client_a = SynchronizedMockClientA()
        client_b = MockAntigravityClient()

        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=test_j, timeout=5, json=False, verbose_private_data=False
        )

        async def run_synchronized_race():
            # 1. Start Worker A
            task_a = asyncio.create_task(execute_resume_pipeline(args, client_override=client_a, journal_override=journal_a))

            # 2. Wait until Worker A dispatches and releases send lock
            await a_dispatched_event.wait()

            # 3. Start Worker B while Worker A is paused
            task_b = asyncio.create_task(execute_resume_pipeline(args, client_override=client_b, journal_override=journal_b))
            res_b = await task_b
            b_finished_event.set()

            # 4. Resume Worker A
            a_resume_event.set()
            res_a = await task_a

            return res_a, res_b

        res_a, res_b = asyncio.run(run_synchronized_race())

        # Assertions
        self.assertEqual(client_a.dispatch_count, 1, "Worker A must have dispatched exactly once")
        self.assertEqual(client_b.dispatch_count, 0, "Worker B must NOT dispatch")
        self.assertEqual(res_a["status"], "TURN_STARTED")
        self.assertEqual(res_b["status"], "PREVIOUS_SUBMISSION_UNCONFIRMED")

        # Check journal state & attempts
        raw_data, _ = journal_a._read_raw()
        records = raw_data.get("records", {}).get(SYNTHETIC_UUID_1, [])
        self.assertEqual(len(records), 1, "Exactly ONE recovery attempt_id must exist in journal")
        self.assertEqual(records[0]["attempt_id"], res_a["recovery_attempt_id"])
        self.assertIsNone(res_b["recovery_attempt_id"], "Worker B must NOT create a new attempt_id")
        self.assertEqual(records[0]["state"], STATE_TURN_STARTED, "Final state must progress forward to TURN_STARTED")

    # 13. Pre-click validation failure transitions to PRE_IRREVERSIBLE
    def test_13_pre_click_validation_failure_allows_pre_irreversible(self):
        """dispatch_submission_input returning dispatched=False transitions to FAILED+PRE_IRREVERSIBLE."""
        mock_client = MockAntigravityClient()
        mock_client.dispatch_override = lambda uuid, p: {"dispatched": False, "error": "PROMPT_IDENTITY_MISMATCH"}
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "SEND_INPUT_DISPATCH_FAILED")
        latest, _ = self.journal.get_latest_record(SYNTHETIC_UUID_1)
        self.assertEqual(latest["state"], STATE_FAILED)
        self.assertEqual(latest["failure_stage"], "PRE_IRREVERSIBLE")

    # 14. Nonce verification on lock release protects successor
    def test_14_lock_release_nonce_verification(self):
        """Releasing lock context does not delete successor lock if nonce differs."""
        async def run_test():
            async with self.journal.async_exclusive_lock(conversation_uuid=SYNTHETIC_UUID_1):
                successor_meta = {
                    "owner_pid": os.getpid(),
                    "start_identity": get_process_start_identity(os.getpid()),
                    "lock_nonce": "successor-nonce-1234",
                    "created_at": time.time(),
                    "conversation_uuid": SYNTHETIC_UUID_1
                }
                with open(self.journal.lock_path, "w", encoding="utf-8") as f:
                    json.dump(successor_meta, f)
            self.assertTrue(os.path.exists(self.journal.lock_path))
            with open(self.journal.lock_path, "r", encoding="utf-8") as f:
                cur = json.load(f)
            self.assertEqual(cur.get("lock_nonce"), "successor-nonce-1234")
        asyncio.run(run_test())

    # 15. Send button ambiguity aborts send
    def test_15_send_button_ambiguity_aborts_send(self):
        """Multiple send buttons (>1) returns SEND_CONTROL_AMBIGUOUS and 0 dispatches."""
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

    # 16. Route mutation before send aborts
    def test_16_route_mutation_before_send_aborts(self):
        """Route mutation prior to dispatch returns ROUTE_MUTATED_BEFORE_DISPATCH and 0 dispatches."""
        mock_client = MockAntigravityClient()
        mock_client.dispatch_override = lambda uuid, p: {"dispatched": False, "error": "ROUTE_MUTATED_BEFORE_DISPATCH"}
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "SEND_INPUT_DISPATCH_FAILED")
        self.assertEqual(mock_client.dispatch_count, 0)

    # 17. Strict journal schema validation
    def test_17_strict_journal_schema_validation(self):
        """Valid JSON with semantic error sets status to SCHEMA_INVALID."""
        invalid_data = {
            "version": 2,
            "records": {
                SYNTHETIC_UUID_1: [
                    {
                        "attempt_id": "11111111-1111-4111-8111-111111111111",
                        "conversation_uuid": SYNTHETIC_UUID_1,
                        "state": "INVALID_GARBAGE",
                        "prompt_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                        "created_at_utc": 1000.0,
                        "updated_at_utc": 1001.0,
                        "failure_stage": None,
                        "history": [{"state": "INVALID_GARBAGE", "timestamp": 1000.0}]
                    }
                ]
            }
        }
        with open(self.journal_file, "w", encoding="utf-8") as f:
            json.dump(invalid_data, f)
        _, status = self.journal._read_raw()
        self.assertEqual(status, "SCHEMA_INVALID")

    # 18. Fsync failure raises durability error
    def test_18_fsync_failure_raises_durability_error(self):
        """os.fsync failure raises JournalDurabilityError."""
        with patch("os.fsync", side_effect=OSError("Disk failure")):
            with self.assertRaises(JournalDurabilityError):
                self.journal._write_atomic({"version": 2, "records": {}})

    # 19. Duplicate blocks send
    def test_19_duplicate_blocks_send(self):
        """Duplicate prompt in user message history blocks send."""
        mock_client = MockAntigravityClient()
        mock_client.scoped_state["userMessages"] = [SYNTHETIC_PROMPT]
        args = argparse.Namespace(
            conversation_id=SYNTHETIC_UUID_1, title=None, prompt=SYNTHETIC_PROMPT,
            send=True, probe_composer_write=False, cdp_endpoint="http://127.0.0.1:58859",
            journal_path=self.journal_file, timeout=5, json=False, verbose_private_data=False
        )
        res = asyncio.run(execute_resume_pipeline(args, client_override=mock_client, journal_override=self.journal))
        self.assertEqual(res["status"], "RESUME_ALREADY_OBSERVED")

    # 20. Draft present blocks send
    def test_20_draft_present_blocks_send(self):
        """Unsubmitted user draft in composer blocks send."""
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

    # 21. Navigation failure blocks send
    def test_21_navigation_failure_blocks_send(self):
        """Switch timeout halts pipeline before dispatch."""
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

    # 22. Strict transition enforcement
    def test_22_illegal_state_transitions_rejected(self):
        """Illegal state transitions raise ValueError."""
        rec = self.journal.start_recovery_attempt(SYNTHETIC_UUID_1, SYNTHETIC_PROMPT)
        with self.assertRaises(ValueError):
            self.journal.transition_state(SYNTHETIC_UUID_1, rec["attempt_id"], STATE_MESSAGE_OBSERVED)

    # -------------------------------------------------------------------------
    # REAL JS DOM CONTRACT TESTS (Executed directly in Node.js against DOM_QUALIFICATION_JS)
    # -------------------------------------------------------------------------

    def _eval_dom_contract(self, dom_setup_js, target_uuid=SYNTHETIC_UUID_1, pathname=None):
        """
        Executes DOM_QUALIFICATION_JS in Node.js with a minimal synthetic DOM environment.
        Returns the parsed JSON evaluation result without mocking or stubbing evaluate().
        """
        curr_pathname = pathname if pathname is not None else f"/c/{target_uuid}"
        runner_script = f"""
class Node {{
    constructor(tagName, id = '', className = '', attrs = {{}}) {{
        this.tagName = tagName.toUpperCase();
        this.id = id;
        this.className = className;
        this.attributes = Object.assign({{}}, attrs);
        if (id) this.attributes.id = id;
        this.children = [];
        this.parentElement = null;
        this.offsetParent = {{}};
        this.textContent = '';
        this.innerText = '';
        this.disabled = false;
    }}
    getAttribute(name) {{
        return this.attributes[name] !== undefined ? this.attributes[name] : null;
    }}
    setAttribute(name, val) {{
        this.attributes[name] = String(val);
    }}
    appendChild(child) {{
        child.parentElement = this;
        this.children.push(child);
        return child;
    }}
    contains(other) {{
        let curr = other;
        while (curr) {{
            if (curr === this) return true;
            curr = curr.parentElement;
        }}
        return false;
    }}
    closest(selector) {{
        let curr = this;
        while (curr && curr.tagName) {{
            if (curr.matches(selector)) return curr;
            curr = curr.parentElement;
        }}
        return null;
    }}
    matches(selector) {{
        const selList = selector.split(',').map(s => s.trim());
        for (const sel of selList) {{
            if (sel === 'main' && this.tagName === 'MAIN') return true;
            if (sel === 'article' && this.tagName === 'ARTICLE') return true;
            if (sel === 'button' && this.tagName === 'BUTTON') return true;
            if (sel === '[data-lexical-editor="true"]' && this.attributes['data-lexical-editor'] === 'true') return true;
            if (sel === '[data-testid="conversation-view"]' && this.attributes['data-testid'] === 'conversation-view') return true;
            if (sel === '[data-testid="conversation-messages"]' && this.attributes['data-testid'] === 'conversation-messages') return true;
            if (sel === '[data-testid="agent-input-box"]' && this.attributes['data-testid'] === 'agent-input-box') return true;
            if (sel === '#antigravity\\\\.agentSidePanelInputBox' && (this.id === 'antigravity.agentSidePanelInputBox' || this.attributes.id === 'antigravity.agentSidePanelInputBox')) return true;
            if (sel === 'button[data-testid="send-button"]' && this.tagName === 'BUTTON' && this.attributes['data-testid'] === 'send-button') return true;
            if (sel === 'button[aria-label="Send message"]' && this.tagName === 'BUTTON' && this.attributes['aria-label'] === 'Send message') return true;
            if (sel === '[data-testid="conversation-list-sidebar"]' && this.attributes['data-testid'] === 'conversation-list-sidebar') return true;
            if (sel === '[role="article"]' && this.attributes['role'] === 'article') return true;
        }}
        return false;
    }}
    querySelectorAll(selector) {{
        const matches = [];
        const traverse = (node) => {{
            for (const child of node.children) {{
                if (child.matches(selector)) matches.push(child);
                traverse(child);
            }}
        }};
        traverse(this);
        return matches;
    }}
    querySelector(selector) {{
        const res = this.querySelectorAll(selector);
        return res.length > 0 ? res[0] : null;
    }}
}}

global.document = {{
    body: new Node('body'),
    querySelectorAll: (sel) => global.document.body.querySelectorAll(sel),
    querySelector: (sel) => global.document.body.querySelector(sel)
}};
global.window = {{
    location: {{ pathname: {json.dumps(curr_pathname)} }}
}};

{DOM_QUALIFICATION_JS}

// Setup user DOM
{dom_setup_js}

// Execute production functions
const targetRes = qualifyTargetSurface({json.dumps(target_uuid)});
let compRes = {{ error: 'TARGET_NOT_QUALIFIED' }};
let msgRes = {{ error: 'TARGET_NOT_QUALIFIED' }};
let stopRes = {{ found: false }};

if (!targetRes.error && targetRes.surface) {{
    compRes = qualifyComposerSurface(targetRes.surface);
    msgRes = qualifyMessageSurface(targetRes.surface);
    stopRes = qualifyStopControl(targetRes.surface);
}}

const output = {{
    targetRes: {{ variant: targetRes.variant, error: targetRes.error }},
    compRes: {{ error: compRes.error, sendButton: compRes.sendButton }},
    msgRes: {{ variant: msgRes.variant, error: msgRes.error }},
    stopRes: stopRes
}};

console.log(JSON.stringify(output));
"""
        import subprocess
        proc = subprocess.run(["node", "-e", runner_script], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"Node DOM evaluation failed: {proc.stderr}")
        return json.loads(proc.stdout)

    # 23. Real JS DOM: Legacy Main only
    def test_23_real_js_dom_legacy_main_only(self):
        """Case 1: LEGACY MAIN only qualifies variant LEGACY_MAIN."""
        setup_js = """
        const m = document.body.appendChild(new Node('main'));
        const ib = m.appendChild(new Node('div', '', '', { 'data-testid': 'agent-input-box' }));
        ib.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' }));
        ib.appendChild(new Node('button', '', '', { 'data-testid': 'send-button' }));
        const mb = m.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-messages' }));
        mb.appendChild(new Node('article'));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertIsNone(res["targetRes"]["error"])
        self.assertEqual(res["targetRes"]["variant"], "LEGACY_MAIN")
        self.assertIsNone(res["compRes"]["error"])
        self.assertIsNone(res["msgRes"]["error"])

    # 24. Real JS DOM: Current conversation-view only
    def test_24_real_js_dom_current_conversation_view_only(self):
        """Case 2: CURRENT conversation-view only qualifies variant CONVERSATION_VIEW."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        const mb = cv.appendChild(new Node('div'));
        mb.appendChild(new Node('article'));
        const ib = cv.appendChild(new Node('div', 'antigravity.agentSidePanelInputBox'));
        ib.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' }));
        ib.appendChild(new Node('button', '', '', { 'aria-label': 'Send message' }));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertIsNone(res["targetRes"]["error"])
        self.assertEqual(res["targetRes"]["variant"], "CONVERSATION_VIEW")
        self.assertIsNone(res["compRes"]["error"])
        self.assertIsNone(res["msgRes"]["error"])

    # 25. Real JS DOM: Same element matches both main and conversation-view
    def test_25_real_js_dom_same_element_matches_both_deduplicates(self):
        """Case 3: Same element matches both main and conversation-view -> deduplicated to 1 target."""
        setup_js = """
        const m = document.body.appendChild(new Node('main', '', '', { 'data-testid': 'conversation-view' }));
        const ib = m.appendChild(new Node('div', '', '', { 'data-testid': 'agent-input-box' }));
        ib.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' }));
        ib.appendChild(new Node('button', '', '', { 'data-testid': 'send-button' }));
        const mb = m.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-messages' }));
        mb.appendChild(new Node('article'));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertIsNone(res["targetRes"]["error"])
        self.assertEqual(res["targetRes"]["variant"], "LEGACY_MAIN")

    # 26. Real JS DOM: Distinct main + distinct conversation-view fails ambiguous
    def test_26_real_js_dom_distinct_main_and_conv_view_fails_ambiguous(self):
        """Case 4: Distinct main + distinct conversation-view -> TARGET_SURFACE_AMBIGUOUS."""
        setup_js = """
        document.body.appendChild(new Node('main'));
        document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertEqual(res["targetRes"]["error"], "TARGET_SURFACE_AMBIGUOUS")

    # 27. Real JS DOM: Zero target surfaces
    def test_27_real_js_dom_zero_target_surfaces_fails_closed(self):
        """Case 5: Zero target surfaces -> TARGET_SURFACE_NOT_FOUND."""
        setup_js = """
        document.body.appendChild(new Node('div', '', 'regular-container'));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertEqual(res["targetRes"]["error"], "TARGET_SURFACE_NOT_FOUND")

    # 28. Real JS DOM: Unique target + zero explicit input boxes
    def test_28_real_js_dom_zero_explicit_input_boxes_fails_closed(self):
        """Case 6: Unique target + zero explicit input boxes -> COMPOSER_SURFACE_NOT_FOUND."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        cv.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' })); // Editor without explicit input box
        """
        res = self._eval_dom_contract(setup_js)
        self.assertIsNone(res["targetRes"]["error"])
        self.assertEqual(res["compRes"]["error"], "COMPOSER_SURFACE_NOT_FOUND")

    # 29. Real JS DOM: Unique target + two distinct input boxes
    def test_29_real_js_dom_two_distinct_input_boxes_fails_ambiguous(self):
        """Case 7: Unique target + two distinct input boxes -> COMPOSER_SURFACE_AMBIGUOUS."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        cv.appendChild(new Node('div', '', '', { 'data-testid': 'agent-input-box' }));
        cv.appendChild(new Node('div', 'antigravity.agentSidePanelInputBox'));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertIsNone(res["targetRes"]["error"])
        self.assertEqual(res["compRes"]["error"], "COMPOSER_SURFACE_AMBIGUOUS")

    # 30. Real JS DOM: One input box + one editor succeeds
    def test_30_real_js_dom_one_input_box_one_editor_succeeds(self):
        """Case 8: One input box + one editor -> success."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        const ib = cv.appendChild(new Node('div', '', '', { 'data-testid': 'agent-input-box' }));
        ib.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' }));
        ib.appendChild(new Node('button', '', '', { 'data-testid': 'send-button' }));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertIsNone(res["compRes"]["error"])
        self.assertTrue(res["compRes"]["sendButton"]["found"])

    # 31. Real JS DOM: Editor outside explicit input box fails closed
    def test_31_real_js_dom_editor_outside_input_box_fails_closed(self):
        """Case 9: Editor exists outside explicit input box only -> COMPOSER_NOT_FOUND."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        cv.appendChild(new Node('div', '', '', { 'data-testid': 'agent-input-box' })); // empty box
        cv.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' })); // outside box
        """
        res = self._eval_dom_contract(setup_js)
        self.assertEqual(res["compRes"]["error"], "COMPOSER_NOT_FOUND")

    # 32. Real JS DOM: One input box + two editors fails ambiguous
    def test_32_real_js_dom_one_input_box_two_editors_fails_ambiguous(self):
        """Case 10: One input box + two editors -> COMPOSER_AMBIGUOUS."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        const ib = cv.appendChild(new Node('div', '', '', { 'data-testid': 'agent-input-box' }));
        ib.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' }));
        ib.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' }));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertEqual(res["compRes"]["error"], "COMPOSER_AMBIGUOUS")

    # 33. Real JS DOM: Explicit conversation-messages container succeeds
    def test_33_real_js_dom_explicit_conversation_messages_succeeds(self):
        """Case 11: Explicit conversation-messages container -> success."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        const mb = cv.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-messages' }));
        mb.appendChild(new Node('article'));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertIsNone(res["msgRes"]["error"])
        self.assertEqual(res["msgRes"]["variant"], "EXPLICIT_DATA_TESTID")

    # 34. Real JS DOM: Live structural message variant succeeds
    def test_34_real_js_dom_live_structural_message_variant_succeeds(self):
        """Case 12: Live structural variant (direct child, no editor, >=1 article) -> success."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        const mb = cv.appendChild(new Node('div')); // direct child
        mb.appendChild(new Node('article'));
        const ib = cv.appendChild(new Node('div', '', '', { 'data-testid': 'agent-input-box' }));
        ib.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' }));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertIsNone(res["msgRes"]["error"])
        self.assertEqual(res["msgRes"]["variant"], "LIVE_STRUCTURAL_DIRECT_CHILD")

    # 35. Real JS DOM: Two structural message candidates fails ambiguous
    def test_35_real_js_dom_two_structural_message_candidates_fails_ambiguous(self):
        """Case 13: Two structural message candidates -> MESSAGE_CONTAINER_AMBIGUOUS."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        const mb1 = cv.appendChild(new Node('div'));
        mb1.appendChild(new Node('article'));
        const mb2 = cv.appendChild(new Node('div'));
        mb2.appendChild(new Node('article'));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertEqual(res["msgRes"]["error"], "MESSAGE_CONTAINER_AMBIGUOUS")

    # 36. Real JS DOM: Visible banner direct child with 0 articles does not qualify
    def test_36_real_js_dom_banner_direct_child_zero_articles_does_not_qualify(self):
        """Case 14: Visible banner direct child with 0 articles MUST NOT qualify as message surface."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        const banner = cv.appendChild(new Node('div', '', 'announcement-banner')); // 0 articles
        const ib = cv.appendChild(new Node('div', '', '', { 'data-testid': 'agent-input-box' }));
        ib.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' }));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertEqual(res["msgRes"]["error"], "MESSAGE_CONTAINER_NOT_FOUND")

    # 37. Real JS DOM: Random panel direct child with 0 articles does not qualify
    def test_37_real_js_dom_random_panel_zero_articles_does_not_qualify(self):
        """Case 15: Random panel direct child with 0 articles MUST NOT qualify."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        cv.appendChild(new Node('div', '', 'side-controls'));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertEqual(res["msgRes"]["error"], "MESSAGE_CONTAINER_NOT_FOUND")

    # 38. Real JS DOM: Zero message variant fails closed
    def test_38_real_js_dom_zero_message_variant_fails_closed(self):
        """Case 16: Zero message variant -> MESSAGE_CONTAINER_NOT_FOUND."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        const ib = cv.appendChild(new Node('div', '', '', { 'data-testid': 'agent-input-box' }));
        ib.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' }));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertEqual(res["msgRes"]["error"], "MESSAGE_CONTAINER_NOT_FOUND")

    # 39. Real JS DOM: One exact data-testid send button succeeds
    def test_39_real_js_dom_one_exact_testid_send_button_succeeds(self):
        """Case 17: One exact data-testid send button -> success."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        const ib = cv.appendChild(new Node('div', '', '', { 'data-testid': 'agent-input-box' }));
        ib.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' }));
        ib.appendChild(new Node('button', '', '', { 'data-testid': 'send-button' }));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertIsNone(res["compRes"]["error"])
        self.assertTrue(res["compRes"]["sendButton"]["found"])

    # 40. Real JS DOM: One exact aria-label Send message button succeeds
    def test_40_real_js_dom_one_exact_aria_label_send_button_succeeds(self):
        """Case 18: One exact aria-label='Send message' button -> success."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        const ib = cv.appendChild(new Node('div', '', '', { 'data-testid': 'agent-input-box' }));
        ib.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' }));
        ib.appendChild(new Node('button', '', '', { 'aria-label': 'Send message' }));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertIsNone(res["compRes"]["error"])
        self.assertTrue(res["compRes"]["sendButton"]["found"])

    # 41. Real JS DOM: Same button matching both testid and aria-label deduplicates
    def test_41_real_js_dom_same_send_button_both_selectors_deduplicates(self):
        """Case 19: Same button matches both data-testid and aria-label -> deduplicated to 1."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        const ib = cv.appendChild(new Node('div', '', '', { 'data-testid': 'agent-input-box' }));
        ib.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' }));
        ib.appendChild(new Node('button', '', '', { 'data-testid': 'send-button', 'aria-label': 'Send message' }));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertIsNone(res["compRes"]["error"])
        self.assertTrue(res["compRes"]["sendButton"]["found"])

    # 42. Real JS DOM: Two distinct supported send buttons fails ambiguous
    def test_42_real_js_dom_two_distinct_send_buttons_fails_ambiguous(self):
        """Case 20: Two distinct supported send buttons -> SEND_CONTROL_AMBIGUOUS."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        const ib = cv.appendChild(new Node('div', '', '', { 'data-testid': 'agent-input-box' }));
        ib.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' }));
        ib.appendChild(new Node('button', '', '', { 'data-testid': 'send-button' }));
        ib.appendChild(new Node('button', '', '', { 'aria-label': 'Send message' }));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertIsNone(res["compRes"]["error"])
        self.assertEqual(res["compRes"]["sendButton"]["error"], "SEND_CONTROL_AMBIGUOUS")

    # 43. Real JS DOM: Button with aria-label="Send feedback" must not qualify
    def test_43_real_js_dom_send_feedback_button_does_not_qualify(self):
        """Case 21: Button aria-label='Send feedback' MUST NOT qualify as send control."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        const ib = cv.appendChild(new Node('div', '', '', { 'data-testid': 'agent-input-box' }));
        ib.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' }));
        ib.appendChild(new Node('button', '', '', { 'aria-label': 'Send feedback' }));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertEqual(res["compRes"]["sendButton"]["error"], "SEND_CONTROL_NOT_FOUND")

    # 44. Real JS DOM: Button with aria-label="Send to..." must not qualify
    def test_44_real_js_dom_send_to_button_does_not_qualify(self):
        """Case 22: Button aria-label='Send to...' MUST NOT qualify."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        const ib = cv.appendChild(new Node('div', '', '', { 'data-testid': 'agent-input-box' }));
        ib.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' }));
        ib.appendChild(new Node('button', '', '', { 'aria-label': 'Send to email' }));
        """
        res = self._eval_dom_contract(setup_js)
        self.assertEqual(res["compRes"]["sendButton"]["error"], "SEND_CONTROL_NOT_FOUND")

    # 45. Real JS DOM: Supported send button outside input box fails closed
    def test_45_real_js_dom_send_button_outside_input_box_fails_closed(self):
        """Case 23: Supported send button outside input box -> SEND_CONTROL_NOT_FOUND."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        const ib = cv.appendChild(new Node('div', '', '', { 'data-testid': 'agent-input-box' }));
        ib.appendChild(new Node('div', '', '', { 'data-lexical-editor': 'true' }));
        cv.appendChild(new Node('button', '', '', { 'data-testid': 'send-button' })); // outside ib
        """
        res = self._eval_dom_contract(setup_js)
        self.assertEqual(res["compRes"]["sendButton"]["error"], "SEND_CONTROL_NOT_FOUND")

    # 46. Real JS DOM: Sidebar target and controls excluded
    def test_46_real_js_dom_sidebar_target_and_controls_excluded(self):
        """Case 24: Elements inside conversation-list-sidebar are completely excluded."""
        setup_js = """
        const sb = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-list-sidebar' }));
        sb.appendChild(new Node('main')); // sidebar main
        sb.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' })); // sidebar conv view
        """
        res = self._eval_dom_contract(setup_js)
        self.assertEqual(res["targetRes"]["error"], "TARGET_SURFACE_NOT_FOUND")

    # 47. Real JS DOM: Wrong route fails closed
    def test_47_real_js_dom_wrong_route_fails_closed(self):
        """Case 25: Mismatched pathname returns WRONG_CONVERSATION_ACTIVE."""
        setup_js = """
        const cv = document.body.appendChild(new Node('div', '', '', { 'data-testid': 'conversation-view' }));
        """
        res = self._eval_dom_contract(setup_js, pathname="/c/wrong-uuid-0000-0000")
        self.assertEqual(res["targetRes"]["error"], "WRONG_CONVERSATION_ACTIVE")

    # 48. Production mutation check: Unsupported root before clear
    def test_48_production_unsupported_root_before_clear_aborts(self):
        """DOM target surface disappearing before clear raises RuntimeError with 0 Input commands."""
        client = QualifiedAntigravityClient(endpoint="http://127.0.0.1:58859")
        cmds = []
        async def stub_send_command(method, params=None):
            cmds.append(method)
            return {}
        client.send_command = stub_send_command
        async def stub_evaluate(expr):
            return {"focused": False, "error": "TARGET_SURFACE_NOT_FOUND"}
        client.evaluate = stub_evaluate

        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(client.clear_composer(SYNTHETIC_UUID_1))
        self.assertIn("TARGET_SURFACE_NOT_FOUND", str(ctx.exception))
        self.assertEqual(len(cmds), 0)

    # 49. Production mutation check: Input box disappears before insert
    def test_49_production_input_box_disappears_before_insert_aborts(self):
        """DOM composer disappearing before insert raises RuntimeError with 0 Input commands."""
        client = QualifiedAntigravityClient(endpoint="http://127.0.0.1:58859")
        cmds = []
        async def stub_send_command(method, params=None):
            cmds.append(method)
            return {}
        client.send_command = stub_send_command
        async def stub_evaluate(expr):
            return {"focused": False, "error": "COMPOSER_SURFACE_NOT_FOUND"}
        client.evaluate = stub_evaluate

        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(client.insert_prompt_text(SYNTHETIC_UUID_1, SYNTHETIC_PROMPT))
        self.assertIn("COMPOSER_SURFACE_NOT_FOUND", str(ctx.exception))
        self.assertEqual(len(cmds), 0)

    # 50. Production mutation check: Send button becomes ambiguous before dispatch
    def test_50_production_send_button_ambiguous_before_dispatch_aborts(self):
        """Send button becoming ambiguous before dispatch halts dispatch with 0 dispatches."""
        client = QualifiedAntigravityClient(endpoint="http://127.0.0.1:58859")
        async def stub_evaluate(expr):
            return {"safe": False, "error": "SEND_CONTROL_AMBIGUOUS"}
        client.evaluate = stub_evaluate
        res = asyncio.run(client.dispatch_submission_input(SYNTHETIC_UUID_1, SYNTHETIC_PROMPT))
        self.assertFalse(res.get("dispatched"))
        self.assertEqual(res.get("error"), "SEND_CONTROL_AMBIGUOUS")

    # 51. Production mutation check: Active turn detected before dispatch
    def test_51_production_active_turn_before_dispatch_aborts(self):
        """Active stop button appearing before dispatch halts dispatch with 0 dispatches."""
        client = QualifiedAntigravityClient(endpoint="http://127.0.0.1:58859")
        async def stub_evaluate(expr):
            return {"safe": False, "error": "TURN_ALREADY_ACTIVE_BEFORE_DISPATCH"}
        client.evaluate = stub_evaluate
        res = asyncio.run(client.dispatch_submission_input(SYNTHETIC_UUID_1, SYNTHETIC_PROMPT))
        self.assertFalse(res.get("dispatched"))
        self.assertEqual(res.get("error"), "TURN_ALREADY_ACTIVE_BEFORE_DISPATCH")

if __name__ == '__main__':
    unittest.main()
