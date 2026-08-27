import unittest
from unittest.mock import patch

import production_adapters as pa
from safe_production_adapters import SafeProductionAdapters


class SafeProductionAdapterTests(unittest.TestCase):
    def make_adapter(self, decision, structural_ok=True):
        obj = SafeProductionAdapters.__new__(SafeProductionAdapters)
        obj._t03_preflight = lambda event_id: {
            "ready": decision == "NEW_ATTEMPT_ALLOWED" and structural_ok,
            "status": "TRANSITION_PREFLIGHT_READY" if structural_ok else "TRANSITION_PREFLIGHT_BLOCKED",
            "t03_status": "DRY_RUN_READ_ONLY_SUCCESS" if structural_ok else "CONVERSATION_NOT_FOUND",
            "decision": decision,
            "exact_uuid": structural_ok,
            "draft_present": False if structural_ok else None,
        }
        obj._t03_args = lambda event_id, send: (event_id, send)
        return obj

    def test_new_attempt_executes_exactly_one_send_pipeline(self):
        obj = self.make_adapter("NEW_ATTEMPT_ALLOWED")
        calls = []

        async def fake_pipeline(args):
            calls.append(args)
            return {"status": "TURN_STARTED"}

        with patch.object(pa, "execute_resume_pipeline", fake_pipeline):
            result = obj.resume_conversation("evt_1")
        self.assertEqual(result["status"], "TURN_STARTED")
        self.assertEqual(calls, [("evt_1", True)])

    def test_turn_already_active_reconciles_without_send(self):
        obj = self.make_adapter("TURN_ALREADY_ACTIVE")
        with patch.object(pa, "execute_resume_pipeline") as mocked:
            result = obj.resume_conversation("evt_1")
        self.assertEqual(result["status"], "TURN_STARTED")
        self.assertTrue(result["reconciled_without_send"])
        mocked.assert_not_called()

    def test_resume_already_observed_reconciles_without_send(self):
        obj = self.make_adapter("RESUME_ALREADY_OBSERVED")
        with patch.object(pa, "execute_resume_pipeline") as mocked:
            result = obj.resume_conversation("evt_1")
        self.assertEqual(result["status"], "USER_MESSAGE_OBSERVED_ASSISTANT_PENDING")
        self.assertTrue(result["reconciled_without_send"])
        mocked.assert_not_called()

    def test_previous_submission_unconfirmed_never_resends(self):
        obj = self.make_adapter("PREVIOUS_SUBMISSION_UNCONFIRMED")
        with patch.object(pa, "execute_resume_pipeline") as mocked:
            result = obj.resume_conversation("evt_1")
        self.assertEqual(result["status"], "DISPATCHED_UNCONFIRMED")
        self.assertTrue(result["reconciled_without_send"])
        mocked.assert_not_called()

    def test_structural_preflight_failure_blocks(self):
        obj = self.make_adapter("NEW_ATTEMPT_ALLOWED", structural_ok=False)
        with patch.object(pa, "execute_resume_pipeline") as mocked:
            result = obj.resume_conversation("evt_1")
        self.assertEqual(result["status"], "POST_TRANSITION_PREFLIGHT_FAILED")
        mocked.assert_not_called()

    def test_unknown_recovery_decision_fails_closed(self):
        obj = self.make_adapter("RECOVERY_STATE_UNKNOWN")
        with patch.object(pa, "execute_resume_pipeline") as mocked:
            result = obj.resume_conversation("evt_1")
        self.assertEqual(result["status"], "POST_TRANSITION_PREFLIGHT_FAILED")
        mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
